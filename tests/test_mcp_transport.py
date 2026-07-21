"""The agent's MCP capability servers connect over stdio and expose the right tools.

This is the real transport verification for the MCP migration (D-029): it spawns each
configured server as MAF's `MCPStdioTool` does, connects over stdio, and asserts the server
advertises exactly the `allowed_tools` the agent is restricted to — proving config → subprocess
→ tool discovery + the index/write tools stay out of the agent. Tool *discovery* (`list_tools`)
needs no database, so this runs in the sandbox; actually *invoking* a tool needs Postgres and
is exercised in CI (`test_molfp_postgres.py` / `test_rxnfp_postgres.py`) against the same code.

Each connection spawns a Python subprocess that imports RDKit, so these are slow but real.
"""

import asyncio

import pytest
from agent_framework import MCPStdioTool

from agents.chemclaw_agent import _mcp_tool
from chemclaw.config import McpServerSpec, settings


async def _discovered_tools(spec: McpServerSpec) -> set[str]:
    """Spawn the server for `spec`, connect over stdio, and return the tool names it exposes."""
    tool: MCPStdioTool = _mcp_tool(spec)
    try:
        async with tool:
            return {f.name for f in tool.functions}
    except (FileNotFoundError, ImportError) as exc:  # pragma: no cover - toolchain absent
        # Skip ONLY when the toolchain itself is missing (no python/git on PATH, or
        # RDKit not importable). Any other failure — a server that starts but crashes,
        # or advertises the wrong tools — must fail loudly: this is the one test guarding
        # the `allowed_tools` boundary that keeps write/index tools off the agent (D-029).
        pytest.skip(f"MCP server toolchain unavailable in this environment: {exc}")


@pytest.mark.parametrize("spec", settings.mcp_servers, ids=lambda s: s.name)
def test_server_exposes_only_its_allowed_tools(spec: McpServerSpec) -> None:
    """Each configured server connects and advertises exactly its `allowed_tools`.

    The `allowed_tools` restriction is what keeps the write/index tools (`index_molecule`,
    `index_reaction`) off the conversational agent — ingestion writes go through the PR-gate.
    """
    assert spec.allowed_tools is not None  # every capability server pins its agent-facing tools
    discovered = asyncio.run(_discovered_tools(spec))
    assert discovered == set(spec.allowed_tools)
