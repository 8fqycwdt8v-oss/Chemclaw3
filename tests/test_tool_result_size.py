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
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from chemclaw.agent.audit import NullAuditSink, make_audit_middleware
from chemclaw.agent.langgraph_agent import tool_call_middleware
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.tool_result_size import bound_tool_results, bounded_content
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.middleware import tool_request


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
    assert removed == len(content) - 1_000


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

    assert removed == 78_000
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
    # A registered tool, because that is the shape `ToolNode` builds for a name the graph holds —
    # and it is what `metric_tool_name` reads the counter's label off.
    request = tool_request(
        "find_calculations", tool=SimpleNamespace(name="find_calculations", metadata={})
    )

    async def handler(_: Any) -> ToolMessage:
        return ToolMessage(content="y" * 200_000, tool_call_id="c1", name="find_calculations")

    before = METRICS.value("chemclaw_tool_results_truncated_total")
    result = asyncio.run(bound_tool_results.awrap_tool_call(request, handler))

    assert isinstance(result, ToolMessage)
    assert len(result.content) < 200_000
    assert METRICS.value("chemclaw_tool_results_truncated_total") > before
    assert 'tool="find_calculations"' in METRICS.render()


def test_a_name_the_graph_never_held_cannot_become_a_metric_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label is the *registered* tool's name, never the string the model emitted.

    **Measured on a compiled graph before this was pinned.** With the ceiling lowered — it is
    `ge=0` and ENV-overridable, so every legal value has to hold — a scripted model calling a name
    the graph does not hold produced
    `chemclaw_tool_results_truncated_total{tool="made_up_yyyy…"} 1`: `ToolNode` answers an
    unregistered name with its own 1,061-character "not a valid tool, try one of […]" message,
    which is a `ToolMessage` like any other and is bounded like any other. One permanent time
    series per string a model invents, on an endpoint that is unauthenticated by design.

    `core/metrics.py` declared this label as reading "the request's tool name, which is the one the
    graph dispatched". It read `request.tool_call["name"]` — the *call's* name, which is whatever
    the model emitted. `metric_tool_name` reads the registered tool object, which is the sentence
    that was already true of the two counters beside it.
    """
    monkeypatch.setattr(settings, "agent_max_tool_result_chars", 50)
    invented = "made_up_" + "y" * 40

    async def handler(_: Any) -> ToolMessage:
        return ToolMessage(content="z" * 5_000, tool_call_id="c1", name=invented)

    # `tool=None` is what `ToolNode` passes for a name the graph does not hold, which is the case
    # under test rather than a convenience of the fixture.
    asyncio.run(bound_tool_results.awrap_tool_call(tool_request(invented), handler))

    assert invented not in METRICS.render(), (
        "a tool name the model invented reached /metrics as a label value: one time series per "
        "string anything that can reach the pod can name"
    )


def test_the_cut_sits_inside_the_framer_and_outside_the_trail() -> None:
    """The two positional claims this middleware rests on, as relations rather than as a sequence.

    `tests/test_middleware_order.py` pins the compiled list, so any reorder turns it red — but a
    *deliberate* reorder is exactly the change that list is meant to let a reviewer adjudicate, and
    what survives it has to be stated somewhere. `tests/test_tool_framing.py` already writes the
    same three lines for the framer; this is the entry immediately below it, whose own docstring
    states two invariants that nothing but the sequence held.

    Inside `frame_connector_results`, because the framer wraps the payload in an envelope with a
    closing tag: cutting from outside it would take the tag off and leave the model a fragment it
    was told to read as delimited third-party data.

    Outside `audit_tool_calls` and `announce_tool_failures`, because both read the tool's *own*
    result — `audit_events.detail` is a record of what came back, not of what the model was shown.
    """
    audit = make_audit_middleware(correlation_id="c", actor="a", sink=NullAuditSink())
    names = [
        getattr(entry, "name", type(entry).__name__)
        for entry in tool_call_middleware(audit, get_profile(None))
    ]
    assert names.index("frame_connector_results") < names.index("bound_tool_results"), (
        "the cut is outside the envelope, so it can take the closing tag off"
    )
    for reader in ("audit_tool_calls", "announce_tool_failures"):
        assert names.index("bound_tool_results") < names.index(reader), (
            f"{reader} would read the truncated result as what the tool returned"
        )
