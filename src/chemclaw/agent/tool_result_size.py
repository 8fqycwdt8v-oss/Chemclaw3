"""Bound what one model call's tool results may put in front of the model.

Nothing else caps a result's size, and the context edits cannot reclaim it: `ClearToolUsesEdit`
keeps the whole newest batch verbatim and the conversation window never cuts past it. So
`agent_max_tool_result_chars` is divided evenly across the batch (a lone call gets the whole
ceiling), so what the model reads does not depend on which tool returned first. Per-tool ceilings
stay; this is the floor under all of them, applied where every result passes.

Order: inside `frame_connector_results` (the envelope wraps an already-bounded payload) and outside
the governance chain (the audit records what the tool returned, not what the model saw).

A cut keeps head and tail — a procedure states its outcome at the end — and replaces the middle with
a notice ending in `SYSTEM_SPEECH_MARK`, which a connector cannot forge. The full text of every
successful result, cut or not, goes to the turn's `FullResultSink` and its ref is stamped on
`response_metadata` (`FULL_RESULT_REF_KEY` for a cut, `RESULT_REF_KEY` for the handle line), so the
chemist keeps what the model lost. No tool fetches it back: that would undo the cut.
"""

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from chemclaw.agent.audit import metric_tool_name
from chemclaw.agent.framing import SYSTEM_SPEECH_MARK
from chemclaw.agent.tool_result_shape import rewritten_command_files, rewritten_tool_messages
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.model_prose import ModelProse

logger = logging.getLogger(__name__)

#: How much of the budget goes to the head; the rest is the tail. More than half because a result's
#: identifying material (query, columns, first rows) is at the front.
_HEAD_SHARE = 3 / 5

#: What a model can do about a cut *it caused by asking*, which is every tool result and a `task`
#: report: it chose the call, so it can choose a narrower one.
TOOL_REMEDY = ModelProse(
    "narrow the question (a filter, a smaller limit, one identifier) to see the part you need"
)

#: The remedy for a cut in a template `agent` step's prompt: the text came from a step reference the
#: model did not write, so there is no question to narrow — it can only say so in its answer.
STEP_REMEDY = ModelProse(
    "this step's template interpolated it, so there is no question to narrow — answer from what "
    "is here and say in your answer that part of the input was not shown"
)


def _notice(
    tool: str,
    removed: int,
    total: int,
    mark: str = SYSTEM_SPEECH_MARK,
    remedy: str = TOOL_REMEDY,
) -> str:
    """The sentence that replaces the middle, addressed to the model.

    States the arithmetic so the model can say how much it did not see, and names the remedy. Ends
    in `SYSTEM_SPEECH_MARK` so the claim of system provenance cannot be forged by a connector.
    Inside a framed connector result the outer framing defangs the mark, which is consistent: there
    the whole span is data. `remedy` varies because narrowing the question is wrong advice for a
    template step, whose prompt the model did not write.
    """
    return (
        f"\n\n[{removed:,} of {total:,} characters removed from the middle of this "
        f"{tool} result to stay inside this session's context budget. This is written by the "
        f"system, not by the tool. The result was not empty and was not an error — {remedy}.] "
        f"{mark}\n\n"
    )


def _brief_notice(removed: int, mark: str = SYSTEM_SPEECH_MARK) -> str:
    """The shortest honest form of the notice, for a share too small to hold the full one.

    Without it each result floors at the full notice's length and a wide batch's total grows with
    its width. Keeps only what the mark cannot carry: that something was removed, and how much. It
    is never itself cut, so `tests/test_tool_result_size.py` derives the width above which a batch
    can exceed the ceiling from this function.

    Args:
        removed: How many characters were removed.
        mark: The provenance anchor to end on. Only `agent/tool_framing.py`'s connector branch
            overrides it, because its notice lands inside an envelope.
    """
    return f"[{removed:,} chars cut] {mark}"


def _spans(content: Any) -> list[str]:
    """Every span of text in a `ToolMessage.content`, in order.

    `content` is a string (in-process tool) or a list of blocks (MCP tool). A block with no `text`
    (an image, an embedded resource) contributes no span and cannot be shortened.
    """
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [_block_text(block) for block in content]
    return []


