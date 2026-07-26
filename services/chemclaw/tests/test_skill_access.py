"""Role-scoped skill visibility gates advertised skills by the turn's ambient roles (Phase 6).

Proves the RBAC seam: with no gates every skill is visible (today's behavior); a gated skill is
hidden from a caller (the ambient identity) holding none of its roles and shown to one holding a
role; ungated skills are unaffected. Roles come from `agents.identity_context`, so the front door
never threads identity through `build_agent`.
"""

import asyncio
from typing import cast

from agent_framework import FileSkillsSource, SkillsSourceContext
from agent_framework._agents import SupportsAgentRun

from agents.identity_context import reset_current_identity, set_current_identity
from agents.skill_access import RoleScopedSkillsSource
from chemclaw.config import settings


def _skill_names(
    gates: dict[str, list[str]] | None, roles: frozenset[str] | None = None
) -> set[str]:
    """Names advertised under a gate map, evaluated as a caller holding `roles` (None = no user)."""
    source = RoleScopedSkillsSource(FileSkillsSource(settings.skills_dirs), gates)
    # The file source ignores the context's agent; a cast keeps the stand-in strictly typed.
    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))
    token = set_current_identity("u-1", roles) if roles is not None else None
    try:
        skills = asyncio.run(source.get_skills(context))
    finally:
        if token is not None:
            reset_current_identity(token)
    return {skill.frontmatter.name for skill in skills}


def test_no_gates_advertises_every_skill() -> None:
    """The default (empty gate map) is unfiltered — all skills stay visible."""
    unfiltered = _skill_names({})
    assert "deep-research" in unfiltered
    assert len(unfiltered) > 1


def test_gated_skill_hidden_from_caller_lacking_the_role() -> None:
    """A gated skill is dropped for a caller (and an anonymous turn) holding none of its roles."""
    gates = {"deep-research": ["process-chemist"]}
    # Anonymous: no ambient identity at all.
    assert "deep-research" not in _skill_names(gates)
    # Authenticated but without the required role.
    assert "deep-research" not in _skill_names(gates, roles=frozenset({"viewer"}))


def test_gated_skill_shown_to_caller_holding_the_role() -> None:
    """A gated skill is advertised to a caller holding one of its allowed roles."""
    gates = {"deep-research": ["process-chemist"]}
    assert "deep-research" in _skill_names(gates, roles=frozenset({"process-chemist"}))


def test_ungated_skills_are_unaffected_by_gates() -> None:
    """Gating one skill never hides the others — only the gated name is scoped."""
    all_skills = _skill_names({})
    gated = _skill_names({"deep-research": ["process-chemist"]}, roles=frozenset({"viewer"}))
    assert gated == all_skills - {"deep-research"}
