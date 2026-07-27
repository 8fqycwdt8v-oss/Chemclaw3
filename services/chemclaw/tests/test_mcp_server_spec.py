"""The `McpServerSpec` transport union (config-extensibility item 6).

Proves a remote MCP server configures as cleanly as a local one without breaking a single existing
config: an untagged entry (every config written before the union) still parses as stdio, an explicit
tag selects its variant, `_mcp_tool` builds the matching MAF transport, and the `allowed_tools`
boundary that keeps write/index tools off the agent is identical on both transports. No subprocess
and no network — construction is lazy, so this is pure config/dispatch. See
`docs/audit/10-config-extensibility.md` §9 item 6.
"""

from typing import Any

import pytest
from agent_framework import MCPStdioTool, MCPStreamableHTTPTool

from agents.chemclaw_agent import _mcp_tool
from chemclaw.config import HttpMcpServerSpec, Settings, StdioMcpServerSpec


def test_untagged_entry_still_parses_as_stdio() -> None:
    """The backwards-compatibility guarantee: a config predating `transport` keeps working."""
    # A raw env-shaped payload: exactly what `CHEMCLAW_MCP_SERVERS` JSON deserializes to.
    raw: list[Any] = [
        {"name": "legacy", "command": "python", "args": ["-m", "x"], "allowed_tools": ["a"]}
    ]
    settings = Settings(_env_file=None, mcp_servers=raw)  # type: ignore[call-arg]
    (spec,) = settings.mcp_servers
    assert isinstance(spec, StdioMcpServerSpec)
    assert spec.transport == "stdio"
    assert spec.allowed_tools == ["a"]


def test_explicit_tags_select_their_variants() -> None:
    """`transport` picks the variant; an http entry needs no `command`/`args` at all."""
    raw: list[Any] = [
        {"transport": "stdio", "name": "local", "command": "python", "args": ["-m", "x"]},
        {"transport": "http", "name": "remote", "url": "https://mcp.internal/mcp"},
    ]
    settings = Settings(_env_file=None, mcp_servers=raw)  # type: ignore[call-arg]
    local, remote = settings.mcp_servers
    assert isinstance(local, StdioMcpServerSpec)
    assert isinstance(remote, HttpMcpServerSpec)
    assert remote.url == "https://mcp.internal/mcp"


def test_mcp_tool_builds_the_matching_transport() -> None:
    """Dispatch: a stdio spec builds `MCPStdioTool`, an http spec builds `MCPStreamableHTTPTool`."""
    stdio = _mcp_tool(StdioMcpServerSpec(name="local", command="python", args=["-m", "x"]))
    http = _mcp_tool(HttpMcpServerSpec(name="remote", url="https://mcp.internal/mcp"))
    assert isinstance(stdio, MCPStdioTool)
    assert isinstance(http, MCPStreamableHTTPTool)
    assert stdio.name == "local"
    assert http.name == "remote"


def test_allowed_tools_boundary_is_transport_independent() -> None:
    """The PR-gate boundary is transport-independent: both carry the same agent-facing subset."""
    allowed = ["similar_molecules"]
    stdio = _mcp_tool(
        StdioMcpServerSpec(name="l", command="python", args=["-m", "x"], allowed_tools=allowed)
    )
    http = _mcp_tool(
        HttpMcpServerSpec(name="r", url="https://mcp.internal/mcp", allowed_tools=allowed)
    )
    assert set(stdio.allowed_tools or []) == set(allowed)
    assert set(http.allowed_tools or []) == set(allowed)


def test_wrong_field_for_the_chosen_transport_is_rejected() -> None:
    """`extra="forbid"`: a stdio field on an http entry is a config error, not a silent drop."""
    raw: list[Any] = [
        {"transport": "http", "name": "r", "url": "https://x/mcp", "command": "python"}
    ]
    with pytest.raises(ValueError):
        Settings(_env_file=None, mcp_servers=raw)  # type: ignore[call-arg]


def test_unknown_transport_is_rejected() -> None:
    """An unknown transport tag fails loud rather than falling back to a default variant."""
    raw: list[Any] = [{"transport": "carrier-pigeon", "name": "r", "url": "https://x/mcp"}]
    with pytest.raises(ValueError):
        Settings(_env_file=None, mcp_servers=raw)  # type: ignore[call-arg]


def test_defaults_are_stdio_and_unchanged() -> None:
    """The two shipped capability servers stay stdio with their pinned allowed_tools."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert {s.name for s in settings.mcp_servers} == {"mcp-molfp", "mcp-rxnfp", "mcp-calc"}
    assert all(isinstance(s, StdioMcpServerSpec) for s in settings.mcp_servers)
    assert all(s.allowed_tools for s in settings.mcp_servers)  # the boundary is still pinned
