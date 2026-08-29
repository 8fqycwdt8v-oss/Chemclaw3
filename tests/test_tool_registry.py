"""The capability-tool registry seam (config-extensibility item 2).

Proves the `@tool` registry replaced the hardcoded `_capability_tools()` list without changing the
advertised toolset or the safety wiring: registration is by function name, duplicates are a loud
programming error, and the agent still advertises exactly the same in-process tools wrapped by the
same audit+authz middleware. See `docs/archive/audit/10-config-extensibility.md` §5/§8 (Spike 1).
"""

import pytest

from chemclaw.agent.chemclaw_agent import _capability_tools
from chemclaw.connectors.registry import enabled
from chemclaw.core.tool_registry import (
    _REGISTRY,
    register_tool,
    registered_tool_names,
    registered_tools,
    tool,
)
from chemclaw.templates.registry import template_tool_names
from tests.surface import surface

# Every in-process capability tool, spelled out: the registry must reproduce this set, no more and
# no less (a connector's tools are advertised separately, per turn). Adding one is a deliberate,
# reviewed edit here rather than a silent widening of what the agent can do — and the review that
# edit invites is "should this be a connector tool instead?", which is the point.
_EXPECTED_INPROCESS_TOOLS = {
    # The conversation plumbing: everything that reads or writes the *turn's own* state, which is
    # by definition unavailable to another process.
    "ask_clarifying_question",
    "list_attachments",
    "read_attachment",
    "watch_for",
    "list_watches",
    "stop_watching",
    "remember_preference",
    "recall_preferences",
    "forget_preference",
    # The knowledge layer: reads, plus the two PR-gate writers. The gate is core's, so its writers
    # are too — a connector reaches it only by returning a note in a job envelope.
    "find_notes",
    "expand_note",
    "gather_evidence",
    "condense_protocols",
    "find_knowledge_gaps",
    "propose_knowledge_note",
    "record_confirmed_answer",
    "record_failure",
    "recall_observations",
    # The durable launchers core still owns, and the one status tool every durable job is
    # collected with, connector-owned or not. The QM launcher and its bespoke status tool were the
    # last pair to go; every expensive job is a declared connector job now (D-118).
    #
    # The report's workflow has not moved into a bundle (D-115: its closure *is* core's).
    # `synthesize_memory` is core's for the same reason and one more: D-2026-08-25 took the corpus
    # miners' Schedules away so that no timer opens a pull request, which left four registered
    # workflows with no caller at all — this is the trigger that replaced the clock, and a person
    # asking is now the only thing that starts one.
    "request_development_report",
    "synthesize_memory",
    "get_durable_job_status",
    # The retrospective half of that pair (D-157): the durable record of every finished run, which
    # is core's for the same reason the status tool is — it is generic over every job, and a
    # connector must not be able to see another bundle's runs.
    "find_past_jobs",
    # The operational read model (D-2026-08-29). In-process for the same reason `find_past_jobs`
    # is: it is generic over every capability, and a connector bundle must not be able to read
    # another bundle's record — nor, being a projection of the audit trail itself, may the
    # capability that writes that trail be the thing that reads it back.
    "review_activity",
}


def test_registry_holds_the_inprocess_tools_and_only_generated_launchers_besides() -> None:
    """Importing the agent registers precisely the in-process tools; building it adds launchers.

    Three populations share this registry on purpose. The `@tool` functions arrive on import; the
    generated launcher for each declared connector job and each enabled step template is registered
    when an agent is built — which is exactly what makes a generated tool addressable by
    `tool_role_gates` and wrapped by the audit middleware like any other. So "exactly the
    in-process set" is only true before a build, and the invariant worth asserting is that nothing
    *else* ever appears.
    """
    assert _EXPECTED_INPROCESS_TOOLS <= set(registered_tool_names())
    surface(None)
    extra = set(registered_tool_names()) - _EXPECTED_INPROCESS_TOOLS
    jobs = {job.name for manifest in enabled() for job in manifest.jobs}
    assert extra == jobs | set(template_tool_names())


def test_capability_tools_are_exactly_the_registry() -> None:
    """`_capability_tools()` is the registry, whole and in order — connectors are not in it.

    A connector's MCP tools are per-turn (`connector_tools`), not per-process, so the agent's own
    tool list is the registry and nothing more.
    """
    tools = _capability_tools()
    assert tools == registered_tools()


def test_agent_advertises_the_registered_inprocess_tools() -> None:
    """The built agent advertises every registered in-process tool under its function name."""
    agent = surface(None)
    advertised = agent.tool_names
    assert _EXPECTED_INPROCESS_TOOLS <= advertised


def test_duplicate_registration_is_a_loud_error() -> None:
    """Registering two tools under one name is a programming error (as in `evals.metric`)."""

    async def gather_evidence() -> None:  # shadows an always-registered name on purpose
        return None

    with pytest.raises(ValueError, match="already registered"):
        register_tool(gather_evidence)


def test_decorator_registers_and_returns_function_unchanged() -> None:
    """`@tool` registers by name and returns the same object the framework wraps (identity)."""
    try:

        @tool
        async def _probe_only_tool() -> int:
            return 7

        assert "_probe_only_tool" in registered_tool_names()
        assert _probe_only_tool.__name__ == "_probe_only_tool"  # unchanged by the decorator
    finally:
        _REGISTRY.pop("_probe_only_tool", None)  # keep the module-global registry clean for others
