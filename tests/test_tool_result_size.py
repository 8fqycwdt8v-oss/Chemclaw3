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
from langchain_core.messages import ToolMessage

from chemclaw.agent.tool_result_size import bound_tool_results, bounded_content
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
    """The two attributes `bound_tool_results` reads off a tool-call request."""

    def __init__(self, name: str) -> None:
        """Name the tool this request is for; nothing else about it is read."""
        self.tool_call = {"name": name, "args": {}, "id": "c1"}
