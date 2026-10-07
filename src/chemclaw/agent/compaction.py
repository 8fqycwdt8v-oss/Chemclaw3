"""Keep a session's thread inside a token budget.

Two deterministic, LLM-free edits run inside `wrap_model_call`, cheapest first:

1. `ClearOlderToolResultsEdit` — lossless: older tool results become a placeholder (keeping the
   note ids they cited); the model can re-fetch. Its own, lower trigger.
2. `KeepLastConversationGroupsEdit` — destructive: the oldest conversation groups are cut, on a
   group boundary, until the thread fits the budget.

Budgets are in billed tokens and converted by `agent/context_budget.py`, which also charges the
request prefix. Both edits are non-destructive to graph state: they narrow only the list this
call is sent, and the next call re-derives the same reduction. The checkpoint tables are bounded
elsewhere (retention, and `checkpointer._PRUNE_SUPERSEDED`).

Upstream's summarizer is installed switched off (`disabled_summarizer`): a summary rewrites
framed, untrusted evidence into unframed prose replayed every turn. `agent/condense.py` is a tool
result, not thread history, so it is not an exception to this.

A reduction tells the repeat guard which calls were cleared (`forget_calls`), and
`RecordContextCompaction` measures what the edits actually did: the compaction counter, one
`context.compacted` event per turn (reclaimed tokens, cleared tools, groups dropped) and the
over-budget counter. Edit and observer failures degrade to an uncompacted request instead of
ending the turn; upstream's own deep copy and counter setup are outside that guard.
`deepagents.FilesystemMiddleware` offloads an oversized final `HumanMessage` before this group;
`tests/test_compaction.py` holds every shipped producer below its threshold.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain.agents.middleware import (
    AgentMiddleware,
    ContextEditingMiddleware,
    ModelRequest,
)
from langchain.agents.middleware.context_editing import ContextEdit, TokenCounter
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages

from chemclaw.agent.authz import KNOWLEDGE_READ_TOOLS
from chemclaw.agent.context_budget import (
    MeasureRequestPrefix,
    current_context,
    effective_trigger,
    note_model_call,
    prefix_tokens,
)
from chemclaw.agent.framing import SYSTEM_SPEECH_MARK
from chemclaw.agent.repeat_guard import forget_calls
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import degraded, record_metric
from chemclaw.core.model_prose import ModelProse
from chemclaw.kg.note import is_note_slug

logger = logging.getLogger(__name__)

# What a cleared tool result leaves behind. It says why the result is gone (otherwise it reads as a
# tool that returned nothing) and gives no instruction, since it repeats per cleared result; the
# guidance lives once in the system prompt. It carries the system-speech mark, which
# `framing._MARK_FORGERY` makes unforgeable, so the model may trust it.
_PLACEHOLDER_SENTENCE = ModelProse(
    "Earlier tool result dropped to stay inside this session's context budget."
)


def _placeholder(extra: str = "") -> str:
    """The one spelling of a cleared result: the sentence, any addition, then the mark.

    Composed from one sentence so the plain and citation renderings cannot drift apart.
    """
    return f"[{_PLACEHOLDER_SENTENCE}{extra}] {SYSTEM_SPEECH_MARK}"


TOOL_RESULT_PLACEHOLDER = _placeholder()

# The `response_metadata` key that marks a `HumanMessage` as a note a middleware appended to *this
# request* rather than a message the thread holds. Nothing reads it but `_opens_a_group`.
_REQUEST_NOTE = "chemclaw_request_note"


def request_note(text: str) -> HumanMessage:
    """A human-role note for one request, which the conversation window will not treat as a turn.

    A group is a chemist's message and what answers it; treating a request-only note (e.g.
    `loop_cap.AnswerAtTheCap`'s wrap-up) as the newest group would let the window cut the chemist's
    actual question. Marked in `response_metadata`, which is never sent to the provider.
    """
    return HumanMessage(text, response_metadata={_REQUEST_NOTE: True})


def _opens_a_group(message: AnyMessage) -> bool:
    """Whether `message` starts a conversation group: a human message the thread holds.

    A `request_note` starts nothing, so the newest group is always the chemist's latest message.
    """
    return isinstance(message, HumanMessage) and not message.response_metadata.get(_REQUEST_NOTE)


# Note ids inside a cleared result, read from `EvidenceChunk.source_note_id` in this repository's
# own repr, so a citation index survives the clearing.
_CITED_NOTE_ID = re.compile(r"source_note_id='([^']+)'")
# How many ids to name before the list itself costs real budget.
_MAX_NAMED_CITATIONS = 12


def cited_note_ids(content: object) -> list[str]:
    """Every *resolvable* note id a tool result cites, in first-seen order, deduplicated.

    Filtered by `is_note_slug`: the placeholder tells the model to `expand_note` these, and chunk
    origins from shares, ELN warehouses or vendored datasets are not notes. Public for tests.
    """
    seen: dict[str, None] = {}
    for note_id in _CITED_NOTE_ID.findall(str(content)):
        if is_note_slug(note_id):
            seen.setdefault(note_id, None)
    return list(seen)


@dataclass(slots=True)
class ClearOlderToolResultsEdit(ContextEdit):
    """Clear older tool results, oldest first, with the bounds upstream's `ClearToolUsesEdit` lacks.

    - `keep` is raised to cover every result of the newest batch, so a parallel fan-out is never
      cleared before the model has seen it.
    - The overshoot is passed as `clear_at_least`, so clearing stops at the trigger rather than
      wiping everything but `keep`.
    - The strategy itself is first-party and linear (`_clear_older_tool_results`); upstream's is
      quadratic in thread length on the event loop.
    """

    trigger: int
    """Billed tokens the whole *request* may cost before older tool results are cleared.

    `effective_trigger` converts it: it subtracts this request's own prefix and divides by the
    measured estimator ratio, so the number compared against `count_tokens` here is what is left
    for the thread. A configured value at or below the prefix leaves nothing and floors at 1,
    which is the shipped `agent_tool_result_clear_trigger`'s state — see that setting's comment.
    """

    keep: int
    """Floor on the newest results kept verbatim. The newest batch raises it when it is wider."""

    placeholder: str
    """What a cleared result leaves behind."""

    def apply(self, messages: list[AnyMessage], *, count_tokens: TokenCounter) -> None:
        """Clear older tool results, never this step's own, and stop at the trigger."""
        budget = effective_trigger(self.trigger)
        tokens = count_tokens(messages)
        if tokens <= budget:
            return
        # Read citations before the bodies carrying them are cleared: the oldest result is usually
        # the evidence sweep, and its note ids decide whether the answer can cite anything.
        #
        # Only from this repository's own retrieval tools, identified by the tool name on the
        # calling `AIMessage` (`ToolMessage.name` may not survive middleware rewrites). A
        # connector's text could otherwise forge a system-authored citation.
        called = {
            call["id"]: call["name"]
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        }
        citations = {
            message.tool_call_id: cited_note_ids(message.content)
            for message in messages
            if isinstance(message, ToolMessage)
            and called.get(message.tool_call_id) in KNOWLEDGE_READ_TOOLS
        }
        _clear_older_tool_results(
            messages,
            count_tokens=count_tokens,
            keep=max(self.keep, newest_batch_size(messages)),
            # The overshoot, so clearing stops as soon as the thread is back under the trigger
            # rather than continuing to the end of the candidate list.
            clear_at_least=tokens - budget,
            placeholder=self.placeholder,
        )
        for message in messages:
            if not isinstance(message, ToolMessage) or message.content != self.placeholder:
                continue
            cited = citations.get(message.tool_call_id) or []
            if not cited:
                continue
            named = cited[:_MAX_NAMED_CITATIONS]
            more = "" if len(cited) == len(named) else f", and {len(cited) - len(named)} more"
            message.content = _placeholder(
                f" It cited: {', '.join(named)}{more}. "
                "Call expand_note on any of these to read it again."
            )


