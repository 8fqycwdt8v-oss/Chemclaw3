"""One tool result cannot be larger than the budget it is inside — the bound that did not exist.

`connector_max_request_bytes` capped what this system sends a capability server. Nothing capped
what came back, and the two context edits are structurally unable to: `ClearToolUsesEdit` preserves
the newest `agent_keep_last_tool_groups` results verbatim and the conversation window never cuts
past the newest group, so a single oversized result is the one thing neither can reclaim.

Measured on the shipped defaults, with each result inside its own tool's ceiling: two results at
200,000 characters are 100,077 estimated tokens — one over the budget — and ~224,000 billed, with
both edits running and reclaiming nothing.
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import StructuredTool

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.context_budget import estimate_tool_schemas
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.tool_result_size import (
    _notice,
    bound_tool_results,
    bounded_content,
    bounded_for_batch,
)
from chemclaw.connectors.transport import SERVED_BY
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS


def test_a_result_inside_the_ceiling_is_untouched() -> None:
    """Identity, not a copy: most results are small and must cost nothing at all."""
    content = "a modest answer"

    bounded, removed = bounded_content(content, "find_notes", 60_000)

    assert bounded is content
    assert removed == 0


def test_both_ends_of_an_oversized_result_survive() -> None:
    """Head *and* tail, because a procedure states its outcome at the end.

    `agent/condense.py` makes this argument for a protocol and it generalises: a head-truncated
    result returns conditions that look complete with the yield and purity silently absent, which
    reads as "not measured" against neighbours that measured it. Keeping both ends costs nothing
    and leaves the two places a reader's eye actually goes.
    """
    content = "HEAD" + ("x" * 100_000) + "TAIL"

    bounded, removed = bounded_content(content, "read_document", 1_000)

    assert bounded.startswith("HEAD")
    assert bounded.endswith("TAIL")
    # Inside the ceiling, notice included — see `test_a_cut_result_is_never_larger_than_its
    # _ceiling`. So more of the tool's own text is removed than the naive `total - limit`.
    assert len(bounded) <= 1_000
    assert removed > len(content) - 1_000


def test_the_cut_says_it_happened_and_says_who_said_so() -> None:
    """A silently shortened result is a model reporting on a corpus it was never shown all of.

    The notice names the tool, the arithmetic and the remedy that exists — narrowing the question,
    not asking again — and marks itself as system text, for the reason `TOOL_RESULT_PLACEHOLDER`
    does: a model shown a shortened result with no explanation reads it as what the tool returned.
    """
    bounded, _ = bounded_content("x" * 50_000, "find_calculations", 1_000)

    assert "written by the" in bounded and "not by the tool" in bounded
    assert "find_calculations" in bounded
    assert "50,000" in bounded, "the notice does not say how much the model is not seeing"


def test_a_block_list_keeps_its_blocks() -> None:
    """An MCP result is content blocks, and their ids are read as citations.

    Cutting positionally rather than by concatenating is what keeps a truncated multi-block result
    the same result: `agent/framing.py` and `kg.note.mentioned_ids` both read a block's other keys.
    """
    content = [
        {"type": "text", "text": "A" * 40_000, "id": "first"},
        {"type": "image", "data": "…"},
        {"type": "text", "text": "B" * 40_000, "id": "second"},
    ]

    bounded, removed = bounded_content(content, "search_patents", 2_000)

    assert sum(len(block["text"]) for block in bounded if "text" in block) <= 2_000
    assert removed > 78_000, "the notice is charged against the ceiling, so more text goes"
    assert bounded[0]["id"] == "first"
    assert bounded[0]["text"].startswith("A")
    assert {"type": "image", "data": "…"} in bounded, "a block with no text span was dropped"
    assert bounded[-1]["text"].endswith("B")


def test_the_cap_can_be_switched_off() -> None:
    """0 restores the unbounded behaviour, which is a decision rather than an accident."""
    content = "x" * 500_000

    bounded, removed = bounded_content(content, "read_document", 0)

    assert bounded is content and removed == 0


def test_switching_the_cap_off_reaches_the_share_arithmetic_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 means "no ceiling" all the way through, and only the *callee* was ever asked.

    `test_the_cap_can_be_switched_off` above proves `bounded_content(..., 0)` is a no-op. Nothing
    asserted that `bounded_for_batch` hands it 0 when `agent_max_tool_result_chars` is 0 — and that
    translation is where the meaning lives, because the share is `max(ceiling // width, 1)` and the
    floor is deliberately *not* applied when the ceiling is off. Measured with `else 1` in that
    conditional: a 5,000-character result comes back as **62 characters** of notice saying it was
    cut, in every deployment that switched the cap off, with this whole file green. The rule was
    tested; the translation the rule depends on was not.

    Identity rather than equality, because "unchanged" is what `bound_tool_results` reads to decide
    whether to copy the message at all.
    """
    monkeypatch.setattr(settings, "agent_max_tool_result_chars", 0)
    content = "A" * 5_000
    before = METRICS.value("chemclaw_tool_results_truncated_total")

    out = bounded_for_batch(cast(Any, _Request("read_document")), content)

    assert out is content, f"the ceiling was off and {5_000 - len(out)} characters went anyway"
    assert METRICS.value("chemclaw_tool_results_truncated_total") == before, (
        "a result was counted as truncated while the cap was off"
    )