def _block_text(block: Any) -> str:
    """One content block's text span, or `""` for a block that carries none."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict) and isinstance(block.get("text"), str):
        return str(block["text"])
    return ""


def _carrier(content: Any, spans: list[str]) -> int:
    """The first block index whose text `_rebuilt` will actually keep.

    The notice must land on such a block, or an image-first result loses it and the cut is silent.
    Falls back to 0 when no block can carry text, where `_rebuilt` leaves the content untouched.
    """
    if isinstance(content, str):
        return 0
    for index, _ in enumerate(spans):
        block = content[index] if isinstance(content, list) and index < len(content) else None
        carries_text = isinstance(block, dict) and isinstance(block.get("text"), str)
        if isinstance(block, str) or carries_text:
            return index
    return 0


def _kept(spans: list[str], limit: int, notice: str, carrier: int = 0) -> list[str]:
    """Per-span replacement text: a prefix from the front, a suffix from the back, nothing between.

    Positional, so a multi-block result keeps its blocks. A span outside both budgets becomes `""`
    and is dropped by the caller; the notice rides on the last surviving head span.

    Args:
        spans: The result's text spans, in order.
        limit: Total characters the result may keep, notice excluded.
        notice: The sentence explaining the cut.
        carrier: The earliest index whose text survives `_rebuilt`; the notice never lands before
            it.

    Returns:
        One replacement per input span, same length and same order.
    """
    head_budget = int(limit * _HEAD_SHARE)
    tail_budget = limit - head_budget
    heads = [""] * len(spans)
    tails = [""] * len(spans)
    last_head = -1
    for index, span in enumerate(spans):
        if head_budget <= 0:
            break
        heads[index] = span[:head_budget]
        head_budget -= len(heads[index])
        last_head = index
    # Down to and including `last_head`: on a single-span result both walks share that string, and
    # stopping above it would keep a head and no tail.
    for index in range(len(spans) - 1, max(last_head - 1, -1), -1):
        if tail_budget <= 0:
            break
        # Where both walks meet in one span, the tail draws only from what the head did not take.
        available = spans[index][len(heads[index]) :]
        tails[index] = available[-tail_budget:] if tail_budget < len(available) else available
        tail_budget -= len(tails[index])
    # A head budget of 0 leaves `last_head` at -1, so clamp it or the notice is dropped and the cut
    # is silent. Then move to the first block that survives `_rebuilt`, which on an image-first
    # result is not where the budget ran out.
    at = max(last_head, carrier)
    return [
        heads[index] + (notice if index == at else "") + tails[index] for index in range(len(spans))
    ] or [notice]


def _rebuilt(content: Any, kept: list[str]) -> Any:
    """`content` with each text span replaced, dropping the blocks that kept nothing."""
    if isinstance(content, str):
        return kept[0] if kept else content
    rebuilt: list[Any] = []
    for block, text in zip(content, kept, strict=True):
        if isinstance(block, str):
            if text:
                rebuilt.append(text)
            continue
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            if text:
                rebuilt.append({**block, "text": text})
            continue
        # No text span (an image or embedded resource): carried through untouched.
        rebuilt.append(block)
    return rebuilt


def bounded_content(
    content: Any,
    tool: str,
    limit: int,
    *,
    mark: str = SYSTEM_SPEECH_MARK,
    remedy: str = TOOL_REMEDY,
    charged_total: int | None = None,
    expanded_from: int | None = None,
) -> tuple[Any, int]:
    """`content` cut to `limit` characters of text, and how many characters that removed.

    The notice is charged against `limit`, so the result never exceeds it; its length is taken at
    its widest form. A result shorter than the notice is left alone: a cut is never silent and a
    bound never grows what it bounds.

    This runs twice on a connector result (`frame_connector_results` re-bounds after escaping), so
    the notice's numbers must describe the tool, not the intermediate. `charged_total` is the tool's
    real output size; `expanded_from` is the size before the caller expanded the text, used to
    convert the kept span back into the tool's units in proportion. The conversion is an unbiased
    estimate, bounded by the tool's total, not an exact count. `None` for either means a single
    pass.

    Returns:
        The bounded content and the number of characters removed from `content` (0 when nothing
        was); the notice's sentence is about `charged_total` when one is given.
    """
    spans = _spans(content)
    total = sum(len(span) for span in spans)
    if limit <= 0 or total <= limit:
        return content, 0
    # The notice describes the tool's output; the cut is made against the text in hand.
    charged = total if charged_total is None else charged_total
    # The pre-expansion size of the text in hand; `total` makes every conversion the identity.
    source = total if expanded_from is None else expanded_from
    widest = len(_notice(tool, charged, charged, mark, remedy))
    carrier = _carrier(content, spans)
    if limit < widest:
        # The share is smaller than the full notice, so use the brief form. It is not itself cut —
        # the one case this returns more than `limit` — because cutting it would make the cut
        # silent; the overshoot only matters at batch widths far beyond any real turn. Nothing of
        # the tool's output survives here.
        brief = _brief_notice(charged, mark)
        if total <= len(brief):
            return content, 0
        return _rebuilt(content, _kept(spans, 0, brief, carrier)), total
    # Past the branch above, `limit >= widest` and `total > limit`, so `total > widest` holds.
    kept = max(limit - widest, 0)
    removed = total - kept
    # What the model can still see, in the tool's own units. Never above `charged`, since
    # `kept <= total` and `source <= charged`.
    visible = kept * source // total if total else 0
    notice = _notice(tool, charged - min(visible, charged), charged, mark, remedy)
    return _rebuilt(content, _kept(spans, kept, notice, carrier)), removed


#: Where the inner bound records the tool's real output size, for the outer bound to charge.
#:
#: On `response_metadata` because it travels with the message through `model_copy` and the model
#: does not read it; a contextvar would be wrong when a batch is bounded concurrently.
ORIGINAL_CHARS_KEY = "chemclaw_original_chars"


def text_chars(content: Any) -> int:
    """How many characters of text a `ToolMessage.content` holds, across every span of it.

    Public for callers that expand a result and must report the size before expanding.
    """
    return sum(len(span) for span in _spans(content))


def original_chars(message: Any) -> int | None:
    """The tool's real output size, if an earlier bound in this chain recorded one.

    Returns:
        The character count the first pass saw, or `None` when nothing has bounded this result yet.
    """
    stamped = (getattr(message, "response_metadata", None) or {}).get(ORIGINAL_CHARS_KEY)
    return stamped if isinstance(stamped, int) else None


#: Where a cut result names the full text it was cut from: present iff the model was shown a cut,
#: valued with the `FullResultSink` ref or `""` when nothing was stored. Presence and value are
#: separate facts on purpose. On `response_metadata`, like `ORIGINAL_CHARS_KEY`, so the thread
#: carries a pointer, never the text.
FULL_RESULT_REF_KEY = "chemclaw_full_result_ref"

#: `(tool, full_text) -> ref`, answering `""` when it stored nothing and never raising. Mirrors
#: `api/tool_results.ResultSink` without importing it (`agent` may not import `api`).
FullResultSink = Callable[[str, str], Awaitable[str]]

# Ambient, because the middleware is built once per profile and cached, so a sink captured at build
# time would file one turn's results under another's session. Only `api/runner` sets it; elsewhere a
# cut stamps `""` and stores nothing.
_full_results: ContextVar[FullResultSink | None] = ContextVar(
    "chemclaw_full_result_sink", default=None
)


def set_full_result_sink(sink: FullResultSink) -> Token[FullResultSink | None]:
    """Make `sink` where this turn's cut results keep their full text; returns the reset token."""
    return _full_results.set(sink)


