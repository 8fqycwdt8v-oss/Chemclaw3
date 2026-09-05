"""Bound what one model call's tool results may put in front of the model, because nothing did.

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

**The unit is the batch, and for a long time it was the result — which bounded nothing.** The
argument above is about what the edits cannot reclaim, and what they cannot reclaim is not one
result: `ClearOlderToolResultsEdit` raises `keep` to `newest_batch_size(messages)` so the *whole*
newest batch survives, and the conversation window clamps its cut at the newest group. Both are
right — that evidence has not been read yet — and both are exactly why a fan-out escaped a
per-result ceiling. Nothing capped how many results one assistant message produces, so the bound
was the product of two numbers chosen a hundred lines apart in `core/config/agent.py`: 60,000
characters and eight parallel calls, 480,000 characters, ~120,000 estimated tokens before the
~43,000-token prefix. Measured on the compiled graph, a batch of results each at the ceiling sent
**164,229** estimated tokens at a width of 8 and **345,735** at 20, against a 100,000 budget, with
`chemclaw_context_compactions_total` at 0 because there was nothing older to clear.
`agent_max_parallel_tool_calls` is not the missing bound either: it is LangGraph's
`max_concurrency`, so twenty calls still return twenty results.

So `agent_max_tool_result_chars` is divided by the batch's width. A lone call — which is nearly
every call — gets the whole ceiling exactly as before; a batch shares it. The share is even rather
than first-come, which costs a narrow result the chance to lend its slack to a wide one and buys
the property that what the model reads does not depend on which tool returned first.

**Why a middleware and not a smaller number in each tool's config.** Those per-tool ceilings are
right and stay: each is an argument about what *that* tool should return, made where the tool's
own trade-offs are visible. What none of them can be is a bound on the total, because none of them
knows about the context budget or about each other — the two results above were 60 lines apart in
`core/config/memory.py` and `core/config/sources.py`. This is the floor under all of them.

**Applied at two places, and the second is not a leak in the first.** This paragraph said "at the
one place every tool result passes" for as long as that was false. It is true of every result a
tool *returns*; it was never true of the two this system **composes**.
`surface_authorization_denials` and `surface_domain_errors` are at indices 0 and 1 of
`tool_call_middleware` and the middleware below is at 3, so a gate that refuses by raising travels
up past the cut and the converter manufactures the `ToolMessage` the model reads. Measured, against
a 60,000-character ceiling: a **200,254**-character malformed-argument refusal and a
**150,141**-character denial, both of them interpolating model-authored text. `bound_refusal_text`
is that second point — one function, called from `agent/tool_authz._refusal_message`, which is the
one function both converters compose through — and it shares this module's arithmetic, notice and
counter rather than restating them.

**Where the middleware sits, and all three neighbours are decisions.** *Inside*
`frame_connector_results`, so the envelope wraps an already-bounded payload rather than this cutting
the envelope's closing tag off. *Outside* `defang_tool_results`, because neutralising a forged
delimiter **grows** the payload — every `<` becomes `&lt;` once a delimiter is disguised — and a
rewrite that grows a result above this ceiling is a rewrite this ceiling does not bound: measured on
the compiled graph at the shipped fan-out width, results cut to their 7,500-character share reached
the model at **29,096** each. *Outside* the governance chain, so `audit_events.detail` still records
what the tool returned rather than what the model was shown — the same split, for the same reason,
that framing already argues for.

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
from chemclaw.agent.tool_result_shape import rewritten_tool_messages
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


def _brief_notice(tool: str, removed: int) -> str:
    """The shortest honest form of the sentence above, for a share too small to hold it.

    A batch's share is `agent_max_tool_result_chars // width`, so a wide enough fan-out drives it
    below the explanatory notice — and the explanatory notice is then *longer than the budget it is
    explaining*. Returning it anyway is what made the bound stop bounding: each result floored at
    ~312 characters, so the batch total grew linearly with width (measured 124,800 characters at
    width 400 against a 60,000 ceiling, and 312,000 at width 1000).

    The model loses the advice about narrowing its question, which is the right thing to lose: at
    this width it is not going to read four hundred copies of it. What it keeps is the three facts
    it cannot act correctly without — that something was removed, how much, and that the removal is
    the system's rather than the tool's, so an empty-looking answer is not read as an empty result.
    """
    return f"[{removed:,} chars cut from this {tool} result by the system]"


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


def _carrier(content: Any, spans: list[str]) -> int:
    """The first block index whose text `_rebuilt` will actually keep.

    `_kept` used to put the notice at index 0 unconditionally, and `_rebuilt` drops the text it
    computed for any block that is neither a string nor a dict with a `text` key — so a result
    whose first block is an image lost the notice entirely: measured, 9,000 characters removed, the
    truncation counter incremented, and the model handed the image alone with nothing saying the
    rest had gone. The silent cut this module exists to prevent, one block along.

    Falls back to 0 when no block can carry text, which is the case where `_rebuilt` returns the
    content untouched anyway and there is nothing to say.
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

    Positional rather than a concatenation, so a multi-block result keeps its blocks — and with
    them the ids `kg.note.mentioned_ids` and `agent/framing.py` read. A span outside both budgets
    becomes `""` and its block is dropped by the caller; the span that straddles the head budget is
    cut at it, and the notice rides on the last surviving head span so it lands in the middle of
    what the model reads rather than at either end.

    Args:
        spans: The result's text spans, in order.
        limit: Total characters the result may keep, notice excluded.
        notice: The sentence explaining the cut.
        carrier: The earliest index whose text survives `_rebuilt`; the notice never lands
            before it, because a block that carries no text span drops whatever is computed for it.

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
    # `max(last_head, 0)` rather than `last_head`, because a head budget of 0 leaves it at -1 and
    # no index would match: below a limit of 2, `int(limit * 3/5)` is 0, the head loop breaks
    # before its first iteration and the notice — this module's one contract — was discarded, so
    # the result was silently shortened instead. `agent_max_tool_result_chars` is `ge=0`, so a
    # deployment can reach that, and `bounded_content` reaches it deliberately whenever the limit
    # is smaller than the sentence explaining the cut.
    # The notice goes on the block that will survive `_rebuilt` — `max(last_head, 0)` decided
    # where the *budget* ran out, which is not the same question as which block can hold a
    # sentence, and on an image-first result the two answers differ.
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
        # No text span to cut — an image or an embedded resource. Carried through untouched, for
        # the same reason `_rewritten_block` carries one through: there is nothing here to shorten.
        rebuilt.append(block)
    return rebuilt