def test_a_result_of_exactly_the_limit_is_not_cut() -> None:
    """`<=`, not `<`: at the ceiling exactly there is nothing to reclaim and nothing to say.

    The boundary is the only place "at most `limit` characters" is an interesting claim, and it is
    where this file used to stop — every fixture sat comfortably either side. One character in from
    it, the cut is the one this module's own docstring calls the defect it exists to prevent: a
    result at the ceiling replaced by a shorter result plus a notice, which is the truncation that
    grew what it bounded re-entering by the other door.
    """
    at_limit = "A" * 1_000
    bounded, removed = bounded_content(at_limit, "read_document", 1_000)

    assert bounded is at_limit and removed == 0

    over_by_one, removed_over = bounded_content("A" * 1_001, "read_document", 1_000)

    assert removed_over > 0 and len(over_by_one) <= 1_000


def test_a_limit_of_exactly_the_notice_keeps_the_explanatory_form_and_no_text() -> None:
    """`limit == widest` is where the two notice forms and the `kept` floor all disagree.

    Below the widest form of the sentence the brief form is used; at it exactly the explanatory
    form fits, and the whole share is spent on it — `kept` is 0, not 1. Three mutations of this
    function disagree about this single point (`limit < widest` -> `<=`, `max(limit - widest, 0)`
    -> `1`, and the at-the-limit branch above) and 126 tests could not tell any of them apart,
    because no fixture ever put `limit` there.
    """
    total = 5_000
    widest = len(_notice("read_document", total, total))

    bounded, removed = bounded_content("A" * total, "read_document", widest)

    assert len(bounded) <= widest, "the bound returned more than the limit it was given"
    assert "removed from the middle" in bounded, "the brief form was used where the full one fits"
    assert removed == total, "a character of the tool's own text was kept inside the notice's share"


def test_the_head_and_tail_budgets_are_spent_down_across_every_block() -> None:
    """Three text blocks, because at two the accumulation is indistinguishable from a reset.

    `_kept` walks the spans spending one head budget and one tail budget down across all of them.
    Every fixture in this file had one text block or two, and with two the second walk's `-=` is
    reached at most once — so `head_budget -= len(...)` could become `head_budget = len(...)` and
    all 18 tests in the repository that execute `_kept` still passed. Under that mutation the
    budget *resets* at every block, so a result is bounded per block rather than in total: measured
    here, 3,042 characters of tool text against a 2,000 limit. The share `bounded_for_batch`
    divides is then not a ceiling at all, which is the property this whole module is for.

    The same fixture pins the three things the walk owes its caller besides the total: the head
    survives, the tail survives (the tail budget is spent down too, and a reset there loses it
    outright), and the notice lands on a block that survives `_rebuilt` rather than on the trailing
    image, whose text `_rebuilt` discards.
    """
    limit = 2_000
    image = {"type": "image", "source": {"data": "abc"}}
    content: list[Any] = ["A" * 40_000, "B" * 40_000, "C" * 40_000, image]

    bounded, removed = bounded_content(content, "sweep", limit)

    text = "".join(block for block in bounded if isinstance(block, str))
    assert len(text) <= limit, f"three blocks kept {len(text)} characters against a {limit} limit"
    assert text.startswith("A"), "the head of the first block went"
    assert text.endswith("C"), "the tail of the last block went"
    assert "removed from the middle" in text, "the cut landed somewhere the rebuild discards it"
    assert image in bounded, "a block with no text span was dropped"
    assert removed > 0