def reset_full_result_sink(token: Token[FullResultSink | None]) -> None:
    """Undo `set_full_result_sink`, so the next turn on this worker starts with no sink."""
    _full_results.reset(token)


def was_cut(message: Any) -> bool:
    """Whether the model was shown a cut of this result rather than all of it."""
    return FULL_RESULT_REF_KEY in (getattr(message, "response_metadata", None) or {})


def full_result_ref(message: Any) -> str:
    """The ref of the full text this result was cut from, or `""` when there is none to fetch."""
    ref = (getattr(message, "response_metadata", None) or {}).get(FULL_RESULT_REF_KEY)
    return ref if isinstance(ref, str) else ""


def full_text(content: Any) -> str:
    """The text a `ToolMessage.content` holds, every span joined — what the full-result store keeps.

    The same walk as `_spans`, so what is kept is exactly what was cut from.
    """
    return "".join(_spans(content))


#: Where a result names the stored full text its handle line points at: present on every result the
#: turn's `FullResultSink` stored, cut or not. `stamp_result_handles` appends `⟨r:<12 hex>⟩` from
#: it, and the stream names it as the result's `result_ref`. Distinct from `FULL_RESULT_REF_KEY`,
#: which says the model saw a cut; for a cut result kept whole the two hold the same ref.
RESULT_REF_KEY = "chemclaw_result_ref"


def stored_result_ref(message: Any) -> str:
    """The ref of this result's stored full text, or `""` when nothing stored it."""
    ref = (getattr(message, "response_metadata", None) or {}).get(RESULT_REF_KEY)
    return ref if isinstance(ref, str) else ""


def is_referable(message: Any) -> bool:
    """Whether a result is one a later binding may point at: a success that carried some text.

    A failure is a statement about the call, not evidence, and an empty result has nothing to point
    at.
    """
    return (
        isinstance(message, ToolMessage)
        and message.status != "error"
        and bool(full_text(message.content).strip())
    )


