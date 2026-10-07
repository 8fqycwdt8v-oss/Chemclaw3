"""Skill visibility: independent narrowings, each answering a different question.

`EnabledSkills`: is it turned on in this deployment? `ProfileScopedSkills`: is this agent about it?
`ToolScopedSkills`: can this agent do any of what it teaches? `RoleScopedSkills`: may this caller
see it? `UnreservedNames` (stored tiers only): does a reviewed tree already own the name? Each only
removes skills, so their order does not change the answer; `skill_permits` returns one predicate per
kind of tier (`SkillNarrowing`).

Capability scoping exists because the tool surface is narrowed per deployment and profile, and a
skill about tools the agent cannot reach misleads the model into planning around them. The basis a
turn passes is what the graph binds, not what manifests advertise.

Role scoping is the one gate with a security posture: a skill named in `settings.skill_role_gates`
is hidden from a caller holding none of its roles; an ungated skill is visible to all. A typo'd gate
key is therefore an ungated skill, which is why `make skill-validate` checks the keys. Roles come
from the turn's ambient identity (`chemclaw.core.identity_context`); off the request path there are
none, so only ungated skills show.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from chemclaw.core.identity_context import get_current_roles


class _Narrowing:
    """One reason a skill may be hidden, as a predicate over its name.

    A subclass says only whether it narrows at all (`_narrows`) and which skills survive
    (`_permits`). `skill_backend` calls `permits` rather than reimplementing it, so "may this caller
    see this skill" has one answer.
    """

    def permits(self, name: str) -> bool:
        """Whether the skill named `name` survives this narrowing (framework-free).

        The short-circuit keeps an unconfigured narrowing (the shipped default) free.
        """
        return not self._narrows() or self._permits(name)

    @abstractmethod
    def _narrows(self) -> bool:
        """Whether this decorator is configured to remove anything (False = pass everything)."""

    @abstractmethod
    def _permits(self, name: str) -> bool:
        """Whether the skill named `name` survives this narrowing."""


class EnabledSkills(_Narrowing):
    """Advertise only the explicitly enabled skills.

    Discovery is not enablement: an enable-list lets a deployment ship the whole tree and turn on
    the subset it has validated. An empty list means everything discovered. An unknown name is
    simply absent, so a config typo degrades the advertised set rather than breaking live turns;
    `make skill-validate` catches it before deploy.

    Args:
        enabled: The skill names to advertise; empty leaves every discovered skill visible.
    """

    def __init__(self, enabled: Iterable[str] | None = None) -> None:
        """Pre-normalize the enable-list to a frozenset for cheap lookups."""
        self._enabled: frozenset[str] = frozenset(enabled or ())

    def _narrows(self) -> bool:
        """An empty enable-list means "everything discovered" — the default."""
        return bool(self._enabled)

    def _permits(self, name: str) -> bool:
        """A skill survives if this deployment turned it on."""
        return name in self._enabled


class ToolScopedSkills(_Narrowing):
    """Hide a skill whose declared capability this agent cannot reach at all.

    A skill is judgment about tools; without the tools it misleads the model into planning around
    capability it will never get. The rule is conservative: a skill is hidden only when *every*
    declared tool is absent (one tool outside a narrow agent's surface is common and harmless), and
    a skill declaring none is always visible.

    Args:
        declared: `{skill name: declared tool names}`
        (`chemclaw.agent.skill_manifest.declared_tools`), read from disk because a loaded skill's
        frontmatter model drops `tools:`.
        available: The tool names this agent can reach. A turn passes what its graph binds (a
        manifest does not move when a server is unreachable); a corpus check passes what the tree
        declares, so skills stay checked while a bundle is down.
    """

    def __init__(
        self,
        declared: Mapping[str, frozenset[str]],
        available: Iterable[str],
        required: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        """Pre-normalize the two declaration maps and the available tool set."""
        self._declared = dict(declared)
        self._required = dict(required or {})
        self._available: frozenset[str] = frozenset(available)

    def _narrows(self) -> bool:
        """Nothing declares a dependency ⇒ nothing to scope, whatever the tool surface is."""
        return any(self._declared.values()) or any(self._required.values())

    def _permits(self, name: str) -> bool:
        """A skill survives if its `requires:` are all reachable and any declared tool is.

        The declared rule alone passes a skill whose central tools are gone while peripheral ones
        remain; `requires:` names that central subset, and one absent entry hides the skill.
        """
        needed = self._required.get(name)
        if needed and not needed <= self._available:
            return False
        declared = self._declared.get(name)
        return not declared or bool(declared & self._available)


class ProfileScopedSkills(_Narrowing):
    """Advertise only the skills this agent's profile names.

    The narrowing a profile owns. The enable-list is one set per process, so without this the only
    per-profile skill narrowing was indirect (removing tools), which makes a skill impossible to
    vary on its own in an A/B arm.

    An unset `skill_names` narrows nothing. An unknown name is absent rather than an error, as in
    `EnabledSkills`; `make skill-validate` catches it.

    Args:
        names: The skill names this profile may reach; `None` narrows nothing.
    """

    def __init__(self, names: Iterable[str] | None = None) -> None:
        """Pre-normalize to a frozenset, keeping `None` distinct from the empty set."""
        self._names: frozenset[str] | None = None if names is None else frozenset(names)

    def _narrows(self) -> bool:
        """`None` narrows nothing; an **empty** set narrows everything away, and means to.

        Unlike the enable-list, `skill_names: []` is a profile author stating the agent reaches no
        skill (a no-skills eval arm), which must be representable.
        """
        return self._names is not None

    def _permits(self, name: str) -> bool:
        """A skill survives if this profile names it."""
        return name in (self._names or frozenset())


class RoleScopedSkills(_Narrowing):
    """Advertise a gated skill only to callers holding one of its roles.

    Args:
        gates: Maps a skill name to the app-roles allowed to see it. A skill absent from the map
            is ungated (visible to all); an empty map leaves every skill visible.
    """

    def __init__(self, gates: Mapping[str, list[str]] | None = None) -> None:
        """Pre-normalize the gate map to frozensets for cheap lookups."""
        self._gates: dict[str, frozenset[str]] = {
            name: frozenset(roles) for name, roles in (gates or {}).items()
        }

    def _narrows(self) -> bool:
        """An empty gate map leaves every skill visible — the default."""
        return bool(self._gates)

    def _permits(self, name: str) -> bool:
        """A skill is permitted if it is ungated, or the caller holds one of its gate roles.

        Roles are read per call from the turn's contextvar, never cached on `self`: one predicate
        object can outlive a turn.
        """
        required = self._gates.get(name)
        return required is None or bool(get_current_roles() & required)


class UnreservedNames(_Narrowing):
    """Hide a **stored** skill whose name this deployment's reviewed trees already occupy.

    Applied only to stored tiers (asked of a filed tree it would hide every shipped skill from
    itself). The write doors already refuse a new stored skill under a shipped name, but not one
    stored before the tree shipped that name. Upstream's `SkillsMiddleware` resolves mount
    collisions by listing, so if another narrowing hid the shared skill the stored document would be
    served under the reserved name; this makes the reviewed name safe structurally.

    Args:
        reserved: Every name the reviewed trees occupy (`shipped_skill_names()`, the discovered set
        the write doors also use). Empty narrows nothing.
    """

    def __init__(self, reserved: Iterable[str] | None = None) -> None:
        """Pre-normalize the reserved set for cheap lookups."""
        self._reserved: frozenset[str] = frozenset(reserved or ())

    def _narrows(self) -> bool:
        """No reviewed tree, no reserved name — the default on a deployment shipping no skills."""
        return bool(self._reserved)

    def _permits(self, name: str) -> bool:
        """A stored skill survives if no reviewed tree occupies its name."""
        return name not in self._reserved


@dataclass(frozen=True)
class SkillNarrowing:
    """The narrowing for a **filed** tree and the narrowing for a **stored** tier, as one value.

    Two fields because the tiers are asked different questions: `EnabledSkills` names shipped
    skills, so applied to a stored tier it would delete the tier rather than narrow it. One tuple of
    narrowing objects builds both, so this is a partition, not a second composition. `stored` is the
    same objects minus `EnabledSkills`, plus `UnreservedNames`.

    `ProfileScopedSkills` stays in `stored` so an eval arm's skill surface means what it names
    (`tests/test_org_skills.py`); consequently a non-empty `skill_names` also removes the stored
    tiers. `RoleScopedSkills` stays too; its keys are filed names, so it is a no-op on a purely
    stored name.
    """

    filed: Callable[[str], bool]
    stored: Callable[[str], bool]

    @classmethod
    def permissive(cls) -> SkillNarrowing:
        """Narrow nothing, either tier — for a caller whose subject is not the narrowing.

        A named constructor rather than a default, so a permissive narrowing is always explicit.
        Every caller is a test; it lives here so a new field fails in one place.
        """
        return cls(filed=lambda _name: True, stored=lambda _name: True)


def skill_permits(
    *,
    enabled: Iterable[str] | None,
    declared: Mapping[str, frozenset[str]],
    available: Iterable[str],
    gates: Mapping[str, list[str]] | None,
    required: Mapping[str, frozenset[str]] | None = None,
    names: Iterable[str] | None = None,
    reserved: Iterable[str] | None = None,
) -> SkillNarrowing:
    """The narrowings as one predicate per tier — the engine-neutral form.

    The one implementation of "may this caller see this skill", read by
    `deepagents.SkillsMiddleware`'s backend; a second would let a skill be hidden in one place and
    offered in another. All narrowings only remove, so their order does not matter.

    Args:
        enabled: The deployment's enable-list; empty means every discovered skill.
        declared: `{skill name: declared tool names}` from `skill_manifest.declared_tools`.
        required: `{skill name: required tool names}` from `skill_manifest.required_tools`, the
        subset without which a skill misleads. Usually empty.
        available: The tool names this agent advertises, both halves of the surface.
        gates: `{skill name: allowed roles}`; a skill absent from the map is ungated.
        names: The profile's `skill_names`; `None` narrows nothing and an empty set narrows
        everything (see `ProfileScopedSkills._narrows`).
        reserved: Every name the reviewed trees occupy, applied to the stored narrowing alone (see
        `UnreservedNames`).

    Returns:
        The narrowing for each kind of tier. Both predicates are evaluated per call, never cached,
        because the role gate reads the turn's ambient identity and one agent serves every
        concurrent turn.
    """
    # One tuple, two subsets of it. The objects are shared rather than rebuilt, so the two tiers
    # cannot come to disagree about a narrowing they both apply — see `SkillNarrowing`.
    enabled_only = EnabledSkills(enabled)
    both = (
        ProfileScopedSkills(names),
        ToolScopedSkills(declared, available, required),
        RoleScopedSkills(gates),
    )
    filed = (enabled_only, *both)
    stored = (*both, UnreservedNames(reserved))
    return SkillNarrowing(
        filed=lambda name: all(narrowing.permits(name) for narrowing in filed),
        stored=lambda name: all(narrowing.permits(name) for narrowing in stored),
    )