def test_a_string_block_carries_the_notice_when_the_first_block_cannot() -> None:
    """The same silent-cut guard, for the block shape an in-process tool returns.

    `test_a_cut_is_not_silent_when_the_first_block_carries_no_text` proves this for a list of
    *dicts*. A content list may also hold bare strings, and `_carrier`'s test for one is
    `isinstance(block, str) or carries_text` — the `or` arm, which no fixture reached. Mutated to
    `and`, no bare string is ever a carrier, the notice is computed for the leading image, and
    `_rebuilt` discards the text of a block that carries none: the result is shortened by 9,000
    characters with nothing in it saying so.

    Driven below the explanatory notice's own length on purpose, because that is the only share at
    which the head budget is 0 and the carrier — rather than where the budget ran out — decides
    where the sentence lands.
    """
    image = {"type": "image", "source": {"data": "abc"}}
    content: list[Any] = [image, "y" * 9_000]

    bounded, removed = bounded_content(content, "sweep", 50)

    assert removed == 9_000
    text = "".join(block for block in bounded if isinstance(block, str))
    assert "chars cut" in text, "the result was shortened and nothing in it says so"
    assert image in bounded


def test_an_oversized_result_is_bounded_on_its_way_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the middleware, because the claim is about the chain and not about the arithmetic.

    Every tool, not only an out-of-process one: the two results that measured this defect —
    `read_document` and `find_calculations` — are in-process, so a cap keyed on the `SERVED_BY`
    stamp would have missed exactly the case it exists for.
    """
    monkeypatch.setattr(settings, "agent_max_tool_result_chars", 5_000)
    request = _Request("find_calculations")

    async def handler(_: Any) -> ToolMessage:
        return ToolMessage(content="y" * 200_000, tool_call_id="c1", name="find_calculations")

    before = METRICS.value("chemclaw_tool_results_truncated_total")
    # `_Request` carries the one attribute the middleware reads; `ToolCallRequest` is a
    # dataclass with a graph's worth of fields around it.
    result = asyncio.run(bound_tool_results.awrap_tool_call(cast(Any, request), handler))

    assert isinstance(result, ToolMessage)
    assert len(result.content) < 200_000
    assert METRICS.value("chemclaw_tool_results_truncated_total") > before


class _Request:
    """The attributes `bound_tool_results` reads off a tool-call request.

    `tool` is `None`, which is both LangChain's documented default for a request built outside a
    graph and what `ToolNode` passes for a name the graph does not hold — so the metric label
    clamps to `"unknown"`, which is the case the clamp exists for.
    """

    def __init__(self, name: str) -> None:
        """Name the tool this request is for; nothing else about it is read."""
        self.tool_call = {"name": name, "args": {}, "id": "c1"}
        self.tool = None
        self.state: dict[str, Any] = {}


def test_an_invented_tool_name_never_reaches_the_truncation_label() -> None:
    """The counter is on an unauthenticated `/metrics`, so its label may not be model-authored.

    `core/metrics.py` declares this counter's label as bounded and says why — "a tool name here is
    one the registry served, never a string a caller invented" — and that was the belief rather
    than the code. `ToolNode` dispatches an unregistered name through this chain deliberately, its
    not-a-valid-tool error **echoes the name back**, and the echo is over the ceiling exactly when
    the name is: measured on a compiled graph, a 90,006-character invented name minted a
    **90,054-character** exposition line, one new series per name, while
    `chemclaw_tool_calls_total` beside it correctly read `tool="unknown"` — the same clamp, two
    middlewares away, already applied.
    """
    request = _Request("EXFIL_" + "B" * 200)

    async def handler(_: Any) -> ToolMessage:
        return ToolMessage(content="y" * 200_000, tool_call_id="c1")

    asyncio.run(bound_tool_results.awrap_tool_call(cast(Any, request), handler))

    rendered = METRICS.render()
    assert "EXFIL_" not in rendered, "a model's string became a metric label"
    assert 'chemclaw_tool_results_truncated_total{tool="unknown"}' in rendered


def test_a_cut_result_is_never_larger_than_its_ceiling() -> None:
    """The notice is charged against the ceiling, because the model reads it like any other span.

    Past the ceiling the function used to keep exactly `limit` characters and *then* add a
    313-character notice, so the bound returned `limit + 313` — and for an overshoot smaller than
    the notice it grew what it was bounding: measured at the shipped ceiling, 60,001 characters in,
    **60,313 out**, 312 more than the tool returned, with the truncation counter incremented and a
    notice telling the model to narrow its question. A ceiling that is exact is also what lets a
    batch's share of it be exact (`bound_tool_results`).
    """
    for total in (60_001, 61_000, 100_000):
        bounded, removed = bounded_content("A" * total, "read_document", 60_000)

        assert len(bounded) <= 60_000, f"{total} characters came back as {len(bounded)}"
        assert len(bounded) < total, "the bound grew the result it was bounding"
        assert removed > 0


def test_a_cut_is_never_silent_at_the_smallest_configurable_ceiling() -> None:
    """`agent_max_tool_result_chars` is `ge=0`, so a deployment may set 1 — and did lose the notice.

    Three fifths of the limit goes to the head, which rounds to 0 below 2: the head loop then broke
    before its first iteration, `last_head` stayed at -1, no index matched, and the notice was
    dropped. `bounded_content("A" * 1_000, …, 1)` returned a single character with nothing saying
    so — the one contract this module has, that a cut is never silent, broken at the edge of its
    own configuration range.
    """
    bounded, removed = bounded_content("A" * 1_000, "read_document", 1)

    assert removed == 1_000, "every character of the result was dropped"
    # The brief form, because the explanatory sentence is 312 characters and the limit is 1. It
    # keeps the three facts the model cannot act correctly without — that something was removed,
    # how much, and that the system removed it rather than the tool returning nothing — and drops
    # the advice about narrowing the question, which is what there is no room for.
    assert "read_document" in bounded and "by the system" in bounded
    assert "1,000 chars cut" in bounded


def test_a_result_smaller_than_the_notice_is_left_alone() -> None:
    """Below the notice's own length there is nothing to reclaim, so nothing is cut.

    The two rules — a cut is never silent, and a bound never grows what it bounds — collide only
    here, and this is the resolution: a result shorter than the sentence explaining the cut cannot
    be made smaller by cutting it, so it is not cut and no notice is owed.
    """
    bounded, removed = bounded_content("AB", "read_document", 1)

    assert bounded == "AB" and removed == 0


#: What the fan-out model was sent on each call, and what was bound to it. Module level rather than
#: instance state for the reason `tests/test_compaction.py` gives: a `BaseChatModel` is a pydantic
#: model, so an annotated class attribute would become a *field* with a mutable default.
_SENT: list[list[Any]] = []
_BOUND: list[Any] = []


class _FanOutModel(GenericFakeChatModel):
    """Ask for a whole batch of calls in one message, then answer — recording what it was sent.

    The request the second call receives is the one under test: it is the first time the model sees
    the batch's results, so it is the request neither context edit may reduce.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Record the bound surface, so the prefix can be measured from the far side of the call."""
        _BOUND[:] = list(tools)
        return self

    def _generate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        """Record the request, then replay the script."""
        _SENT.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _oversized_sweep() -> Any:
    """A connector tool whose every answer is well past the per-result ceiling."""

    async def sweep(q: str) -> str:
        """Sweep the corpus.

        Args:
            q: the query
        """
        return "X" * 200_000

    tool = StructuredTool.from_function(coroutine=sweep, name="sweep", description="Sweep.")
    # The stamp `connectors/transport._stamped` writes, so the result is framed as well as bounded
    # — the two rewrites a real connector result passes through.
    tool.metadata = {SERVED_BY: {"connector": "fakeconn", "server": "s"}}
    return tool