async def kept_in_full(
    result: Any, originals: dict[str, str], tool: str, *, cut: bool = True
) -> Any:
    """Store each result's full text and stamp its ref on the message the model is handed.

    `originals` maps a `tool_call_id` to the text that call returned before any rewrite. Runs before
    the message leaves the middleware, so the bytes are durable before the stream announces the ref.

    - `cut=True` stamps `FULL_RESULT_REF_KEY` whatever the sink answered, plus `RESULT_REF_KEY` when
      it stored. The first pass to cut saw the most of the tool's output, so its stamp stands.
    - `cut=False` stamps only `RESULT_REF_KEY`, and only when the sink stored.

    Returns `result` itself when there is nothing to store, so identity still means "unchanged".
    """
    if not originals:
        return result
    sink = _full_results.get()
    if sink is None and not cut:
        return result
    refs = {
        call_id: (await sink(tool, text) if sink is not None else "")
        for call_id, text in originals.items()
    }

    def _stamped(message: ToolMessage) -> ToolMessage:
        if message.tool_call_id not in refs:
            return message
        ref = refs[message.tool_call_id]
        stamps: dict[str, str] = {FULL_RESULT_REF_KEY: ref} if cut else {}
        if ref and message.status != "error":
            stamps[RESULT_REF_KEY] = ref
        if not stamps:
            return message
        return message.model_copy(
            update={"response_metadata": {**(message.response_metadata or {}), **stamps}}
        )

    return rewritten_tool_messages(result, _stamped)


def bounded_for_batch(
    request: Any,
    content: Any,
    *,
    mark: str = SYSTEM_SPEECH_MARK,
    charged_total: int | None = None,
    expanded_from: int | None = None,
    count: bool = True,
) -> Any:
    """`content` cut to this call's share of the ceiling, counted, logged, and said so in the text.

    Shared by `bound_tool_results` (what a tool returned) and `agent/tool_authz._refusal_message`
    (what a converter writes when a tool raised, which never reaches this middleware), so the
    ceiling is enforced in one place. Returns `content` itself when nothing was removed.

    On the second pass over one result, `charged_total` and `expanded_from` keep the notice about
    the tool (see `bounded_content`), and `count=False` keeps the metric and log row to one per cut.
    """
    tool = str(request.tool_call["name"])
    ceiling = settings.agent_max_tool_result_chars
    # Floored at 1, since 0 would mean "no cap" exactly where the batch is widest.
    limit = max(ceiling // batch_width(request), 1) if ceiling else 0
    bounded, removed = bounded_content(
        content,
        tool,
        limit,
        mark=mark,
        charged_total=charged_total,
        expanded_from=expanded_from,
    )
    if not removed or not count:
        return bounded if removed else content
    # The metric label is the served tool name, never the model's string: an invented name would
    # mint an unbounded series on the unauthenticated `/metrics`. The notice keeps the raw name so
    # the result says which call it belongs to.
    label = metric_tool_name(request, tool)
    record_metric(
        lambda m: m.increment("chemclaw_tool_results_truncated_total", 1.0, {"tool": label})
    )
    log_event(
        logger,
        "tool_result.truncated",
        "cut %d characters from the %s result to stay inside this batch's share of the ceiling",
        removed,
        tool,
        tool=tool,
        characters_removed=removed,
        ceiling=limit,
    )
    return bounded


def _batch_calls(request: Any) -> list[Any]:
    """The tool calls the assistant message that asked for this one made, this one included.

    Read off the originating `AIMessage`, because `ToolNode` gives each call a pre-batch snapshot
    and the sibling results do not exist yet. Empty when the message cannot be found (a middleware
    driven directly), which callers treat as a batch of one.

    Args:
        request: The tool-call request the middleware chain is running.

    Returns:
        The batch's calls, or an empty list when the originating message is not in state.
    """
    messages = (getattr(request, "state", None) or {}).get("messages") or []
    this_call = request.tool_call.get("id")
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None) or []
        if any(call.get("id") == this_call for call in calls):
            return list(calls)
    return []


def batch_width(request: Any) -> int:
    """How many tool calls the assistant message that asked for this one made.

    The divisor of `bounded_for_batch`'s share: every result in the batch reaches the same request.

    Args:
        request: The tool-call request the middleware chain is running.

    Returns:
        The number of calls in this call's batch, never below 1.
    """
    return max(len(_batch_calls(request)), 1)


