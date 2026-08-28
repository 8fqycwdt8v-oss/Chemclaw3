"""Bound what one tool result may put in front of the model, because nothing else did.

**The gap.** `connector_max_request_bytes` caps what this system *sends* a capability server. There
was no cap in the other direction — not at the transport, not in the middleware chain, not anywhere
— so the size of a tool result was whatever the tool decided, and the two context edits are
precisely the wrong things to catch it. `ClearToolUsesEdit` preserves the newest
`agent_keep_last_tool_groups` results verbatim, and the conversation window never cuts past the
newest group, so **a single oversized result is by construction the one thing neither edit can
reclaim.**

Measured on the shipped defaults: two results at 200,000 characters each — each inside its own
tool's ceiling, `document_read_max_chars` and `calc_find_max_results` x `calc_find_max_result_chars`
— come to 100,077 estimated tokens, one over the budget, and ~224,000 billed. Both edits ran. Both
reclaimed nothing. Adding the static prefix, that is a ~245,000-token request assembled entirely
from numbers this system chose for itself.

**Why a middleware and not a smaller number in each tool's config.** Those per-tool ceilings are
right and stay: each is an argument about what *that* tool should return, made where the tool's
own trade-offs are visible. What none of them can be is a bound on the total, because none of them
knows about the context budget or about each other — the two results above were 60 lines apart in
`core/config/memory.py` and `core/config/sources.py`. This is the floor under all of them, applied
at the one place every tool result passes.

**Where it sits, and both neighbours are decisions.** *Inside* `frame_connector_results`, so the
envelope wraps an already-bounded payload rather than this cutting the envelope's closing tag off.
*Outside* the governance chain, so `audit_events.detail` still records what the tool returned
rather than what the model was shown — the same split, for the same reason, that framing already
argues for.

**Head and tail, never head alone.** `agent/condense.py` makes this argument for a protocol and it
generalises: a procedure states its yield and purity at the *end*, so a head-truncated result reads
as complete and silently drops the outcome. Keeping both ends costs nothing and leaves the two
places a reader's eye actually goes.

**And it says so, in the result, in this system's own words.** A silently shortened result is
`FingerprintSearch.verdict`'s failure one layer down: the model reports on a corpus it was never
shown all of. The notice names the tool, the characters removed and what to do about it.
"""

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from chemclaw.agent.audit import metric_tool_name
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

#: How much of the budget goes to the head. The rest is the tail. Three fifths rather than a half
#: because a result's identifying material — the query it answered, the columns, the first rows —
#: is at the front, while what the tail carries is usually one conclusion.
_HEAD_SHARE = 3 / 5


def _notice(tool: str, removed: int, total: int) -> str:
    """The sentence that replaces the middle, addressed to the model rather than to a log.

    Named as system text and not as tool output, for the reason `TOOL_RESULT_PLACEHOLDER` is: a
    model shown a shortened result with no explanation reads it as the tool having returned that
    much, which is a different fact and one it would reasonably act on. It states the arithmetic so
    the model can say how much it did not see, and it names the remedy that actually exists —
    asking the same tool something narrower, rather than asking it again.
    """
    return (
        f"\n\n[{removed:,} of {total:,} characters removed from the middle of this "
        f"{tool} result to stay inside this session's context budget. This is written by the "
        "system, not by the tool. The result was not empty and was not an error — narrow the "
        "question (a filter, a smaller limit, one identifier) to see the part you need.]\n\n"
    )


