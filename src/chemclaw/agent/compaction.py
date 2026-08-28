"""Keep a session's thread inside a token budget — D-025's policy, restored on this engine.

**What was wrong.** D-025 bounded the model's context with two deterministic, LLM-free strategies:
collapse older tool-result payloads first (they are this system's largest context consumers), then
slide the conversation window. Its implementation was `chemclaw_agent._build_compaction`, built out
of the previous framework's strategy classes, and it left with that framework in M13. Nothing
replaced it. What survived the removal was the *appearance* of the policy: three settings with no
reader anywhere in `src/` or `tests/`, a config comment describing a mechanism that no longer ran,
three rows in `.env.example`, and — the part that reached a chemist — a sentence in the system
prompt telling the model "this session's context is compacted to a token budget, so an older turn
can age out of what you currently see". The thread was in fact replayed whole on every turn, and
the failure at the provider's context limit is a hard error rather than a degradation.

This module is that policy again, over the same three settings, with the two halves it always had.

**Upstream owns the expensive half, and reusing it is the decision.** `ClearToolUsesEdit` is
`ToolResultCompactionStrategy` under another name: a token trigger, the newest N tool results kept
verbatim, everything older replaced by a placeholder. Re-writing it here would have been a second
copy of somebody else's tested code, and `langgraph_agent`'s own opening argument — use the
framework's machinery rather than re-implement it — applies with more force to a strategy than it
did to the agent loop. What upstream does *not* ship is a conversation window as a `ContextEdit`,
so that half is first-party (`KeepLastConversationGroupsEdit`) — but only the *edit* is: the cut
itself is `langchain_core`'s `trim_messages`, for the same reason. Re-deriving "which suffix of a
message list fits a token budget without splitting a tool call from its result" is somebody else's
tested code, and the first version of this edit is what re-deriving it costs (see its docstring).

**Nothing here is destructive, and that is a change from D-025.** Both edits run inside
`wrap_model_call`, so they narrow the list *this model call* is sent and leave graph state
untouched; the next turn re-derives the same reduction from the full thread. D-025 also ran its
strategy over the persisted history so the next turn "started smaller", and the commit that removed
the durable half of that named the reason it was wrong: a context heuristic must not edit a record
somebody else's policy governs. The checkpointer is turn state rather than the durable record — that
is `session_messages` — but the same argument applies to it one step down, and a reduction that is
recomputed costs an estimator pass while a reduction that is *applied* costs history. What bounds
the checkpoint tables is age, in `durable/retention.py`, which is the policy statement a deployment
actually makes.

**One thing is lost against D-025 and it is named rather than glossed.** Its
`ToolResultCompactionStrategy` collapsed an older tool result "into a short cited
`[Tool results: …]` trace"; `ClearToolUsesEdit` replaces the whole payload with a flat placeholder,
so the citation goes with it. Keeping the trace would mean this module parsing evidence payloads
for note ids — coupling the context policy to the shape of every tool's result, for a benefit that
only exists above the budget, where the alternative is a hard context-limit failure and the
model's own prose in the thread still carries what it concluded. `exclude_tools` is the escape
hatch if a deployment ever measures that it needs one; it is deliberately empty, because excluding
the evidence sweeps would exclude exactly the results this edit exists to reclaim.
`docs/guides/harness-konzept.md` §9 carries the provenance risk this trades against.

**Why no summarizer**, unchanged from D-025 and worth restating because `SummarizationMiddleware`
is now one import away: a summarizer reads retrieved evidence and writes text that is then replayed
as conversation, so it is an indirect-prompt-injection surface pointed straight at the thread. The
char/4 estimator and two deterministic edits need no credential, no extra model call, and no trust.

**This prohibition is about the thread, and `agent/condense.py` is not a counter-example to it**
(`D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool`). That module does make a
model call over retrieved evidence, and it is named here because a reader arriving at this
paragraph as *the* prohibition would otherwise read it as a contradiction. The two reasons above
are the replay and the envelope, and a tool result is neither: it arrives as a `ToolMessage`, framed
on the way out, crossing every `wrap_tool_call` gate, carrying its citations, and cleared by
`ClearToolUsesEdit` like any other result rather than becoming history. Nothing below changes for
it, and `disabled_summarizer` stays switched off.

**It tells the repeat guard, and that coupling is deliberate.** `agent/repeat_guard.py` refuses a
third identical call on the stated grounds that the model already holds the first answer. Clearing a
tool result is exactly what makes that false, and both modules said so and neither acted: this
module's placeholder lost a "re-run the tool if you still need it" line *because* the guard would
deny it. One call to `forget_calls` at the moment a reduction is known closes it, and it is here
rather than in the guard because this is the only place that can see one happen.

**The metric exists because prose about compaction is what caused this defect.**
`RecordContextCompaction` is the reader that can say the mechanism fired — it compares the full
thread on the request's state against the list the edits actually produced, so the number is
measured downstream of the policy rather than asserted beside it.

**One number was not enough, because the two edits are not the same kind of act.** Clearing a tool
result is lossless and the model can re-fetch; dropping a conversation group is *destructive* and
the model simply no longer sees what was said. "The agent forgot what I told it three turns ago"
and "the agent re-ran a tool it had already run" are those two edits, and one unlabelled counter
answers neither. A label cannot be added to an already-declared counter from this side of
`core/metrics.py`, so the distinction is published as a structured record instead — one
`context.compacted` event per turn, on the high-water reduction, naming the reclaimed tokens, the
tool results cleared *and the tools they belonged to*, and the number of conversation groups
dropped. Counts and names, never content.

**And nothing here may end a turn.** There used to be no `try` in this module at all, so a raising
edit or a `count_tokens_approximately` that choked on an unfamiliar message shape killed the turn as
a generic internal error — losing the answer, the tokens already spent and every tool the turn had
run, in order to save tokens. `GuardedEdit` and `_record_reduction` degrade instead, and the request
proceeds uncompacted, which is the direction with a chance of being answered.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelRequest,
)
from langchain.agents.middleware.context_editing import ContextEdit, TokenCounter
from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages

from chemclaw.agent.context_budget import (
    MeasureRequestPrefix,
    current_context,
    effective_trigger,
    note_model_call,
    prefix_tokens,
)
from chemclaw.agent.repeat_guard import forget_calls
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import degraded, record_metric

logger = logging.getLogger(__name__)

# What a cleared tool result leaves behind. Not upstream's bare "[cleared]": the model is being
# shown a message it can see it once received, and a placeholder that does not say why reads as a
# tool that returned nothing — a different fact, and one the model would reasonably act on by
# calling the tool again.
#
# **It states the fact and gives no instruction, and that is now for one reason where it used to be
# two.** An earlier version ended "Re-run the tool if you still need it". That contradicted
# `agent/repeat_guard.py` — a cleared result could be re-fetched, cleared and re-fetched, and the
# third identical call in a turn was *refused*, so the placeholder told the model to do what a guard
# three middlewares away would then deny. **That half is fixed**: a reduction now clears the repeat
# counters (`_record_reduction` calls `forget_calls`), because after a clearing an identical call is
# a re-read rather than a repeat. What stands is the cost: this string is repeated once per cleared
# result, tens of times, in exactly the situation where the budget is already spent. The guidance is
# paid for once instead, in the system prompt (`chemclaw_agent`), where a sentence costs one copy
# rather than twenty.
TOOL_RESULT_PLACEHOLDER = (
    "[Earlier tool result dropped to stay inside this session's context budget.]"
)


@dataclass(slots=True)
class ClearOlderToolResultsEdit(ContextEdit):
    """Upstream's tool-result clearing, with the two bounds it does not have.

    `ClearToolUsesEdit` is the right strategy and is reused unchanged; what is wrong is what it is
    handed. Two defects, both measured, and both fixed by computing the arguments per apply instead
    of once at construction.

    **It cleared results the model had never seen.** Upstream's `keep` counts the newest tool
    *results*, not the newest tool-call *step* — a distinction this repository had written down and
    not acted on. The edit runs in `wrap_model_call`, so the list it reduces already holds the
    results that came back in the step immediately before; with `agent_keep_last_tool_groups` at 2
    and `agent_max_parallel_tool_calls` at **8**, a fan-out wider than two lost its own newest
    results to a placeholder before the model's first look at them. Measured over the shipped
    settings on a five-way fan-out past the trigger: three of the five replaced, each with a
    placeholder beginning "Earlier tool result" about a result that was not earlier. What reaches
    the chemist is not a slow turn — it is an answer that quietly rests on two of five values.

    The two settings are a hundred lines apart in `core/config/agent.py` and contradicted each
    other. `keep` is now the larger of the configured floor and the number of results belonging to
    the newest batch, so the batch survives **structurally** rather than because a number happened
    to be big enough.

    **And it cleared everything when it needed to clear something.** `clear_at_least` defaults to
    0, which never breaks upstream's loop, so crossing the trigger by one token wiped every result
    but the newest `keep`: measured, 18 of 20 results on a thread that needed roughly half of that,
    an 88% cut. Every one of those is re-fetchable, which is what makes the edit lossless — and
    also what makes over-clearing expensive, because a re-fetch costs a model call, the tool again,
    and (`repeat_guard.forget_calls`) a forgiveness that lets the same call be cleared again.
    Passing the overshoot as `clear_at_least` stops the loop at the trigger.

    A wrapper rather than a subclass: upstream's `apply` is the whole strategy and none of it is
    being changed, only its arguments. `tests/test_upstream_surface.py` holds the constructor
    keywords this depends on.
    """

    trigger: int
    """Billed tokens above which older tool results are cleared (`effective_trigger` converts)."""

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
        ClearToolUsesEdit(
            trigger=budget,
            keep=max(self.keep, newest_batch_size(messages)),
            # The overshoot, so upstream stops as soon as the thread is back under the trigger
            # rather than continuing to the end of its candidate list.
            clear_at_least=tokens - budget,
            placeholder=self.placeholder,
        ).apply(messages, count_tokens=count_tokens)


def newest_batch_size(messages: Sequence[AnyMessage]) -> int:
    """How many results in `messages` answer the newest assistant message that called tools.

    The number `keep` has to cover for a fan-out to survive its own model call. It is a count
    rather than a set of ids because that is what upstream's `keep` takes: it preserves the last N
    `ToolMessage`s in list order, and a batch's results are exactly the trailing ones — `ToolNode`
    appends every result of a step after the `AIMessage` that requested it.

    0 when no assistant message in the list called a tool at all — a prose conversation, where
    there is no batch to protect and the configured floor stands unchanged.
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

    D-025's second strategy, and the only half of the policy upstream does not ship. It is what
    makes the thread *bounded* rather than merely cheaper: clearing tool results reclaims nothing
    from a conversation that called no tools, so a long enough exchange of prose would grow past any
    budget with the first edit doing its job perfectly.

    **The cut is by tokens, and it used to be by group count — which did not bound anything.** The
    first version triggered on tokens and then dropped everything before the newest `keep` groups,
    which reduces but does not bound: how much it reclaims depends entirely on how large those
    `keep` groups happen to be, and it returned without cutting at all whenever the thread had no
    more groups than `keep`. Measured at the shipped defaults over the 20 tool-free prose groups
    `tests/test_compaction.py` builds: 300,300 tokens in, **180,180 out, against a 100,000
    budget** — the edit ran, logged, dropped eight groups, and left the request 80% over. The budget
    is now `trim_messages` (`strategy="last"`), so what survives is what fits.

    **`keep` survives as a floor rather than the rule.** `agent_keep_last_conversation_groups` is an
    ENV-visible knob and renaming or dropping it costs every deployment that sets it, for a word;
    the same argument this module already makes for `agent_keep_last_tool_groups` at
    `context_compaction_middleware`. So the window always drops everything older than the newest
    `keep` groups, and the budget may drop more — `max(by_tokens, by_groups)`.

    **A group is a human message and everything that answers it**, which is what keeps this safe.
    A tool call and its result are always emitted between two human messages, so a cut taken at a
    group boundary can never separate them — the pairing rule `agent/message_pairing.py` states is
    preserved structurally here rather than checked for afterwards.

    **`start_on="human"` is what buys the boundary from `trim_messages`, and it is load-bearing.**
    `trim_messages` has no pairing logic of its own — `start_on` becomes an `end_on` on a reversed
    first-fit pass, i.e. "drop from the front until the first kept message is of this type", and
    nothing else. Measured without it over a 4-message-per-group thread, sweeping 565 budgets: 24
    of them left a leading `ToolMessage` whose `tool_use` had just been dropped — a `tool_result`
    with no call, which a provider rejects exactly as it rejects the reverse. Suffix trimming makes
    the *call*-side orphan impossible, so this argument is only about the result side; the sweep in
    `tests/test_compaction.py` pins both, with that module's own `calls_without_adjacent_results`
    and with an assertion that the survivors start at a `HumanMessage`.

    **The one thing it will not do is empty the list**, and that is a clamp rather than an
    aspiration. Below the size of the newest group `trim_messages` returns `[]`, and
    `ContextEditingMiddleware` checks for an empty message list only *before* running its edits — so
    an emptied list goes to the provider, which rejects it. The cut never passes `starts[-1]`, which
    means a single group larger than the whole budget is sent over budget rather than not sent. That
    is the honest failure and it is the tool-result edit's case, not this one's.

    Ordered after the tool-result edit in `context_compaction_middleware`, so it sees a list already
    as small as the cheap move can make it and drops conversation only when that was not enough.
    D-025 called this reclaiming cheapest-first; the ordering is the whole of it.
    """

    trigger: int = 100_000
    """Billed tokens above which the window is applied — and the budget it cuts back to.

    Converted to the estimator's unit by `effective_trigger`, and clamped there by a declared
    context window; it is not compared to `count_tokens` directly.
    """

    keep: int = 12
    """Floor on the cut: groups older than the newest `keep` always go, whatever the budget says."""

    def apply(self, messages: list[AnyMessage], *, count_tokens: TokenCounter) -> None:
        """Cut `messages` in place back to the effective budget, when over it.

        In place because that is the `ContextEdit` protocol: `ContextEditingMiddleware` deep-copies
        the request's list once and hands the same list to each edit in turn, so returning a new one
        would silently discard this edit's work. Hence an index is computed and `del` applied,
        rather than the list `trim_messages` returns being handed back.

        **`self.trigger` is a budget in billed tokens and `count_tokens` counts estimated ones**, so
        the two are reconciled by `effective_trigger` rather than compared directly — which is what
        they used to be. That function also subtracts this request's own prefix from a declared
        context window, so the number below is what the *model* has room for rather than what the
        configuration hoped it had (`agent/context_budget.py`).
        """
        budget = effective_trigger(self.trigger)
        if count_tokens(messages) <= budget:
            return
        starts = [
            index for index, message in enumerate(messages) if isinstance(message, HumanMessage)
        ]
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
        # The floor, guarded at both ends because a directly-constructed edit can reach values the
        # config cannot (`agent_keep_last_conversation_groups` is `ge=1`): `keep == 0` would index
        # `starts[0]` while meaning the opposite, and `keep` above the group count would raise
        # `IndexError` inside a middleware, which is a failed turn. Both degrade to "no floor".
        by_groups = starts[-self.keep] if 0 < self.keep <= len(starts) else starts[0]
        # The newest group is the floor on what can be kept, per the clamp in the class docstring.
        cut = min(max(by_tokens, by_groups), starts[-1])
        if cut <= 0:
            return
        # DEBUG, not INFO, and the demotion is the point. Both edits are non-destructive, so this
        # runs on *every* model call of a turn and re-derives the same standing cut — a 30-step turn
        # logged one reduction thirty times at INFO. The turn-level record an operator actually
        # reads is `_record_reduction`'s single `context.compacted` event, emitted once per turn on
        # the high-water mark. This line stays because it is the only place the *window*'s own
        # arithmetic is visible when someone is debugging the cut itself.
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

    **This exists because the declination above stopped being free.** While the middleware list was
    hand-assembled, "no summarizer" was expressed by not importing one. `create_deep_agent` composes
    a `SummarizationMiddleware` unconditionally, so the same decision now has to be *made* rather
    than merely not unmade — and an argument this repository has held since D-025 must not be
    reversed by an upstream default nobody chose.

    The argument is unchanged and the deepagents variant does not escape it. That variant genuinely
    answers half of it: evicted messages are written to a path the summary embeds, so the evidence
    stays readable instead of being dropped, and `state["messages"]` is left intact. What it cannot
    answer is the other half. `agent/framing.py` wraps untrusted tool output in an envelope that
    marks it as untrusted, and a summary is *new prose written by the model over that content* —
    the envelope does not survive it. So retrieved text that arrived flagged as external comes back
    as unflagged narration, and is then re-read on every subsequent turn. That is an
    indirect-prompt-injection surface pointed straight at the thread, and it is the one thing the
    two deterministic edits below cannot become.

    **`trigger=None` is upstream's own off state, not a large number standing in for one.**
    `_should_summarize` opens with `if not self._trigger_clauses: return False`, so no clause means
    it never fires — asserted in `tests/test_compaction.py` rather than read off that source once.
    Constructed here and passed through `middleware=` so `_apply_custom_middleware` swaps it into
    upstream's slot by name, which is the same mechanism `FilesystemMiddleware` uses and for the
    same reason: the alternative, `HarnessProfile.excluded_middleware`, is resolved by the model's
    self-reported provider and is silently skipped on a key miss.

    Args:
        model: The turn's resolved chat model. Required by the constructor and never called, since
            the only thing that would call it is the summarization this disables.
        backend: The turn's backend. Same: held for the offload path that cannot run.

    Returns:
        The middleware to hand `create_deep_agent(middleware=…)` in upstream's slot.
    """
    from langchain.agents.middleware.summarization import SummarizationMiddleware

    return SummarizationMiddleware(model=model, backend=backend, trigger=None)


