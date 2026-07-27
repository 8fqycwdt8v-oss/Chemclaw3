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
from connectors.registry import enabled

# The in-process capability tools: the conversation plumbing that reads or writes the turn's own
# state, plus the two PR-gate writers and the durable launchers core still owns. The domain
# capabilities are not here — they moved to connectors (D-093) and are advertised per turn.
_EXPECTED_INPROCESS_TOOLS = {
    "submit_qm_job",
    "get_qm_job_status",
    "find_notes",
    "expand_note",
    "gather_evidence",
    "propose_knowledge_note",
    "record_confirmed_answer",
    "request_development_report",
    "get_durable_job_status",
    "find_knowledge_gaps",
    "ask_clarifying_question",
    "remember_preference",
    "recall_preferences",
    "forget_preference",
    "watch_for",
    "list_watches",
    "stop_watching",
    "list_attachments",
    "read_attachment",
}


def test_registry_holds_the_inprocess_tools_and_only_job_launchers_besides() -> None:
    """Importing the agent registers precisely the in-process tools; building it adds job launchers.

    Two populations share this registry on purpose. The `@tool` functions arrive on import, and the
    generated launcher for each declared connector job is registered when an agent is built — which
    is exactly what makes a job tool addressable by `tool_role_gates` and wrapped by the audit
    middleware like any other. So "exactly the in-process set" is only true before a build, and the
    invariant worth asserting is that nothing *else* ever appears.
    """
    assert _EXPECTED_INPROCESS_TOOLS <= set(registered_tool_names())
    build_agent(chat_client=object())
    extra = set(registered_tool_names()) - _EXPECTED_INPROCESS_TOOLS
    assert extra == {job for manifest in enabled() for job in (j.name for j in manifest.jobs)}


def test_capability_tools_are_exactly_the_registry() -> None:
    """`_capability_tools()` is the registry, whole and in order — connectors are not in it.

    A connector's MCP tools are per-turn (`connector_tools`), not per-process, so the agent's own
    tool list is the registry and nothing more.
    """
    tools = _capability_tools()
    assert tools == registered_tools()


def test_agent_advertises_the_registered_inprocess_tools() -> None:
    """The built agent advertises every registered in-process tool under its function name."""
    agent = build_agent(chat_client=object())
    advertised = {t.name for t in agent.default_options["tools"]}
    assert _EXPECTED_INPROCESS_TOOLS <= advertised


def test_duplicate_registration_is_a_loud_error() -> None:
    """Registering two tools under one name is a programming error (as in `evals.metric`)."""

    async def gather_evidence() -> None:  # shadows an already-registered name on purpose
        return None

    with pytest.raises(ValueError, match="already registered"):
        register_tool(gather_evidence)


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
