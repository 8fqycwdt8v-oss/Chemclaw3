"""Role-scoped skill visibility — Phase-6 RBAC for advertised skills (plan step 6.2).

By default every skill is advertised to every caller (the model sees them all). Phase 6 scopes
skills by the caller's Entra app-roles: `RoleScopedSkillsSource` hides a *gated* skill from a
caller who lacks one of its roles. A skill with no gate is visible to everyone, so an empty
gate map reproduces today's behavior; an anonymous caller (`principal=None`) holds no roles and
therefore sees only ungated skills.

It is a thin decorator over any `SkillsSource` (DRY): the file source gains role scoping without
duplicating the filter, and a different source would gain the same gating unchanged. The gate
map is config (`settings.skill_role_gates`), so scoping a skill is an admin change, not code.
"""

from collections.abc import Mapping

from agent_framework import Skill, SkillsSource, SkillsSourceContext

from chemclaw.identity import Principal


class RoleScopedSkillsSource(SkillsSource):
    """Wrap a `SkillsSource`, advertising a gated skill only to callers holding one of its roles.

    Args:
        inner: The wrapped source (e.g. a `FileSkillsSource`).
        gates: Maps a skill name to the app-roles allowed to see it. A skill absent from the map
            is ungated (visible to all); an empty map leaves every skill visible.
        principal: The caller. `None` (anonymous / dev) holds no roles, so it sees only ungated
            skills.
    """

    def __init__(
        self,
        inner: SkillsSource,
        gates: Mapping[str, list[str]] | None = None,
        principal: Principal | None = None,
    ) -> None:
        """Wrap `inner` and pre-normalize the gate map to frozensets for cheap lookups."""
        self._inner = inner
        self._gates: dict[str, frozenset[str]] = {
            name: frozenset(roles) for name, roles in (gates or {}).items()
        }
        self._principal = principal

    async def get_skills(self, context: SkillsSourceContext) -> list[Skill]:
        """Return the inner source's skills, dropping gated ones the caller may not see."""
        skills = await self._inner.get_skills(context)
        if not self._gates:
            return skills
        roles = self._principal.roles if self._principal is not None else frozenset()
        return [skill for skill in skills if self._permitted(skill.frontmatter.name, roles)]

    def _permitted(self, name: str, roles: frozenset[str]) -> bool:
        """A skill is permitted if it is ungated, or the caller holds one of its gate roles."""
        required = self._gates.get(name)
        return required is None or bool(roles & required)