def bounded_content(content: Any, tool: str, limit: int) -> tuple[Any, int]:
    """`content` cut to `limit` characters of text, and how many characters that removed.

    Separated from the middleware so the arithmetic can be exercised on a value rather than through
    a graph — the property worth testing is that both ends survive and the middle does not.

    **The notice is charged against `limit`, not added to it**, so what comes back is at most
    `limit` characters and the name of this function is true of its result. It used to keep exactly
    `limit` and then insert a 313-character notice, which for an overshoot smaller than the notice
    *grew* the payload: measured at the shipped ceiling, 60,001 characters in and **60,313 out**,
    312 more than the tool returned, with the truncation counter incremented and the notice's own
    arithmetic ("1 of 60,001 characters removed") true of the cut and false of the effect. An exact
    ceiling is also what lets `bound_tool_results` divide one into shares.

    Its length is measured at the notice's *widest* form — `removed` can never exceed `total`, so
    that bounds the digits — and the sentence the model actually reads is then built from the real
    numbers. Solving the circularity exactly would cost a fixed point to save three characters.

    **A result shorter than the notice is left alone**, which is where this module's two rules meet:
    a cut is never silent, and a bound never grows what it bounds. Below that length cutting cannot
    reclaim anything, so there is no cut and no notice is owed.

    Returns:
        The bounded content and the number of characters removed (0 when nothing was).
    """
    spans = _spans(content)
    total = sum(len(span) for span in spans)
    if limit <= 0 or total <= limit:
        return content, 0
    widest = len(_notice(tool, total, total))
    carrier = _carrier(content, spans)
    if limit < widest:
        # The share is smaller than the sentence explaining the cut, so the sentence is the thing
        # that has to give: keeping the explanatory form anyway floors every result at its ~312
        # characters and makes the batch total grow with the width instead of being bounded by the
        # ceiling — 124,800 characters at width 400 against a 60,000 ceiling, measured.
        #
        # **The brief form is not itself cut**, and that is the one place this function
        # deliberately returns more than `limit`. Cutting it would buy the arithmetic and sell the
        # contract: at a limit of 1 the result is `[`, which is a silent cut wearing a bracket.
        # What it costs is bounded and unreachable in practice — the brief form is 19 characters,
        # so the batch only exceeds the ceiling above width `ceiling // 19`, which at the shipped
        # 60,000 is **3,158 tool calls in one assistant message**. Below that the total falls
        # rather than rises, because 19 is far under the share it replaces.
        brief = _brief_notice(tool, total)
        if total <= len(brief):
            return content, 0
        return _rebuilt(content, _kept(spans, 0, brief, carrier)), total
    if total <= widest:
        return content, 0
    kept = max(limit - widest, 0)
    removed = total - kept
    return _rebuilt(content, _kept(spans, kept, _notice(tool, removed, total), carrier)), removed


