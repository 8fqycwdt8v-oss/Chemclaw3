"""The agent wires its tools and skills correctly (plan step 1.5; harness: D-020).

Built with an injected dummy client so no LLM credentials are needed — this
proves the MAF wiring (tools advertised, skills discovered, backbone selected),
not model behavior. No LLM call happens at construction in either backbone.
"""

import pytest

from agents.chemclaw_agent import build_agent
from chemclaw.config import settings

_DOMAIN_TOOLS = {
    "compute_xtb_energy",
    "predict_solubility",
    "predict_pka",
    "submit_qm_job",
    "get_qm_job_status",
    "find_notes",
    "expand_note",
    "propose_knowledge_note",
}


def test_agent_advertises_qm_tools() -> None:
    """All domain tools are registered on the agent under their function names."""
    agent = build_agent(chat_client=object())
    tool_names = {tool.name for tool in agent.default_options["tools"]}
    assert _DOMAIN_TOOLS <= tool_names


def test_agent_has_a_skills_provider() -> None:
    """A skills provider is attached so SKILL.md judgment is available on demand."""
    agent = build_agent(chat_client=object())
    provider_types = {type(p).__name__ for p in agent.context_providers}
    assert "SkillsProvider" in provider_types


def test_classic_is_the_default_backbone() -> None:
    """With the harness off (default), the agent is plain: no todo/mode providers."""
    provider_types = {type(p).__name__ for p in build_agent(chat_client=object()).context_providers}
    assert "SkillsProvider" in provider_types
    assert "TodoProvider" not in provider_types
    assert "AgentModeProvider" not in provider_types


def test_harness_adds_todo_and_mode_over_the_same_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness backbone adds the todo list + plan/execute mode, keeping the skills."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    provider_types = {type(p).__name__ for p in build_agent(chat_client=object()).context_providers}
    assert {"TodoProvider", "AgentModeProvider", "SkillsProvider"} <= provider_types


def test_harness_advertises_the_same_domain_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switching backbone does not change the agent's capability: same domain tools."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    tool_names = {tool.name for tool in build_agent(chat_client=object()).default_options["tools"]}
    assert _DOMAIN_TOOLS <= tool_names


def test_harness_disables_generic_sandbox_batteries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Governance (§6, G6): no generic file-memory/file-access provider is wired.

    Chemclaw's capability is its explicit tools/skills, never a generic filesystem or
    shell — so the harness's default file batteries must be off.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    provider_types = {type(p).__name__ for p in build_agent(chat_client=object()).context_providers}
    assert "FileMemoryProvider" not in provider_types
    assert "FileAccessProvider" not in provider_types
    assert "BackgroundAgentsProvider" not in provider_types


def test_plan_only_autonomy_has_no_completion_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan-only autonomy stays interactive: no AgentLoopMiddleware is attached."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    middleware = build_agent(chat_client=object()).middleware or []
    assert "AgentLoopMiddleware" not in {type(m).__name__ for m in middleware}


def test_execute_autonomy_wires_a_bounded_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute autonomy attaches the loop, bounded by the configured iteration cap."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 9)
    middleware = build_agent(chat_client=object()).middleware or []
    loops = [m for m in middleware if type(m).__name__ == "AgentLoopMiddleware"]
    assert len(loops) == 1
    assert getattr(loops[0], "max_iterations", None) == 9