# Upstream's marker for a cleared result, kept verbatim: `_cleared_calls` reads it to forgive
# calls, and `_clear_older_tool_results` skips results already carrying it, including any cleared
# by an upstream edit composed elsewhere.
_CLEARED_MARKER: dict[str, object] = {"cleared": True, "strategy": "clear_tool_uses"}


def _preceded_tool_results(
    messages: Sequence[AnyMessage],
) -> list[tuple[int, ToolMessage, AIMessage | None]]:
    """Every tool result with the assistant message that immediately precedes it, oldest first.

    One forward pass where upstream rescans the prefix per result. Positional like upstream: the
    caller checks the id is in that message's `tool_calls`, so a result whose call was made earlier
    is left alone.

    Args:
        messages: The thread as the edit received it.

    Returns:
        `(index, result, the assistant message before it or None)` per `ToolMessage`, in list
        order — the order `keep` slices from the end of.
    """
    found: list[tuple[int, ToolMessage, AIMessage | None]] = []
    latest: AIMessage | None = None
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            latest = message
        elif isinstance(message, ToolMessage):
            found.append((index, message, latest))
    return found


def _clear_older_tool_results(
    messages: list[AnyMessage],
    *,
    count_tokens: TokenCounter,
    keep: int,
    clear_at_least: int,
    placeholder: str,
) -> None:
    """Replace older tool results with `placeholder`, oldest first, until enough is reclaimed.

    `ClearToolUsesEdit.apply` minus the unused `clear_tool_inputs` and `exclude_tools`, and linear
    in thread length. The reclaim per result is the difference between the result and its
    placeholder, not a recount of the whole thread; this is exact because
    `count_tokens_approximately` is additive per message, and a model-based counter's constant
    overhead cancels in the difference. `tests/test_compaction.py` holds it to upstream's output and
    to linear scaling.

    Args:
        messages: The thread, edited in place — the `ContextEdit` protocol.
        count_tokens: The estimator the middleware resolved, in whatever unit it counts.
        keep: How many of the newest tool results are protected, counted over every result
            (matching upstream, which slices before it filters).
        clear_at_least: Stop once this many tokens have been reclaimed; `0` clears every candidate.
        placeholder: What a cleared result leaves behind.
    """
    candidates = _preceded_tool_results(messages)
    if keep >= len(candidates):
        return
    if keep:
        candidates = candidates[:-keep]
    reclaimed = 0
    for index, result, caller in candidates:
        if result.response_metadata.get("context_editing", {}).get("cleared"):
            continue
        if caller is None:
            continue
        if not any(call.get("id") == result.tool_call_id for call in caller.tool_calls):
            continue
        messages[index] = result.model_copy(
            update={
                "artifact": None,
                "content": placeholder,
                "response_metadata": {
                    **result.response_metadata,
                    "context_editing": dict(_CLEARED_MARKER),
                },
            }
        )
        if clear_at_least > 0:
            reclaimed += count_tokens([result]) - count_tokens([messages[index]])
            if reclaimed >= clear_at_least:
                return