@pytest.mark.parametrize("width", [8, 20])
def test_one_assistant_message_cannot_fan_out_past_the_request_budget(width: int) -> None:
    """The ceiling bounds one *result*; what neither context edit can reclaim is one *batch*.

    **Measured before the fix, on this graph.** `ClearOlderToolResultsEdit` raises `keep` to the
    newest batch's size so the batch survives by construction, and the conversation window clamps
    its cut at the newest group — both correct for evidence the model has not read yet, and exactly
    why a fan-out escapes. Nothing bounded the product of the per-result ceiling and the batch
    width: at the shipped 60,000 characters and a width of 8 the request went out at **164,232**
    estimated tokens against a 100,000 budget, and at 20 at **345,735** — every control doing
    precisely what it documents, `chemclaw_context_compactions_total` at 0 because there was
    nothing older to clear.

    `agent_max_parallel_tool_calls` is not the missing bound: it is LangGraph's `max_concurrency`,
    so 20 calls still yield 20 results, which is why the width is swept past it here.
    """
    _SENT.clear()
    _BOUND.clear()
    calls = [{"name": "sweep", "args": {"q": f"q{i}"}, "id": f"c{i}"} for i in range(width)]
    model = _FanOutModel(
        messages=iter([AIMessage(content="", tool_calls=calls), AIMessage(content="done")])
    )
    graph = build_langgraph_agent(
        model=model, connectors=[_oversized_sweep()], audit_sink=NullAuditSink()
    )

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}))

    assert len(_SENT) == 2, "the model was never handed the batch's results"
    sent = _SENT[1]
    results = [m for m in sent if isinstance(m, ToolMessage)]
    assert len(results) == width, "the fixture did not actually fan out"
    # The whole request, because that is what the provider bills and what
    # `agent_context_token_budget` has bounded since the prefix was charged unconditionally.
    prefix = count_tokens_approximately([m for m in sent if isinstance(m, SystemMessage)])
    prefix += estimate_tool_schemas(_BOUND)
    thread = count_tokens_approximately([m for m in sent if not isinstance(m, SystemMessage)])

    assert prefix + thread <= settings.agent_context_token_budget, (
        f"a {width}-wide fan-out sent {prefix + thread} estimated tokens against a budget of "
        f"{settings.agent_context_token_budget}"
    )


