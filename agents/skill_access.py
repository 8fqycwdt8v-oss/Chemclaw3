"""Role-aware skill visibility — the Phase-6 seam for scoping advertised skills.

Today every skill is advertised to every user: `build_agent` loads all `SKILL.md` files, so
the model sees them all. Phase 6 scopes skills by the caller's Entra app-roles/groups (plan
step 6.2). This wrapper is that seam: it filters an inner `SkillsSource` to an allowed set of
skill names, defaulting to "all visible" so today's behavior is unchanged. Phase 6 resolves a
user's roles to the allowed name-set and passes it — a value change at the call site, not a
new mechanism. Kept as a thin decorator over any source (DRY), so the file source (and any
future source) gains role scoping without duplicating the filter.
"""

from agent_framework import Skill, SkillsSource, SkillsSourceContext


class RoleFilteredSkillsSource(SkillsSource):
    """Wrap a `SkillsSource`, advertising only skills whose name is in `allowed`.

    `allowed=None` passes every skill through (the default, unfiltered behavior). A concrete
    set restricts the advertised skills to those names — the role→skill scoping Phase 6 wants.
    """

    def __init__(self, inner: SkillsSource, allowed: set[str] | None = None) -> None:
        """Wrap `inner`; keep only skills named in `allowed` (or all of them when None)."""
        self._inner = inner
        self._allowed = allowed

    async def get_skills(self, context: SkillsSourceContext) -> list[Skill]:
        """Return the inner source's skills, filtered to the allowed name-set if one is given."""
        skills = await self._inner.get_skills(context)
        if self._allowed is None:
            return skills
        return [skill for skill in skills if skill.frontmatter.name in self._allowed]