def batch_width(request: Any) -> int:
    """How many tool calls the assistant message that asked for *this* one made.

    The denominator of the share below, and the number nothing was dividing by. It is read off the
    message rather than counted from the state's tool results, for the reason
    `plan_gate.rewrite_todos_in_batch` gives for the same walk: `ToolNode` hands each call a
    runtime built from one pre-batch snapshot, so the originating `AIMessage` is the only place the
    *other* calls running right now are visible — the results do not exist yet.

    1 when the message cannot be found rather than 0 or a guess: off the request path (a middleware
    driven directly, `agent/tool_invocation.py`'s no-graph fold) there is no batch, and one call
    getting the whole ceiling is exactly today's behaviour.

    Args:
        request: The tool-call request the middleware chain is running.

    Returns:
        The number of calls in this call's batch, never below 1.
    """
    messages = (getattr(request, "state", None) or {}).get("messages") or []
    this_call = request.tool_call.get("id")
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None) or []
        if any(call.get("id") == this_call for call in calls):
            return max(len(calls), 1)
    return 1


def result_char_limit(request: Any) -> int:
    """This call's share of `agent_max_tool_result_chars`; 0 when a deployment disabled the cap.

    One function rather than two spellings of the same arithmetic, because the ceiling now has two
    application points and a share computed differently at each would be a cap that depends on
    which one ran. See `bound_refusal_text` for why the second point exists.

    Never below 1 when a ceiling is configured: 0 is the deployment's own "no cap", and a share that
    rounded to it would restore the unbounded behaviour exactly where the batch is widest.
    """
    ceiling = settings.agent_max_tool_result_chars
    return max(ceiling // batch_width(request), 1) if ceiling else 0


def _record_cut(label: str, tool: str, removed: int, limit: int) -> None:
    """Count and log one cut, under a label the registry served rather than a string a model wrote.

    The label clamp is `agent/audit.metric_tool_name`'s, reused rather than re-derived: `ToolNode`
    dispatches an unregistered name through this chain, and an invented 90,000-character name once
    minted a 90,054-character `chemclaw_tool_results_truncated_total{tool=…}` line on an
    unauthenticated `/metrics`, one series per name.
    """
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


def bound_refusal_text(request: Any, text: str) -> str:
    """Cut a refusal *this system composed* to the same share every tool result gets.

    **The second application point, and it exists because the first cannot reach these.**
    `bound_tool_results` is at index 3 of `tool_call_middleware`; `surface_authorization_denials`
    and `surface_domain_errors` are at 0 and 1. A gate below raises, the exception travels up *past*
    the cut, and the converter **manufactures** the `ToolMessage` the model reads — so the module
    docstring's "the one place every tool result passes" was true of every returned result and false
    for exactly the two this system writes itself.

    That would be a footnote if those two sentences were this system's own words end to end. They
    are not: both interpolate **model-authored** text, which this tree's own threat model treats as
    attacker-influenceable — it is why `metric_tool_name` clamps the metric label and `bounded_repr`
    clamps the audit row, and the thread was the one reader left unclamped. Measured on the real
    chain against a 60,000-character ceiling: a 200,000-character malformed-argument document
    (`agent/model_calls.refuse_unparsed_arguments` embeds `defang(str(document))`) reached the model
    as a **200,254**-character result, and a 150,000-character invented tool name under
    `tool_authz_default="deny"` as a **150,141**-character refusal. Neither is one-shot — the repeat
    guard keys on name plus arguments, so each distinct invented name is a fresh call.

    **Here rather than in each converter**, because `agent/tool_authz._refusal_message` is the one
    function both compose through, and a bound applied at the two call sites is a bound the third
    one forgets.

    **The notice names the clamped tool, not the raw one, and that is load-bearing rather than
    tidy.** `bounded_content` measures the notice at its widest and leaves a result shorter than
    that alone — so interpolating a 150,000-character invented name would make `widest` exceed the
    limit and the function would return the refusal **unbounded**, silently. The same clamp the
    metric label uses is the one the notice must use.

    Args:
        request: The tool-call request the converter is answering.
        text: The refusal or fault sentence the converter composed.

    Returns:
        `text`, cut head-and-tail with the usual notice if it was over this call's share.
    """
    label = metric_tool_name(request, str(request.tool_call["name"]))
    limit = result_char_limit(request)
    bounded, removed = bounded_content(text, label, limit)
    if removed:
        _record_cut(label, label, removed, limit)
    return str(bounded)


@wrap_tool_call
async def bound_tool_results(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Cut an oversized result to its batch's share of `agent_max_tool_result_chars`.

    **The share, not the whole ceiling**, because what the two context edits cannot reclaim is the
    newest *batch* — see the module docstring for the measurement. `batch_width` is the divisor and
    it is 1 for a lone call, so this is unchanged for nearly every call a turn makes.

    Every tool, not only an out-of-process one: the two results that measured this defect —
    `read_document` and `find_calculations` — are both in-process, so a cap keyed on the
    `SERVED_BY` stamp would have missed exactly the case that motivated it.

    A failing result is bounded too. An error is normally short, but a tool that fails by returning
    a provider's whole HTML error page is the same problem in a different dress, and nothing about
    the size argument depends on the status.

    **"Every tool" was not true until `agent/tool_result_shape.py` existed**, and the exception was
    the worst one to have: `task` returns a `Command` rather than a `ToolMessage`, so the guard this
    used to open with excused a helper's report — the one result that is unbounded prose a model
    wrote. Nothing else caught it in the band that matters either. Upstream's `FilesystemMiddleware`
    evicts a result over 20,000 tokens (80,000 chars) to `/large_tool_results/`, and this ceiling is
    60,000, so a report measured at **70,048 characters** reached the caller's thread whole.
    """
    result = await handler(request)
    tool = str(request.tool_call["name"])
    # **The metric label is the served name, never the model's string**, and `core/metrics.py`
    # already claimed it was ("a tool name here is one the registry served, never a string a caller
    # invented"). It was not: `ToolNode` dispatches an unregistered name through this chain, its
    # not-a-valid-tool error echoes that name back, and the echo is over the ceiling whenever the
    # name is — so an invented name of 90,000 characters minted a **90,054-character**
    # `chemclaw_tool_results_truncated_total{tool=…}` line on an unauthenticated `/metrics`, one
    # series per invented name. The same clamp `agent/audit.metric_tool_name` applies two
    # middlewares away, reused rather than re-derived; the notice the model reads keeps the raw
    # name, because a *returned* result must say which call it belongs to and the name it belongs
    # to is bounded here by the result it rides on. `bound_refusal_text` cannot make that trade —
    # see its docstring.
    label = metric_tool_name(request, tool)
    limit = result_char_limit(request)

    def _bounded(message: ToolMessage) -> ToolMessage:
        content, removed = bounded_content(message.content, tool, limit)
        if not removed:
            return message
        _record_cut(label, tool, removed, limit)
        return message.model_copy(update={"content": content})

    return rewritten_tool_messages(result, _bounded)
