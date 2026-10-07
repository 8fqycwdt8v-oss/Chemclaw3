"""A template's `tool` step fails when its tool returns a failure.

`invoke_governed` omits the model-facing converters, but an MCP tool does not raise:
`langchain_mcp_adapters` converts a server's `isError=True` through `handle_tool_error` into an
ordinary return. The tools here use that same callback, so the test exercises the real mechanism.
"""

import asyncio

import pytest
from langchain_core.tools import StructuredTool, ToolException

from chemclaw.agent.profiles import get_profile
from chemclaw.agent.tool_invocation import ToolReturnedFailure, invoke_governed


def _answering_tool(name: str, *, fails: bool) -> StructuredTool:
    """A tool shaped like a connector's: it reports failure by returning, never by raising."""

    async def _body() -> str:
        # `ToolException`, because LangChain routes only that class to `handle_tool_error`, as the
        # MCP adapter's own error class does; a `RuntimeError` would propagate instead.
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
    """A tool that returns a failure fails the step.

    Otherwise the error text becomes `${steps.<id>.result}`, is interpolated into later steps'
    arguments, and a `job` step launches durable work on it while the step reads as done.
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
    """A server that answered will answer the same way again, so the failure is non-retryable.

    `durable/publish.py` matches `non_retryable_error_types` against the class name, not its bases.
    """
    from chemclaw.durable.publish import _BAD_DATA_TYPES

    assert ToolReturnedFailure.__name__ in _BAD_DATA_TYPES


def test_the_trail_records_a_returned_failure_as_a_failure() -> None:
    """The audit trail records a returned failure as a failure.

    `audit._recording` decides the outcome via the `isinstance`-based `returned_failure(result)`, so
    the failure must arrive as a recognisable type, not a bare `str` recorded as `ok`.
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
