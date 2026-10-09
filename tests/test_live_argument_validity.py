"""First-call argument validity: the harness recognises the two ways a schema failure is worded.

A model-text evaluation reads `ProbeOutcome.first_call_argument_errors`. The recogniser matches on
text, so these tests drive the two real producers (LangChain's tool node and an in-repo MCP
server) and fail when an upstream rewording would turn a schema failure into an unclassified one.
"""

import asyncio
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from chemclaw.agent.tool_authz import returned_failure_detail
from chemclaw.connectors.registry import server_tools_module
from chemclaw.evals.live import ARGUMENT_ERROR
from tests.test_live_probes import _probe, _run


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


class _ToolCalling(GenericFakeChatModel):
    """A fake chat model that can be bound to tools (it never reads them)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Stay unbound: the script below is the model."""
        return self


def test_langchain_reports_a_schema_failure_in_the_words_the_harness_matches() -> None:
    """An in-process tool called with arguments that do not fit comes back as a returned failure."""
    seen: list[str] = []

    @wrap_tool_call
    async def observe(request: Any, handler: Any) -> Any:
        result = await handler(request)
        seen.append(returned_failure_detail(result))
        return result

    call = AIMessage(content="", tool_calls=[{"name": "add", "args": {"a": "x"}, "id": "1"}])
    model = _ToolCalling(messages=iter([call, AIMessage(content="done")]))
    agent = create_agent(model, [add], middleware=[observe])
    asyncio.run(agent.ainvoke({"messages": [HumanMessage("add")]}))
    assert len(seen) == 1
    assert ARGUMENT_ERROR.search(seen[0]), seen[0]


def test_an_in_repo_mcp_server_reports_a_schema_failure_in_the_words_the_harness_matches() -> None:
    """A connector tool called without its required argument fails in pydantic's words."""
    module = server_tools_module("molfp")
    assert module is not None

    async def call() -> str:
        async with create_connected_server_and_client_session(module.server) as session:
            first = (await session.list_tools()).tools[0]
            tools = {one.name: one for one in await load_mcp_tools(session)}
            result = await tools[first.name].ainvoke({"not_an_argument": 1})
            return str(result)

    assert ARGUMENT_ERROR.search(asyncio.run(call()))


@pytest.mark.parametrize(
    "message",
    [
        "worker unreachable",
        "ConnectorError: the connector serving rank_species did not answer",
        "Refused: that tool needs approval",
    ],
)
def test_a_failure_that_is_not_about_arguments_is_not_one(message: str) -> None:
    """Only a schema failure counts against a description."""
    assert ARGUMENT_ERROR.search(message) is None


_INVALID = "Error invoking tool 'rank_species' with kwargs {} with error:\n smiles: Field required"


def test_a_first_call_that_fails_its_schema_is_recorded_once() -> None:
    """The retry that follows is the model reading the error, not the description's doing."""
    outcome = _run(
        _probe(),
        {"type": "tool_call", "tool": "rank_species", "arguments": "{}"},
        {"type": "tool_failed", "tool": "rank_species", "message": _INVALID},
        {"type": "tool_call", "tool": "rank_species", "arguments": "{}"},
        {"type": "tool_failed", "tool": "rank_species", "message": _INVALID},
        {"type": "answer", "text": "could not run it"},
    )
    assert outcome.first_calls == 1
    assert outcome.first_call_argument_errors == ["rank_species"]


def test_a_schema_failure_after_a_retry_is_not_a_first_call() -> None:
    """Two calls were issued before the failure arrived, so it cannot be attributed to the first."""
    outcome = _run(
        _probe(),
        {"type": "tool_call", "tool": "rank_species", "arguments": "{}"},
        {"type": "tool_call", "tool": "rank_species", "arguments": "{}"},
        {"type": "tool_failed", "tool": "rank_species", "message": _INVALID},
        {"type": "answer", "text": "a"},
    )
    assert outcome.first_calls == 1
    assert outcome.first_call_argument_errors == []


def test_a_valid_first_call_and_a_gate_refusal_are_not_argument_errors() -> None:
    """A refusal carries a reason and is the gate working; an ordinary failure is not a schema's."""
    outcome = _run(
        _probe(),
        {"type": "tool_call", "tool": "screen_hazards", "arguments": "{}"},
        {"type": "tool_failed", "tool": "screen_hazards", "message": "worker unreachable"},
        {"type": "tool_call", "tool": "start_optimization_campaign", "arguments": "{}"},
        {
            "type": "tool_failed",
            "tool": "start_optimization_campaign",
            "message": _INVALID,
            "reason": "plan_gate",
        },
        {"type": "answer", "text": "a"},
    )
    assert outcome.first_calls == 2
    assert outcome.first_call_argument_errors == []


def test_a_transcript_written_before_the_field_existed_still_loads() -> None:
    """The recording is additive: an older outcome has neither field and reads as zero."""
    from chemclaw.evals.live import ProbeOutcome

    older = ProbeOutcome(
        probe_id="p", section=1, persona="lab_technician", bucket="A", question="q"
    )
    assert older.first_calls == 0
    assert older.first_call_argument_errors == []