@dataclass(slots=True)
class GuardedEdit(ContextEdit):
    """One context edit, wrapped so that a failure inside it costs the reduction and not the turn.

    **There was no `try` anywhere in this module**, and the failure mode that leaves is the wrong
    way round: a raising edit — a bad slice, a `count_tokens` that chokes on a message shape nobody
    anticipated, an upstream change to `response_metadata` — propagates out of
    `ContextEditingMiddleware.wrap_model_call` and ends the turn as a generic internal error. The
    chemist loses the answer, the tokens already spent and every tool the turn had run, to save
    tokens.

    Continuing **uncompacted** is the safe direction and not merely the lenient one. The request is
    then whatever it was: possibly over the provider's limit, in which case the call fails with the
    provider's own context-length error — which is now classified, counted and *told to the
    chemist as such* (`agent/model_calls.py`). More often it is under the limit and simply
    expensive, because both triggers sit well below the hard ceiling. So the degraded path has a
    good chance of answering, and the failed path has none.

    Wrapping both edits rather than only the first-party one: `ClearToolUsesEdit` is upstream's and
    is called with this repository's placeholder, triggers and `keep`, so it is exactly as capable
    of raising on an unexpected message shape. Two real callers, which is what makes this a wrapper
    rather than an inlined `try`.
    """

    edit: ContextEdit
    """The edit to run; its failures are absorbed, its work is not otherwise touched."""

    def apply(self, messages: list[AnyMessage], *, count_tokens: TokenCounter) -> None:
        """Run the edit, or record that it could not run and leave `messages` as they are.

        In-place mutation is the `ContextEdit` protocol, so an edit that raised half-way through
        may leave a partially reduced list. That is accepted rather than rolled back: every
        reduction here is a *suffix* operation on a list the middleware already deep-copied, so a
        partial result is a smaller valid thread rather than a corrupt one, and copying the list a
        second time to protect against a case that has never happened is a per-model-call cost.
        """
        try:
            self.edit.apply(messages, count_tokens=count_tokens)
        except Exception:
            degraded(
                logger,
                "compaction",
                "the %s context edit failed; this model call proceeds uncompacted",
                type(self.edit).__name__,
            )