@pytest.mark.parametrize("width", [190, 400, 1000])
def test_the_batch_share_bounds_the_batch_at_every_width(width: int) -> None:
    """The share has to bound the *batch*, and past a certain width it stopped doing so.

    `bounded_content` refused to return less than the sentence explaining the cut, so once
    `agent_max_tool_result_chars // width` fell below that sentence's ~312 characters every result
    floored there and the batch total grew linearly with the width instead of being capped.
    Measured before this: **124,800** characters at width 400 against a 60,000 ceiling, and
    **312,000** at width 1000 — the defect the share was introduced to close, one order of
    magnitude up.

    Swept past the crossover deliberately. The first version of this test used widths 8 and 20,
    both comfortably below it, which is why it passed against the floor.
    """
    ceiling = settings.agent_max_tool_result_chars
    share = max(ceiling // width, 1)
    out, _ = bounded_content("x" * 200_000, "sweep", share)
    # Per result, the share or the brief notice, whichever is larger — the notice is never cut,
    # because a bound paid for by saying nothing is not what this module is for.
    assert len(out) <= max(share, 19), f"one result overran its share at width {width}"
    assert len(out) * width <= ceiling, (
        f"the batch totalled {len(out) * width:,} against a {ceiling:,} ceiling at width {width}"
    )


def test_a_cut_is_not_silent_when_the_first_block_carries_no_text() -> None:
    """The notice has to land on a block that survives the rebuild.

    `_kept` placed it at index 0 regardless, and `_rebuilt` drops the text computed for any block
    that is neither a string nor a dict with a `text` key — so an image-first result lost the
    notice entirely: the characters went, the truncation counter moved, and the model was handed
    the image with nothing saying the rest had been removed. That is the silent cut this module
    exists to prevent, one block along from where it was being prevented.
    """
    content = [{"type": "image", "source": {"data": "abc"}}, {"type": "text", "text": "y" * 9_000}]
    out, removed = bounded_content(content, "sweep", 500)
    assert removed > 0
    text = "".join(b.get("text", "") for b in out if isinstance(b, dict))
    assert "removed from the middle" in text or "chars cut" in text, (
        "the result was shortened and nothing in it says so"
    )
    # The image is still there: this cap shortens text and carries everything else through.
    assert any(isinstance(b, dict) and b.get("type") == "image" for b in out)
