"""Skill visibility decorators: admin enablement, then Phase-6 RBAC (plan step 6.2).

Two independent narrowings, deliberately kept as separate `SkillsSource` decorators because they
answer different questions. `EnabledSkillsSource` answers *"is this skill turned on in this
deployment?"* (an admin/config concern); `RoleScopedSkillsSource` answers *"may this caller see
it?"* (an identity concern). Both only ever remove skills, so chaining them in either order is
safe; `build_agent` wraps enablement innermost (what exists at all) and role scoping outermost
(who sees what), which reads in the same direction as the request.

By default every skill is advertised to every caller (the model sees them all). This scopes
skills by the caller's Entra app-roles: a *gated* skill (named in `settings.skill_role_gates`,
mapping skill name → allowed roles) is hidden from a caller who holds none of its roles. A skill
with no gate is visible to everyone, so an empty gate map reproduces today's behavior.

The caller's roles are the turn's **ambient identity** (`agents.identity_context`), stamped by
the front door from the validated `Principal` — the same source `agents.audit`/`agents.authz`
read. So no identity is threaded through `build_agent`, and off the request path (tests, the
classic non-service caller) there are simply no roles, so only ungated skills show — and with no
gates configured, that is still every skill. A thin decorator over any `SkillsSource` (DRY): the
file source (and any future source) gains role scoping without duplicating the filter, and the
gate map is config, so scoping a skill is an admin change, not code.
"""

from collections.abc import Iterable, Mapping

from agent_framework import Skill, SkillsSource, SkillsSourceContext

from agents.identity_context import get_current_roles


class EnabledSkillsSource(SkillsSource):
    """Wrap a `SkillsSource`, advertising only the explicitly enabled skills.

    Discovery is not enablement: `FileSkillsSource` advertises every `SKILL.md` it finds, which
    means adding a folder silently changes what the agent offers. An explicit enable-list lets a
    deployment ship the whole skills tree and turn on the subset it has validated.

    An **empty** list means "everything discovered" — the default, and today's behavior — so this
    decorator is a no-op until a deployment opts in. A name that no directory provides is simply
    absent from the result rather than an exception: this runs per turn, so a config typo must
    degrade the advertised set, not break every live conversation. `make skill-validate` is where
    that typo is caught loudly, before deploy.

    Args:
        inner: The wrapped source (e.g. a `FileSkillsSource`).
        enabled: The skill names to advertise; empty leaves every discovered skill visible.
    """

    def __init__(self, inner: SkillsSource, enabled: Iterable[str] | None = None) -> None:
        """Wrap `inner` and pre-normalize the enable-list to a frozenset for cheap lookups."""
        self._inner = inner
        self._enabled: frozenset[str] = frozenset(enabled or ())

    async def get_skills(self, context: SkillsSourceContext) -> list[Skill]:
        """Return the inner source's skills, narrowed to the enabled names when one is set."""
        skills = await self._inner.get_skills(context)
        if not self._enabled:
            return skills
        return [skill for skill in skills if skill.frontmatter.name in self._enabled]


class RoleScopedSkillsSource(SkillsSource):
    """Wrap a `SkillsSource`, advertising a gated skill only to callers holding one of its roles.

    Args:
        inner: The wrapped source (e.g. a `FileSkillsSource`).
        gates: Maps a skill name to the app-roles allowed to see it. A skill absent from the map
            is ungated (visible to all); an empty map leaves every skill visible.
    """

    def __init__(self, inner: SkillsSource, gates: Mapping[str, list[str]] | None = None) -> None:
        """Wrap `inner` and pre-normalize the gate map to frozensets for cheap lookups."""
        self._inner = inner
        self._gates: dict[str, frozenset[str]] = {
            name: frozenset(roles) for name, roles in (gates or {}).items()
        }

    async def get_skills(self, context: SkillsSourceContext) -> list[Skill]:
        """Return the inner source's skills, dropping gated ones the turn's caller may not see."""
        skills = await self._inner.get_skills(context)
        if not self._gates:
            return skills
        roles = get_current_roles()
        return [skill for skill in skills if self._permitted(skill.frontmatter.name, roles)]

    def _permitted(self, name: str, roles: frozenset[str]) -> bool:
        """A skill is permitted if it is ungated, or the caller holds one of its gate roles."""
        required = self._gates.get(name)
        return required is None or bool(roles & required)