def _spans(content: Any) -> list[str]:
    """Every span of text in a `ToolMessage.content`, in order.

    `content` is `str | list[str | dict]` by LangChain's own annotation and both arms occur: an
    in-process tool returns a string, an MCP tool a list of content blocks. A block with no `text`
    is an image or an embedded resource and contributes no span — it is also not something this
    cap can shorten, which is stated rather than silently assumed.
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


def _kept(spans: list[str], limit: int, notice: str) -> list[str]:
    """Per-span replacement text: a prefix from the front, a suffix from the back, nothing between.

    Positional rather than a concatenation, so a multi-block result keeps its blocks — and with
    them the ids `kg.note.mentioned_ids` and `agent/framing.py` read. A span outside both budgets
    becomes `""` and its block is dropped by the caller; the span that straddles the head budget is
    cut at it, and the notice rides on the last surviving head span so it lands in the middle of
    what the model reads rather than at either end.

    Args:
        spans: The result's text spans, in order.
        limit: Total characters the result may keep, notice excluded.
        notice: The sentence explaining the cut.

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
    # Down to and *including* `last_head`: on a single-span result the two walks meet in that one
    # string, and stopping above it would keep a head and no tail at all — which is the truncation
    # this module's docstring argues against.
    for index in range(len(spans) - 1, max(last_head - 1, -1), -1):
        if tail_budget <= 0:
            break
        # Never re-emit what the head already kept: where the two walks meet in one span, the tail
        # may only draw from the part the head did not take.
        available = spans[index][len(heads[index]) :]
        tails[index] = available[-tail_budget:] if tail_budget < len(available) else available
        tail_budget -= len(tails[index])
    return [
        heads[index] + (notice if index == last_head else "") + tails[index]
        for index in range(len(spans))
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
        # No text span to cut — an image or an embedded resource. Carried through untouched, for
        # the same reason `_rewritten_block` carries one through: there is nothing here to shorten.
        rebuilt.append(block)
    return rebuilt


def bounded_content(content: Any, tool: str, limit: int) -> tuple[Any, int]:
    """`content` cut to `limit` characters of text, and how many characters that removed.

    Separated from the middleware so the arithmetic can be exercised on a value rather than through
    a graph — the property worth testing is that both ends survive and the middle does not.

    Returns:
        The bounded content and the number of characters removed (0 when nothing was).
    """
    spans = _spans(content)
    total = sum(len(span) for span in spans)
    if limit <= 0 or total <= limit:
        return content, 0
    kept = _kept(spans, limit, _notice(tool, total - limit, total))
    return _rebuilt(content, kept), total - limit


@wrap_tool_call
async def bound_tool_results(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Cut an oversized tool result to `agent_max_tool_result_chars` before the model reads it.

    Every tool, not only an out-of-process one: the two results that measured this defect —
    `read_document` and `find_calculations` — are both in-process, so a cap keyed on the
    `SERVED_BY` stamp would have missed exactly the case that motivated it.

    A failing result is bounded too. An error is normally short, but a tool that fails by returning
    a provider's whole HTML error page is the same problem in a different dress, and nothing about
    the size argument depends on the status.
    """
    result = await handler(request)
    if not isinstance(result, ToolMessage):
        return result
    # The **registered** tool's name, never the one the model emitted. `ToolNode` runs this chain
    # for a name the graph does not hold — it answers such a call with its own 1,061-character "not
    # a valid tool, try one of […]" message, which is a `ToolMessage` and is bounded like any other
    # — so a label taken from `tool_call["name"]` mints one permanent time series per string a
    # model invents, on an endpoint that is unauthenticated by design. Measured with the ceiling
    # lowered (it is `ge=0` and ENV-overridable, so every legal value has to hold):
    # `chemclaw_tool_results_truncated_total{tool="made_up_yyyy…"} 1`.
    # `audit.metric_tool_name` carries the whole argument, and this is its third caller.
    #
    # The same string names the tool in the notice the model reads and in the line below, and
    # that is deliberate rather than incidental: for a name the graph dispatched the two are the
    # same string, and for one it did not, `unknown` is what the notice should say too.
    tool = metric_tool_name(request)
    content, removed = bounded_content(result.content, tool, settings.agent_max_tool_result_chars)
    if not removed:
        return result
    record_metric(
        lambda m: m.increment("chemclaw_tool_results_truncated_total", 1.0, {"tool": tool})
    )
    log_event(
        logger,
        "tool_result.truncated",
        "cut %d characters from the %s result to stay inside the per-result ceiling",
        removed,
        tool,
        tool=tool,
        characters_removed=removed,
        ceiling=settings.agent_max_tool_result_chars,
    )
    return result.model_copy(update={"content": content})
