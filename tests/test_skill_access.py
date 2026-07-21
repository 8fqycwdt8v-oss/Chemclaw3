"""Role-scoped skill visibility (Phase-6 RBAC, plan step 6.2).

Proves the RBAC the seam became: with no gates every skill stays visible (today's behavior);
a gated skill is hidden from an anonymous caller and from one lacking its role, and shown to a
caller holding it. Ungated skills are always visible.
"""

import asyncio
from collections.abc import Mapping
from typing import cast

from agent_framework import FileSkillsSource, SkillsSourceContext
from agent_framework._agents import SupportsAgentRun

from agents.skill_access import RoleScopedSkillsSource
from chemclaw.config import settings
from chemclaw.identity import Principal


def _visible(
    gates: Mapping[str, list[str]] | None = None, principal: Principal | None = None
) -> set[str]:
    """Names advertised by the real file source under the given gates and caller."""
    source = RoleScopedSkillsSource(FileSkillsSource(settings.skills_dirs), gates, principal)
    # The file source ignores the context's agent; a cast keeps the stand-in strictly typed.
    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))
    skills = asyncio.run(source.get_skills(context))
    return {skill.frontmatter.name for skill in skills}


def test_no_gates_advertises_every_skill() -> None:
    """The default (no gates) is unfiltered — all skills stay visible for everyone."""
    unfiltered = _visible()
    assert "deep-research" in unfiltered
    assert len(unfiltered) > 1


def test_gated_skill_hidden_from_anonymous_caller() -> None:
    """A gated skill is not advertised to an anonymous caller (no principal → no roles)."""
    visible = _visible(gates={"deep-research": ["researcher"]}, principal=None)
    assert "deep-research" not in visible
    # Ungated skills are unaffected — only the gated one is withheld.
    assert len(visible) >= 1


def test_gated_skill_hidden_without_the_role() -> None:
    """A caller lacking the gate's role does not see the gated skill."""
    lab = Principal(oid="u1", roles=frozenset({"lab"}))
    assert "deep-research" not in _visible(gates={"deep-research": ["researcher"]}, principal=lab)


def test_gated_skill_shown_with_the_role() -> None:
    """A caller holding one of the gate's roles sees the gated skill."""
    researcher = Principal(oid="u2", roles=frozenset({"researcher"}))
    assert "deep-research" in _visible(
        gates={"deep-research": ["researcher"]}, principal=researcher
    )


def test_ungated_skills_stay_visible_under_gating() -> None:
    """Gating one skill does not hide the others (only listed skills are restricted)."""
    everyone = _visible()
    gated = _visible(gates={"deep-research": ["researcher"]}, principal=None)
    # Everything except the gated skill remains for an anonymous caller.
    assert gated == everyone - {"deep-research"}