def context_compaction_middleware() -> list[Any]:
    """The context policy, as the middleware list `build_langgraph_agent` splices in.

    Two entries rather than one because they answer different questions and only one of them is the
    policy: the editing middleware reduces, and `RecordContextCompaction` observes what the
    reduction actually did. The observer sits *inside* the editor — later in the list is nested
    deeper — because it reads the edited list off its own request and the full thread off that
    request's state, and only the innermost position sees both.

    Returned as a list so the caller splices rather than composes, matching the three other
    middleware groups `build_langgraph_agent` already splices.
    """
    return [
        # Outermost, because everything below budgets against a prefix only a middleware can see.
        MeasureRequestPrefix(),
        ContextEditingMiddleware(
            edits=[
                # Both wrapped, so a raising edit costs the reduction rather than the turn — see
                # `GuardedEdit`. The wrapper is applied here rather than inside each edit because
                # one of the two is upstream's.
                # Upstream counts the newest *tool results*, where D-025's setting counts tool-call
                # *groups*. The difference is real and the setting's name is the half that is wrong;
                # renaming an ENV-visible knob to fix a name would cost every deployment that sets
                # it, so the name stays and `core/config/agent.py` says what it now means — and
                # `ClearOlderToolResultsEdit` is what stops the difference costing a chemist an
                # answer, by raising `keep` to cover the newest batch.
                GuardedEdit(
                    ClearOlderToolResultsEdit(
                        # Its own trigger, well below the budget the window uses. The two edits
                        # are different instruments: this one is lossless — the `tool_use` record
                        # survives and the model can re-fetch — so it is cheap enough to run
                        # early, and every token it reclaims early is a conversation group the
                        # window below never has to delete. Sharing one threshold meant nothing
                        # reduced until 100k and then both fired together, which is the expensive
                        # edit doing work the free one could have done.
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

    The guard is the whole of this function, and it is separate from `GuardedEdit` because it
    covers a different failure: this reads `request.state`, estimates two message lists with
    `count_tokens_approximately`, and inspects `response_metadata` for upstream's cleared-marker —
    all over shapes this module does not own. A raising *observer* ending a turn would be the worst
    possible trade, since removing it entirely changes nothing a chemist receives.

    The repeat guard's `forget_calls` is inside the guard too, deliberately. Losing it means a
    cleared result may earn a refusal it should have been forgiven — one refused tool call, worded
    and recoverable — where letting the exception out means the turn dies.
    """
    try:
        _publish_reduction(request)
    except Exception:
        degraded(
            logger,
            "compaction",
            "could not measure this model call's context reduction; the call itself is unaffected",
        )


def _publish_reduction(request: ModelRequest[Any]) -> None:
    """Publish this model call's reduction, if there was one.

    The full thread is read off `request.state`, which the edits above leave alone — `override`
    replaces the request's message list and copies everything else through — so this compares what
    the session holds against what the model is being sent, which is the operator's question.

    **Nothing is published when the difference is not positive**, and that one guard carries two
    cases. A call that needed no reduction must not tick, or "compaction did not need to fire" and
    "compaction is not wired" become indistinguishable — which is the failure this module exists to
    correct. And a caller that invoked the graph with messages it did not also put in state would
    otherwise produce a negative reclaim, which a counter cannot take.

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
    # The one place the reduction is *known*, so the one place that can tell the repeat guard its
    # premise has expired. A cleared tool result leaves the model without the answer the guard
    # assumes it is holding, and the third identical call was then refused with advice — "answer
    # from what you already have" — about something it no longer had. See `repeat_guard`.
    #
    # The calls are named rather than the counters wiped: clearing keeps the newest results, so a
    # blanket reset forgave repeats the model can still read. And each is named by its *call id*,
    # forgiven at most once per turn: the edits are non-destructive, so this observer re-derives
    # the same standing reduction on every model call, and forgiving it every time reset the
    # guard's counters as fast as repeats accumulated (`repeat_guard.TurnCallWatch`).
    cleared = _cleared_calls(request.messages)
    forget_calls(cleared)
    # The metrics are high-water-marked on the turn's watch for the same re-derivation reason: a
    # 30-step turn with one standing reduction is one compaction, not thirty, and a reclaimed
    # token is reclaimed once. Off the request path there is no watch and each call reports
    # itself, which is what a single-shot `graph.invoke` in a test observes.
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
        _announce(request, reclaimed, cleared)
    turn.compacted = True
    delta = float(reclaimed) - turn.peak_reclaimed
    if delta > 0:
        record_metric(lambda m: m.increment("chemclaw_context_reclaimed_tokens_total", delta))
        turn.peak_reclaimed = float(reclaimed)


def _record_overrun(request: ModelRequest[Any], sent: int) -> None:
    """Say, once per turn, that a request is going out over the budget anyway.

    **This is the reading the two compaction counters could not express**, and the claim beside
    them in `core/metrics.py` was wrong because of it: a flat zero was documented as "never over
    budget" and in fact meant "never *reduced*". Measured through a compiled graph — one human
    message and two 200,000-character tool results — the thread was 100,081 estimated tokens
    (~224,000 billed), both edits ran, and both counters moved by zero. `ClearToolUsesEdit` had
    exactly `keep` candidates so it cleared nothing; the window edit cannot cut past the newest
    group so it dropped nothing. The single turn about to fail at the provider's context limit was
    indistinguishable from a quiet one.

    The comparison is the window edit's own: the thread being sent, against the budget that edit
    was given. So this fires when the policy has finished and the request is still over — whether
    it reclaimed nothing at all or reclaimed plenty and could not reach the line. Both are the same
    fact for an operator, and it is the only leading indicator this system has for a context-length
    failure.

    Once per turn, for the same reason every other number here is high-water marked: the edits are
    non-destructive, so a standing overrun is re-derived on every model call of the turn.

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

    **The one place both numbers exist.** The estimate is computed here to decide whether a
    reduction happened; the bill arrives on the response of the very call this middleware wraps.
    Nothing compared them, so the budget stayed denominated in a unit measured to be 2.2x off on
    the payload class it governs (`agent/context_budget.py` carries the measurement).

    The estimate is the *whole* request — the ambient prefix plus the messages actually sent —
    because `input_tokens` counts the whole request and half a comparison is not one.

    Guarded end to end: this is an observation, and an observation that ended a turn would be the
    worst trade in this module. A response shape that carries no usage simply teaches nothing.
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

    **One counter cannot answer either question a compaction raises.**
    `chemclaw_context_compactions_total` is unlabelled and the middleware composes two edits with
    opposite consequences: `ClearToolUsesEdit` is *lossless* — the `tool_use` record survives and
    the model can re-fetch — while `KeepLastConversationGroupsEdit` is **destructive**, deleting
    conversation turns from what the model can see. "The agent forgot what I told it three turns
    ago" and "the agent re-ran a tool it had already run" are precisely those two edits, and one
    number distinguishes them not at all. A label cannot be added to a declared counter from here,
    so the distinction lands as a structured record instead, where both halves are separate fields.

    **Counts and names, never content.** The tools whose results were cleared are named because
    "which tool's answer did the model lose" is the actionable half of the second question; their
    arguments and their payloads are not, and `_cleared_calls` returns both. A log line is not a
    place to re-publish a chemist's question or a corpus excerpt.

    Groups dropped is derived here rather than reported by the edit, because only this function sees
    both lists: a group is a `HumanMessage` and everything answering it, and the window only ever
    removes a prefix, so the difference in `HumanMessage` counts *is* the number of conversation
    turns the model no longer sees. Zero means the destructive edit did not fire — the reduction was
    entirely the lossless one, which is the good case and worth being able to see.

    Once per turn, on the high-water compaction, for the reason the metrics are high-water-marked:
    the edits are non-destructive, so this same standing reduction is re-derived on every model
    call of the turn and a per-call line would repeat it thirty times.
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
    """How many conversation groups a message list holds — one per `HumanMessage`.

    The same definition `KeepLastConversationGroupsEdit` cuts on, stated once: a group is a human
    message and everything that answers it, which is why a cut on that boundary can never separate
    a tool call from its result.
    """
    return sum(1 for message in messages if isinstance(message, HumanMessage))


def _cleared_calls(messages: Sequence[AnyMessage]) -> list[tuple[str, str, Any]]:
    """`(call id, tool name, arguments)` per result this reduction replaced with a placeholder.

    Upstream stamps `response_metadata["context_editing"]["cleared"]` on a `ToolMessage` it clears,
    which is the only reliable marker — the placeholder text is first-party and a content match
    would break the moment it is reworded. The arguments come from the originating `AIMessage`'s
    `tool_calls`, because the guard's identity is the call, not the tool — and the call *id* rides
    along because it is what lets the guard forgive each cleared result exactly once across the
    many model calls that will all re-derive this same standing reduction.

    The metadata key is pinned in `tests/test_upstream_surface.py`.
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

    **Both hooks, not one, and that is the whole reason this is a class rather than a decorated
    function.** LangChain's `AgentMiddleware` base raises `NotImplementedError` for whichever half a
    middleware leaves undeclared, and `create_agent` puts a middleware that declares *either* hook
    into *both* chains — so a `@wrap_model_call` async function makes every synchronous
    `graph.invoke()`/`stream()` fail, and a sync one fails every real turn. Measured: with only the
    async half, `build_langgraph_agent(model=fake).invoke(...)` raised "Synchronous implementation
    of wrap_model_call is not available", while the same graph without this middleware answered.
    The reachable caller is a synchronous `graph.invoke()` on a turn that calls no tool — what
    `tests/test_compaction.py` drives, and the reason the sync half must stay. It is **not** the
    sync `func` deepagents' `task` tool carries beside its coroutine, which is what this sentence
    used to name: every tool-call middleware attached here is async-only (`@wrap_tool_call` over an
    `async def` produces no sync half, and the base class raises for it), so a helper reached that
    way dies at its first tool call. The sync tool path is unsupported by design — the governance
    chain is async — and a reader deciding what to do about the sync question should start from
    that, not from a caller that cannot reach it.

    That matters more now than when it was written: `agent/subagents.py` puts a helper behind `task`
    on every agent, so the sync `func` is present on a real tool rather than a hypothetical one. It
    is still unreachable for the same reason, and still not what keeps this half alive.
    `ContextEditingMiddleware` above declares both for the same reason; an observer that narrowed
    the engine its editor runs on would be reporting on a policy it had just disabled.

    Observation only — it never edits the request, so removing it changes what an operator can see
    and nothing a chemist gets. That separation is deliberate: the defect this module fixes was a
    policy everybody believed was running, and a policy whose own middleware reports on itself can
    be believed for a reason.
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
