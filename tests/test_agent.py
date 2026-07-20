"""The agent wires its tools and skills correctly (plan step 1.5).

Built with an injected dummy client so no LLM credentials are needed — this
proves the MAF wiring (tools advertised, skills discovered), not model behavior.
"""

from agents.chemclaw_agent import build_agent


def test_agent_advertises_qm_tools() -> None:
    """Both QM tools are registered on the agent under their function names."""
    agent = build_agent(chat_client=object())
    tool_names = {tool.name for tool in agent.default_options["tools"]}
    assert {"compute_xtb_energy", "submit_qm_job", "get_qm_job_status"} <= tool_names


def test_agent_has_a_skills_provider() -> None:
    """A skills provider is attached so SKILL.md judgment is available on demand."""
    agent = build_agent(chat_client=object())
    provider_types = {type(p).__name__ for p in agent.context_providers}
    assert "SkillsProvider" in provider_types