def newest_batch_size(messages: Sequence[AnyMessage]) -> int:
    """How many results in `messages` answer the newest assistant message that called tools.

    `ToolNode` appends a step's results after its `AIMessage`, so they are the trailing ones and a
    count suffices for `keep`. 0 when no assistant message called a tool.
    """
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None)
        if not calls:
            continue
        wanted = {str(call.get("id")) for call in calls if call.get("id")}
        return sum(
            1
            for candidate in messages
            if isinstance(candidate, ToolMessage) and str(candidate.tool_call_id) in wanted
        )
    return 0


@dataclass(slots=True)
class KeepLastConversationGroupsEdit(ContextEdit):
    """Cut the oldest conversation back to the token budget, on a group boundary.

    What bounds a thread that called no tools. The cut is `max(by_tokens, by_groups)`, the more
    aggressive arm: by tokens via `trim_messages(strategy="last")`, and by `keep` groups when set.
    `agent_keep_last_conversation_groups` ships at 0, since a non-zero `keep` usually binds first
    and makes the budget irrelevant.

    A group is a human message and everything answering it, so a cut on that boundary never
    separates a tool call from its result; `start_on="human"` is what makes `trim_messages` respect
    it. The cut never passes the newest group, so the list is never emptied: a single group larger
    than the budget is sent over budget. Ordered after the tool-result edit, so conversation is
    dropped only when clearing was not enough.
    """

    trigger: int = 100_000
    """Billed tokens the whole *request* may cost — and, less the prefix, the ceiling it cuts to.

    Converted to the estimator's unit by `effective_trigger`, which also subtracts this request's
    own prefix and clamps against a declared context window; it is not compared to `count_tokens`
    directly.

    A ceiling rather than a target: `keep` may take the cut further, and did so on every ordinary
    thread while it defaulted to 12.
    """

    keep: int = 0
    """Floor on the cut: groups older than the newest `keep` always go, whatever the budget says.

    `0` disables the arm, which is what the shipped configuration now asks for — the budget above
    is then the whole rule. The default here matches `agent_keep_last_conversation_groups` so a
    directly-constructed edit behaves as the deployment's does; `tests/test_compaction.py` still
    drives the arm explicitly, because a deployment may re-arm it.
    """

    def apply(self, messages: list[AnyMessage], *, count_tokens: TokenCounter) -> None:
        """Cut `messages` in place back to the effective budget or fewer, when over it.

        In place per the `ContextEdit` protocol (the middleware hands the same list to each edit),
        so an index is computed and deleted rather than `trim_messages`' result returned.
        `self.trigger` is in billed tokens and is reconciled through `effective_trigger`, which also
        charges the prefix.
        """
        budget = effective_trigger(self.trigger)
        if count_tokens(messages) <= budget:
            return
        # Only human messages the thread holds start groups; a `request_note` does not.
        starts = [index for index, message in enumerate(messages) if _opens_a_group(message)]
        if not starts:
            # No group boundary to cut on, so no cut this edit can take without stranding a pairing.
            return
        kept = trim_messages(
            messages,
            max_tokens=budget,
            token_counter=count_tokens,
            strategy="last",
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
        # `kept` is a suffix of `messages` — `strategy="last"` with `allow_partial=False` and no
        # system message to re-insert can only drop a prefix — so its length is the cut index.
        by_tokens = len(messages) - len(kept)
        # `keep == 0` (the shipped value) and `keep` above the group count both mean "no floor":
        # `starts[0] <= by_tokens`, so `max` returns `by_tokens`. Indexing past the count would
        # raise inside a middleware.
        by_groups = starts[-self.keep] if 0 < self.keep <= len(starts) else starts[0]
        # The newest group is the floor on what can be kept, per the clamp in the class docstring.
        cut = min(max(by_tokens, by_groups), starts[-1])
        if cut <= 0:
            return
        # DEBUG: this re-derives the same cut on every model call. The once-per-turn record is
        # `_record_reduction`'s `context.compacted` event.
        logger.debug(
            "context budget exceeded: dropping %d of %d message(s) to fit %d tokens; "
            "%d of %d conversation groups survive",
            cut,
            len(messages),
            budget,
            sum(1 for start in starts if start >= cut),
            len(starts),
        )
        del messages[:cut]


def disabled_summarizer(model: Any, backend: Any) -> Any:
    """Upstream's summarizer, constructed switched off — it arrives whether or not we want it.

    `create_deep_agent` composes a `SummarizationMiddleware` unconditionally, so "no summarizer"
    must be decided here. A summary is model prose over framed, untrusted content, so the envelope
    does not survive and injected text would be replayed every turn. `trigger=None` is upstream's
    own off state (asserted in `tests/test_compaction.py`). Passed through `middleware=` so upstream
    swaps it into its slot by name; `excluded_middleware` can silently miss.

    Args:
        model: The turn's resolved chat model; required by the constructor, never called.
        backend: The turn's backend; held for the offload path that cannot run.

    Returns:
        The middleware to hand `create_deep_agent(middleware=…)` in upstream's slot.
    """
    from langchain.agents.middleware.summarization import SummarizationMiddleware

    return SummarizationMiddleware(model=model, backend=backend, trigger=None)


# Degradation kinds already reported loudly. Process-wide: a shape failure is one fault however
# many turns meet it.
_REPORTED: set[str] = set()


def _degrade_once(marker: str, message: str, *args: object, traceback: bool = True) -> None:
    """Count every degradation; report the first of each kind loudly and the rest at DEBUG.

    These guards run per model call, so logging every occurrence with a traceback at ERROR would
    flood. Every occurrence still increments `chemclaw_degraded_total`. `traceback=False` for the
    one caller not inside an `except`.
    """
    first = marker not in _REPORTED
    _REPORTED.add(marker)
    degraded(
        logger,
        "compaction",
        message,
        *args,
        level=logging.ERROR if first else logging.DEBUG,
        exc_info=first and traceback,
    )


@dataclass(slots=True)
class GuardedEdit(ContextEdit):
    """One context edit, wrapped so that a failure inside it costs the reduction and not the turn.

    Continuing uncompacted is the safe direction: the request is usually under the provider limit,
    and if not, the provider's context-length error is classified and told to the chemist. Both
    edits are wrapped, since each reads message shapes `langchain_core` owns.
    """

    edit: ContextEdit
    """The edit to run; its failures are absorbed, its work is not otherwise touched."""

    def apply(self, messages: list[AnyMessage], *, count_tokens: TokenCounter) -> None:
        """Run the edit, or record that it could not run and leave `messages` as they are.

        An edit that raised half-way may leave a partially reduced list; every reduction is a suffix
        operation on a copy, so that is a smaller valid thread, not a corrupt one.
        """
        try:
            self.edit.apply(messages, count_tokens=count_tokens)
        except Exception:
            _degrade_once(
                type(self.edit).__name__,
                "the %s context edit failed; this model call proceeds uncompacted",
                type(self.edit).__name__,
            )


class OffLoopContextEditing(ContextEditingMiddleware):
    """Upstream's editing middleware, with its synchronous work moved off the event loop.

    Upstream's `awrap_model_call` deep-copies and edits inline in the coroutine, and that CPU
    (mostly the deep copy) blocks every other session on the pod. Rather than copying upstream's
    method body, this runs the synchronous `wrap_model_call` in a thread with a handler that
    captures the prepared request, so upstream changes are inherited. If the handler is never
    called, that is reported rather than silently sending an uncompacted request.

    Safe in a worker thread: the edits only read ambient state (the prefix and ratio, visible via
    the copied context) and only mutate thread-safe process state. `tests/test_upstream_surface.py`
    holds the handler contract.
    """

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """Run the edits in a worker thread, then make the model call on the loop as usual.

        Args:
            request: The model request as the chain below this middleware built it.
            handler: The rest of the chain, which must be awaited on the event loop.

        Returns:
            Whatever the handler returns for the edited request.
        """
        edited: list[ModelRequest[Any]] = []

        def capture(prepared: ModelRequest[Any]) -> None:
            edited.append(prepared)

        prepare = super().wrap_model_call
        await asyncio.to_thread(prepare, request, cast(Any, capture))
        if not edited:
            _degrade_once(
                "off-loop-editing",
                "the context editing middleware did not hand its edited request on; "
                "this model call proceeds uncompacted",
                traceback=False,
            )
        return await handler(edited[-1] if edited else request)


def context_compaction_middleware() -> list[Any]:
    """The context policy, as the middleware list `build_langgraph_agent` splices in.

    The prefix measure is outermost, the editor reduces, and `RecordContextCompaction` observes from
    innermost, the only position that sees both the edited list and the full thread in state.
    """
    return [
        # Outermost, because everything below budgets against a prefix only a middleware can see.
        MeasureRequestPrefix(),
        # Off the event loop, because everything it does is synchronous CPU over a list that grows
        # with the session and every millisecond of it is borne by this pod's other sessions.
        OffLoopContextEditing(
            edits=[
                # Both wrapped, so a raising edit costs the reduction rather than the turn (see
                # `GuardedEdit`). `agent_keep_last_tool_groups` counts the newest tool *results*
                # despite its name (kept for ENV compatibility); `ClearOlderToolResultsEdit` raises
                # `keep` to cover the newest batch.
                GuardedEdit(
                    ClearOlderToolResultsEdit(
                        # Its own trigger, well below the window's budget: clearing is lossless, so
                        # running it early
                        # spares conversation groups the window would otherwise delete.
                        trigger=settings.agent_tool_result_clear_trigger,
                        keep=settings.agent_keep_last_tool_groups,
                        placeholder=TOOL_RESULT_PLACEHOLDER,
                    )
                ),
                GuardedEdit(
                    KeepLastConversationGroupsEdit(
                        trigger=settings.agent_context_token_budget,
                        keep=settings.agent_keep_last_conversation_groups,
                    )
                ),
            ]
        ),
        RecordContextCompaction(),
    ]


def _record_reduction(request: ModelRequest[Any]) -> None:
    """Publish this model call's reduction, never letting the observation break the call.

    Separate from `GuardedEdit` because it reads state and metadata shapes this module does not own.
    `forget_calls` is inside the guard too: losing it costs at most one recoverable refusal.
    """
    try:
        _publish_reduction(request)
    except Exception:
        _degrade_once(
            "reduction",
            "could not measure this model call's context reduction; the call itself is unaffected",
        )


def _publish_reduction(request: ModelRequest[Any]) -> None:
    """Publish this model call's reduction, if there was one.

    Compares the full thread in `request.state` (untouched by the edits) with the list being sent.
    Nothing is published unless the difference is positive, so "no reduction needed" never ticks
    and a caller that bypassed state cannot produce a negative reclaim.

    Args:
        request: The model request as it stands after the editing middleware above it.
    """
    thread = request.state.get("messages") or []
    if not thread:
        return
    sent = count_tokens_approximately(request.messages)
    _record_overrun(request, sent)
    reclaimed = count_tokens_approximately(thread) - sent
    if reclaimed <= 0:
        return
    # Tell the repeat guard which calls were cleared: the model no longer holds those answers, so
    # re-calling is a re-read. Named per call id and forgiven at most once per turn, since every
    # model call re-derives the same reduction.
    cleared = _cleared_calls(request.messages)
    forget_calls(cleared)
    # High-water-marked on the turn's watch: one standing reduction is one compaction. Off the
    # request path each call reports itself.
    turn = current_context()
    if turn is None:
        record_metric(lambda m: m.increment("chemclaw_context_compactions_total"))
        record_metric(
            lambda m: m.increment("chemclaw_context_reclaimed_tokens_total", float(reclaimed))
        )
        _announce(request, reclaimed, cleared)
        return
    if turn.peak_reclaimed == 0:
        record_metric(lambda m: m.increment("chemclaw_context_compactions_total"))
    turn.compacted = True
    delta = float(reclaimed) - turn.peak_reclaimed
    if delta > 0:
        record_metric(lambda m: m.increment("chemclaw_context_reclaimed_tokens_total", delta))
        turn.peak_reclaimed = float(reclaimed)
        # Announce on each new high-water mark (`delta > 0`), not only the first reduction, so a
        # later destructive cut is reported; a mere re-derivation has delta 0.
        _announce(request, reclaimed, cleared)


def _record_overrun(request: ModelRequest[Any], sent: int) -> None:
    """Say, once per turn, that a request is going out over the budget anyway.

    The compaction counters only say a reduction happened; this says the policy finished and the
    request is still over — whether nothing was reclaimable or not enough was. The comparison is the
    window edit's own, against `effective_trigger`, which charges the prefix and converts the whole
    budget, so `sent <= trigger` implies the request fits the budget (and a declared window), as
    `tests/test_context_budget.py` sweeps. With a declared window, a tick is a leading indicator of
    a provider context-length failure. Once per turn, since the overrun is re-derived every call.

    Args:
        request: The model request as it stands after the edits above.
        sent: Estimated tokens of the message list actually being sent.
    """
    budget = effective_trigger(settings.agent_context_token_budget)
    if sent <= budget:
        return
    turn = current_context()
    if turn is not None and turn.unreducible:
        return
    if turn is not None:
        turn.unreducible = True
    record_metric(lambda m: m.increment("chemclaw_context_unreducible_total"))
    log_event(
        logger,
        "context.unreducible",
        "sending ~%d estimated tokens against a budget of %d: the context policy could not "
        "reduce this request further",
        sent,
        budget,
        estimated_tokens=sent,
        budget_tokens=budget,
        prefix_tokens=prefix_tokens(),
    )


def _note_billing(request: ModelRequest[Any], response: Any) -> None:
    """Compare what this call was estimated at with what the provider billed for it.

    The one place both numbers exist; feeds `context_budget.note_model_call`. The estimate is the
    whole request (prefix plus sent messages), matching `input_tokens`. Guarded end to end; a
    response without usage teaches nothing.
    """
    try:
        message = response.result[0] if getattr(response, "result", None) else response
        usage = getattr(message, "usage_metadata", None) or {}
        billed = int(usage.get("input_tokens") or 0)
        estimated = prefix_tokens() + int(count_tokens_approximately(request.messages))
        note_model_call(estimated, billed)
    except Exception:
        degraded(
            logger,
            "compaction",
            "could not compare this model call's estimate with its billed size",
        )


def _announce(
    request: ModelRequest[Any], reclaimed: int, cleared: list[tuple[str, str, Any]]
) -> None:
    """Say, once per turn, what this reduction actually removed — and which of the two edits did it.

    The compaction counter cannot tell lossless clearing from destructive window cuts, so a
    structured event names reclaimed tokens, the tools whose results were cleared, and conversation
    groups dropped (the difference in `HumanMessage` counts). Counts and names, never arguments or
    content. Emitted on each new high-water mark, so a later further reduction is reported too.
    """
    tools = sorted({name for _call_id, name, _args in cleared})
    dropped = _group_count(request.state.get("messages") or []) - _group_count(request.messages)
    log_event(
        logger,
        "context.compacted",
        "reclaimed ~%d tokens: cleared %d tool result(s) (%s) and dropped %d conversation group(s)",
        reclaimed,
        len(cleared),
        ", ".join(tools) or "none",
        dropped,
        reclaimed_tokens=reclaimed,
        tool_results_cleared=len(cleared),
        # A comma-joined string rather than a list, because a log stack indexes scalars.
        tools_cleared=", ".join(tools),
        conversation_groups_dropped=dropped,
    )


def _group_count(messages: Sequence[AnyMessage]) -> int:
    """How many conversation groups a message list holds — one per `HumanMessage`."""
    return sum(1 for message in messages if _opens_a_group(message))


def _cleared_calls(messages: Sequence[AnyMessage]) -> list[tuple[str, str, Any]]:
    """`(call id, tool name, arguments)` per result this reduction replaced with a placeholder.

    Detected by the `_CLEARED_MARKER` stamp, not placeholder text. Arguments come from the calling
    `AIMessage` because the repeat guard keys on the call; the id lets each be forgiven once.
    """
    by_id: dict[str, tuple[str, Any]] = {}
    for message in messages:
        for call in getattr(message, "tool_calls", None) or ():
            if call.get("id"):
                by_id[str(call["id"])] = (str(call.get("name", "")), call.get("args"))
    cleared: list[tuple[str, str, Any]] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        if not message.response_metadata.get("context_editing", {}).get("cleared"):
            continue
        call_id = str(message.tool_call_id)
        identity = by_id.get(call_id)
        if identity is not None:
            cleared.append((call_id, *identity))
    return cleared


class RecordContextCompaction(AgentMiddleware[Any, Any, Any]):
    """Count a model call whose context was reduced, then run it.

    Implements both hooks: `create_agent` puts a middleware declaring either into both chains, so a
    missing half breaks synchronous `graph.invoke()` (which tests use) or every real async turn.
    Observation only — it never edits the request.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        """Record the reduction, run the call, then learn what it was billed (sync path)."""
        _record_reduction(request)
        response = handler(request)
        _note_billing(request, response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """Record, run, and learn what it cost — the path a turn actually takes."""
        _record_reduction(request)
        response = await handler(request)
        _note_billing(request, response)
        return response
