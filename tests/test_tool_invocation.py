"""A template's `tool` step, and the one way a tool failure used to become a step's answer.

`agent/tool_invocation.invoke_governed` deliberately omits the two model-facing converters a chat
turn gets: a template step has no model, and converting a refusal there made a refused `job` step
return the refusal as its payload and launch anyway. That reasoning is right and it left a hole,
because it assumed a failure *raises*. An MCP tool does not — `langchain_mcp_adapters` builds every
connector tool with a `handle_tool_error` callback, so a server reporting `isError=True` is
converted inside `StructuredTool.ainvoke` and comes back as an ordinary return.

The tools here are built with that same callback rather than with a hand-made `ToolMessage`, because
the mechanism *is* the finding: asserting against an invented shape is how this repository has
repeatedly proved a property the engine does not have.
"""

import asyncio

import pytest
from langchain_core.tools import StructuredTool, ToolException

from chemclaw.agent.profiles import get_profile
from chemclaw.agent.tool_invocation import ToolReturnedFailure, invoke_governed


def _answering_tool(name: str, *, fails: bool) -> StructuredTool:
    """A tool shaped like a connector's: it reports failure by returning, never by raising."""

    async def _body() -> str:
        # `ToolException`, because LangChain routes only that class (and subclasses) to
        # `handle_tool_error` — `langchain_mcp_adapters` raises `_MCPToolExecutionError`, which is
        # one. A `RuntimeError` here would propagate instead, and the test would prove a different
        # thing than the one that bites.
        if fails:
            raise ToolException("the instrument is offline")
        return "42.0 kcal/mol"

    return StructuredTool.from_function(
        coroutine=_body,
        name=name,
        description="a connector tool",
        handle_tool_error=lambda exc: f"Error: {exc}",
    )


def _run(tool: StructuredTool) -> object:
    """Drive one governed call the way `durable/template_activities` does."""
    return asyncio.run(
        invoke_governed(
            tool,
            {},
            correlation_id="corr-1",
            actor="",
            profile=get_profile(None),
        )
    )


def test_a_tool_that_returns_a_failure_fails_the_step() -> None:
    """The defect: the error sentence became `${steps.<id>.result}` and the workflow read success.

    Without this, a connector answering `isError=True` had its message interpolated into the next
    step's arguments — including a `job` step's — while the template recorded the step as done. The
    failure is silent in exactly the way that matters: nothing raises, nothing is logged as wrong,
    and a later step launches durable work on the string "the instrument is offline".
    """
    with pytest.raises(ToolReturnedFailure) as raised:
        _run(_answering_tool("screen_hazards", fails=True))

    assert "the instrument is offline" in str(raised.value), (
        "the step's failure must carry what the tool actually said, or a chemist reading the "
        "workflow's history cannot tell which tool refused or why"
    )


def test_a_tool_that_succeeds_still_returns_its_value() -> None:
    """The other direction, so the guard above cannot be satisfied by failing everything."""
    assert _run(_answering_tool("compute_energy", fails=False)) == "42.0 kcal/mol"


def test_the_failure_is_non_retryable_to_temporal() -> None:
    """A server that answered will answer the same way again.

    `durable/publish.py` matches `non_retryable_error_types` against the exception's *class name*,
    not its bases — so inheriting `ChemclawError`, which is on that list, buys nothing. An entry
    that is missing is a retry storm against a tool that has already given its verdict.
    """
    from chemclaw.durable.publish import _BAD_DATA_TYPES

    assert ToolReturnedFailure.__name__ in _BAD_DATA_TYPES


def test_the_trail_records_a_returned_failure_as_a_failure() -> None:
    """The half of this defect that leaves no trace at all.

    `audit._recording` decides the outcome from `returned_failure(result)`, which is
    `isinstance`-based. On this path a refused connector call used to arrive as a bare `str`, so the
    check saw nothing and the row was written `ok` — a tool that refused, recorded as a tool that
    answered. The step's wrong result is at least visible in the workflow's history; this was not
    visible anywhere.
    """
    rows: list[object] = []

    class _Sink:
        """A sink that keeps what it is handed, so the outcome can be read back."""

        async def record(self, event: object) -> None:
            """Keep one audit event."""
            rows.append(event)

    async def _drive() -> None:
        await invoke_governed(
            _answering_tool("screen_hazards", fails=True),
            {},
            correlation_id="corr-2",
            actor="",
            profile=get_profile(None),
            sink=_Sink(),
        )

    with pytest.raises(ToolReturnedFailure):
        asyncio.run(_drive())

    assert rows, "a governed call must leave a row whatever its outcome"
    assert [getattr(row, "outcome", None) for row in rows] == ["error"], (
        "a tool that reported failure was recorded as a successful call"
    )
