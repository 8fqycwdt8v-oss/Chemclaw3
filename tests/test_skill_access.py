"""Role-aware skill visibility filters advertised skills (Phase-6 seam).

Proves the seam that Phase 6 turns into real RBAC: by default every skill is visible
(today's behavior), and a supplied allowed-set restricts what the agent advertises — a value
change at the call site, not a new mechanism.
"""

import asyncio
from typing import cast

from agent_framework import FileSkillsSource, SkillsSourceContext
from agent_framework._agents import SupportsAgentRun

from agents.skill_access import RoleFilteredSkillsSource
from chemclaw.config import settings


def _skill_names(allowed: set[str] | None) -> set[str]:
    """Names advertised by the file skills source under an allowed-set (or None = all)."""
    source = RoleFilteredSkillsSource(FileSkillsSource(settings.skills_dirs), allowed)
    # The file source ignores the context's agent; a cast keeps the stand-in strictly typed.
    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))
    skills = asyncio.run(source.get_skills(context))
    return {skill.frontmatter.name for skill in skills}


def test_none_advertises_every_skill() -> None:
    """The default (allowed=None) is unfiltered — all skills stay visible."""
    unfiltered = _skill_names(None)
    assert "deep-research" in unfiltered
    assert len(unfiltered) > 1


def test_allowed_set_restricts_visible_skills() -> None:
    """An allowed-set advertises only those skills, dropping the rest."""
    visible = _skill_names({"deep-research"})
    assert visible == {"deep-research"}


def test_unknown_allowed_name_yields_nothing() -> None:
    """An allowed-set naming no real skill advertises nothing (fail-closed, not open)."""
    assert _skill_names({"no-such-skill"}) == set()
