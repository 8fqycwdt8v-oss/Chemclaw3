"""The capability-tool registry seam (config-extensibility item 2).

Proves the `@tool` registry replaced the hardcoded `_capability_tools()` list without changing
the advertised toolset or the safety wiring: registration is by function name, duplicates are a
loud programming error, and the agent still advertises exactly the same in-process tools wrapped
by the same audit+authz middleware. See `docs/audit/10-config-extensibility.md` §5/§8 (Spike 1).
"""

import pytest

from agents.chemclaw_agent import _capability_tools, build_agent
from agents.tool_registry import (
    _REGISTRY,
    register_tool,
    registered_tool_names,
    registered_tools,
    tool,
)

# The exact in-process capability tools the old hardcoded list advertised — the registry must
# reproduce this set, no more and no less (the MCP servers are advertised separately).
_EXPECTED_INPROCESS_TOOLS = {
    "compute_xtb_energy",
    "predict_solubility",
    "predict_pka",
    "submit_qm_job",
    "get_qm_job_status",
    "find_notes",
    "expand_note",
    "gather_evidence",
    "suggest_next_experiment",
    "propose_knowledge_note",
    "record_confirmed_answer",
}


def test_registry_holds_exactly_the_inprocess_tools() -> None:
    """Importing the agent populates the registry with precisely the in-process capability tools."""
    assert set(registered_tool_names()) == _EXPECTED_INPROCESS_TOOLS


def test_capability_tools_match_registry_plus_mcp() -> None:
    """`_capability_tools()` is the registered function tools, then the MCP capability tools."""
    from agents.chemclaw_agent import _mcp_capability_tools

    tools = _capability_tools()
    inprocess = registered_tools()
    # The in-process tools appear first, in registration order, unchanged.
    assert tools[: len(inprocess)] == inprocess
    # The tail is exactly the config-driven MCP capability servers.
    assert len(tools) == len(inprocess) + len(_mcp_capability_tools())


def test_agent_advertises_the_registered_inprocess_tools() -> None:
    """The built agent advertises every registered in-process tool under its function name."""
    agent = build_agent(chat_client=object())
    advertised = {t.name for t in agent.default_options["tools"]}
    assert _EXPECTED_INPROCESS_TOOLS <= advertised


def test_duplicate_registration_is_a_loud_error() -> None:
    """Registering two tools under one name is a programming error (as in `evals.metric`)."""

    async def compute_xtb_energy() -> None:  # shadows an already-registered name on purpose
        return None

    with pytest.raises(ValueError, match="already registered"):
        register_tool(compute_xtb_energy)


def test_decorator_registers_and_returns_function_unchanged() -> None:
    """`@tool` registers by name and returns the same object MAF will wrap (identity)."""
    try:

        @tool
        async def _probe_only_tool() -> int:
            return 7

        assert "_probe_only_tool" in registered_tool_names()
        assert _probe_only_tool.__name__ == "_probe_only_tool"  # unchanged by the decorator
    finally:
        _REGISTRY.pop("_probe_only_tool", None)  # keep the module-global registry clean for others