def batch_siblings(request: Any) -> int:
    """How many calls in this batch invoke the same tool as this one.

    The divisor for the caller's `files` channel, which only `task` writes: concurrent `task` calls
    each read the same pre-batch snapshot, so each must take a share. It counts siblings, not actual
    writers (their results do not exist yet), so a lone writer among silent siblings is cut more
    than needed — this fails closed.

    Args:
        request: The tool-call request the middleware chain is running.

    Returns:
        The number of calls in this batch naming this call's tool, never below 1.
    """
    # Subscript, like `bounded_for_batch`: a missing name would match no sibling, floor to 1 and
    # hand out the whole budget (fail open).
    name = request.tool_call["name"]
    return max(sum(1 for call in _batch_calls(request) if call.get("name") == name), 1)


@wrap_tool_call
async def bound_tool_results(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Cut an oversized result to its batch's share of `agent_max_tool_result_chars`.

    The share, not the whole ceiling, because the context edits cannot reclaim the newest batch; for
    a lone call the share is the ceiling. Applies to every tool, in-process or connector, to
    failures too, and to a `task` report (a `Command`, handled via `agent/tool_result_shape.py`).
    """
    result = await handler(request)
    # Full text of every result in this pass, cut or not, for `kept_in_full` to store after the
    # synchronous rewrite.
    originals: dict[str, str] = {}
    wholes: dict[str, str] = {}

    def _bounded(message: ToolMessage) -> ToolMessage:
        # Recorded before the cut so the outer re-bound's notice describes what the tool returned.
        was = sum(len(span) for span in _spans(message.content))
        content = bounded_for_batch(request, message.content)
        if content is message.content:
            if is_referable(message):
                wholes[message.tool_call_id] = full_text(message.content)
            return message
        originals[message.tool_call_id] = full_text(message.content)
        return message.model_copy(
            update={
                "content": content,
                "response_metadata": {
                    **(message.response_metadata or {}),
                    ORIGINAL_CHARS_KEY: was,
                },
            }
        )

    tool = str(request.tool_call["name"])
    bounded = rewritten_tool_messages(result, _bounded)
    return rewritten_command_files(
        await kept_in_full(await kept_in_full(bounded, originals, tool), wholes, tool, cut=False),
        _bounded_file,
        (getattr(request, "state", None) or {}).get("files"),
        _files_budget(request),
    )


def _files_budget(request: Any) -> int | None:
    """How many characters this command may add to the caller's `files` channel.

    The setting bounds the channel, so what it already holds is subtracted, and the remainder is
    divided among same-tool siblings in the batch, which all read the same snapshot.
    `agent/tool_result_shape.rewritten_command_files` spends it, since only it sees what each file
    (key included) costs.

    Args:
        request: The tool-call request, whose state carries the caller's channels.

    Returns:
        The characters this command may add, or `None` when the setting is 0 (the off switch).
    """
    budget = settings.agent_subagent_files_max_chars
    if budget <= 0:
        return None
    return max(budget - _files_already_held(request), 0) // batch_siblings(request)


def _files_already_held(request: Any) -> int:
    """How many characters of `files` the caller's state carries before this command lands.

    `files` is a `DeltaChannel` and accumulates, so without this N delegations each store the whole
    budget. Keys are counted with their text: the checkpoint stores both, and a path is
    model-written.

    Args:
        request: The tool-call request, whose `state` carries the caller's channels.

    Returns:
        The characters already stored, or 0 when the state is unavailable or holds no files.
    """
    files = (getattr(request, "state", None) or {}).get("files") or {}
    if not isinstance(files, dict):
        return 0
    return sum(
        len(path) + len(data["content"])
        for path, data in files.items()
        if isinstance(data, dict) and isinstance(data.get("content"), str)
    )


def _bounded_file(content: str, share: int) -> str:
    """One file's share of `agent_subagent_files_max_chars`, cut with a notice that says so.

    The share is computed by `agent/tool_result_shape.rewritten_command_files`; this does the cut,
    log and counter. Reuses `bounded_content`, so a cut file keeps both ends and the system-marked
    notice — the caller can read it back. A share of 0 means no cap (the setting's off switch); live
    shares are floored at 1 by the caller.

    Args:
        content: The file's text as the helper left it.
        share: How many characters this file may occupy, or 0 for no cap.

    Returns:
        The text to store, or `content` itself when nothing was cut.
    """
    bounded, removed = bounded_content(content, "task", share)
    if not removed:
        return content
    record_metric(lambda m: m.increment("chemclaw_subagent_file_truncations_total"))
    logger.warning(
        "cut %d character(s) from a file a helper wrote into its caller's state; its share of "
        "`agent_subagent_files_max_chars` — what is left of the channel's budget, divided over the "
        "files still to cross, less this file's own path — is %d character(s)",
        removed,
        share,
    )
    return str(bounded)
