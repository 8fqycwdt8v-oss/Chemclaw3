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
from chemclaw.agent.tool_result_size import bound_tool_results, bounded_content
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
    assert "written by the" in bounded and "read_document" in bounded


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
