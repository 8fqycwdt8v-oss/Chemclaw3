"""Skill visibility: admin enablement, capability scoping, then Phase-6 RBAC (plan step 6.2).

Independent narrowings, deliberately kept separate because they answer different questions.
`EnabledSkills` answers *"is this skill turned on in this
deployment?"* (an admin/config concern); `ProfileScopedSkills` answers *"is this agent about it?"*
(a per-profile concern); `ToolScopedSkills` answers *"can this agent do any
of what the skill teaches?"* (a capability concern); `RoleScopedSkills` answers *"may this
caller see it?"* (an identity concern). Every one of them only ever removes skills, so chaining them
in any order is safe; `build_langgraph_agent` wraps them in the order the request reads — what
exists at all, then what this agent is about, then what it can do, then who may see it.

**`ProfileScopedSkills` arrived last of those four, and for a reason that is not symmetry.** The
other three are a
deployment's, a tool surface's and an identity's, and none of them belongs to a *profile* — so
until `AgentProfile.skill_names` existed, the only way to move one agent's skill surface was to
move its tool surface and let `ToolScopedSkills` follow. That coupling is what made a skill
unmeasurable: an A/B arm is a profile file, and an arm that cannot hold tools still while moving
skills produces a delta nobody can attribute. See `ProfileScopedSkills`.

**A fifth arrived with the stored tiers, and it is the one that is not asked of every tier.**
`UnreservedNames` answers *"is this a name the reviewed trees already occupy?"*, which is a question
only a **stored** skill can fail — so `skill_permits` returns a `SkillNarrowing` with one predicate
per kind of tier rather than one for all of them. That type carries the measurement: `EnabledSkills`
was being applied to the stored tiers and **deleted them outright**, which both of their module
docstrings already said it would, as their reason for believing it did not.

All five are the same short-circuit over a different predicate, which is what `_Narrowing`
holds: await the inner source, return it untouched when this narrowing is unconfigured, otherwise
filter. Extracted at the third copy (Rule of Three), and the short-circuit is the part worth
sharing — it is what keeps an unconfigured decorator from paying for itself on every turn.

**Why capability scoping exists.** The tool surface is already narrowed three ways — a deployment's
`connectors_enabled`, a profile's `tool_names`/`mcp_server_names` — and the skill surface was
narrowed by none of them. (A fourth narrowing arrived later and this gate could not see it either:
a declared bundle whose server is unreachable binds nothing, while its manifest still names every
tool — so the basis a *turn* passes is what the graph binds, not what is advertised.)
Measured against the shipped `property-lookup` profile (5 callable
tools), 8 of 28 advertised skills had *no* reachable tool at all: the model was handed judgment
about `suggest_next_experiment`, `sample_conformers` and the three fingerprint tools, none of
which that agent can call. The profile compensated in prose ("if a question needs experimental
history, say that it is outside this mode"), which is the failure this repository fixes with
structure rather than with a longer instruction (D-2026-08-05).

By default every skill is advertised to every caller (the model sees them all). Role scoping is the
one gate with a security posture: a *gated* skill (named in `settings.skill_role_gates`, mapping
skill name → allowed roles) is hidden from a caller who holds none of its roles. A skill with no
gate is visible to everyone, so an empty gate map reproduces today's behavior — and a *typo'd* gate
key is therefore an un-gated skill, which is why `make skill-validate` checks the map's keys
against the discovered skills.

The caller's roles are the turn's **ambient identity** (`chemclaw.core.identity_context`), stamped
by the front door from the validated `Principal` — the same source `chemclaw.agent.audit`/
`chemclaw.agent.authz` read. So no identity is threaded through `build_langgraph_agent`, and off the
request path (tests, the classic non-service caller) there are simply no roles, so only ungated
skills show — and with no gates configured, that is still every skill.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from chemclaw.core.identity_context import get_current_roles


class _Narrowing:
    """One reason a skill may be hidden, as a predicate over its name.

    A subclass says only *whether it is configured to narrow at all* (`_narrows`) and *which skills
    survive* (`_permits`), so a new narrowing is a predicate rather than another copy of the
    short-circuit.

    **These used to be `SkillsSource` decorators**, because MAF reached skills by asking a source
    for them and each narrowing wrapped the one beneath it. The plumbing is gone with that engine;
    the decisions are not, and they were already the separable half — `skill_backend` has always
    called `permits` rather than reimplementing it, since a second implementation of "may this
    caller see this skill" is how a skill ends up hidden in one place and offered in another, which
    is not a gate.
    """

    def permits(self, name: str) -> bool:
        """Whether the skill named `name` survives this narrowing (framework-free).

        The short-circuit lives here rather than in the caller because it is what keeps an
        unconfigured decorator from paying for itself: an empty enable-list, an empty gate map and
        an empty declaration map each mean "narrow nothing", which is the default this system ships.
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

    Discovery is not enablement: the backend advertises every `SKILL.md` it finds, which
    means adding a folder silently changes what the agent offers. An explicit enable-list lets a
    deployment ship the whole skills tree and turn on the subset it has validated.

    An **empty** list means "everything discovered" — the default, and today's behavior — so this
    decorator is a no-op until a deployment opts in. A name that no directory provides is simply
    absent from the result rather than an exception: this runs per turn, so a config typo must
    degrade the advertised set, not break every live conversation. `make skill-validate` is where
    that typo is caught loudly, before deploy.

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

    A skill is judgment *about tools* — "call `suggest_next_experiment` like this", "here is what a
    computed barrier does and does not support". When the tools are gone, the judgment is not
    merely useless, it is misleading: the model reads it as an available path and plans around
    capability it will never get, which is the same defect as prose naming a tool that does not
    exist (`chemclaw.cli.validate_prose_contract`), arriving by a different door.

    **The rule is deliberately conservative: a skill is hidden only when *every* tool it declares is
    absent, and a skill declaring none is always visible.** Both halves were measured rather than
    argued. Hiding on *any* missing tool takes 20 of 28 skills off the shipped `property-lookup`
    profile, including `calculation-selection` — the one that profile's own instructions tell the
    model to load — because a skill routinely names one tool outside a narrow agent's surface while
    remaining entirely useful for the rest. Hiding on *all* takes 8, and every one of them is a
    skill about a capability that agent genuinely does not have. A skill with no declaration is
    process guidance that depends on nothing (`development-report`, `playbook-distillation`), so
    there is nothing to scope it by; leaving it visible is the honest reading of an empty list, and
    `make skill-validate` is what stops a declaration from being *silently* incomplete.

    Args:
        declared: `{skill name: declared tool names}` (`chemclaw.agent.skill_manifest.
            declared_tools`) — read from disk once, because a loaded skill's own frontmatter model
            drops the `tools:` key and therefore cannot answer this question.
        available: The tool names this agent can actually reach. Two callers ask two different
            questions with it and both are right: a turn passes the surface its graph **binds**
            (`langgraph_agent.build_langgraph_agent`), because a manifest does not move when a
            server is unreachable and a listing narrowed by manifests offered skills whose every
            tool was bound to nothing; a corpus check passes what this tree *declares*
            (`chemclaw_agent.advertised_tool_names`), because a `SKILL.md` about a capability the
            repository still ships should not stop being checked when a bundle is down.
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

        Two rules, because one of them alone gets a measured case wrong.

        The **declared** rule — survive if *any* declared tool is reachable — is the conservative
        one this class shipped with, and the docstring above carries the measurement for why it is
        not "all": a skill routinely names one tool outside a narrow agent's surface while staying
        entirely useful for the rest.

        The **required** rule is the case that misses. A skill whose *central* tools left with an
        opt-in bundle keeps its peripheral ones, so it passes the declared rule and is listed in
        every deployment's prefix as judgment about a path the turn cannot take. `requires:` names
        that subset, and one absent entry is enough — because the point of declaring it is that
        without it the skill is misleading rather than narrower.
        """
        needed = self._required.get(name)
        if needed and not needed <= self._available:
            return False
        declared = self._declared.get(name)
        return not declared or bool(declared & self._available)


class ProfileScopedSkills(_Narrowing):
    """Advertise only the skills this agent's profile names.

    The fourth narrowing, and the one a *profile* owns. `EnabledSkills` answers "did this
    deployment turn it on", `ToolScopedSkills` answers "can this agent do any of what it teaches",
    `RoleScopedSkills` answers "may this caller see it" — and none of them can express "this agent
    is about these skills", which is the question a profile exists to answer for every other
    dimension it carries.

    **Why it is not just a longer enable-list.** The enable-list is a deployment's, read from
    `settings`, and it is one set for the whole process: two profiles served by one deployment get
    the same answer from it. `tool_names` already narrows per profile and the skill surface did not,
    so the only per-profile skill narrowing available was the *indirect* one — take a tool away and
    `ToolScopedSkills` takes its skills with it. That indirection is precisely what makes a skill
    unmeasurable, because it cannot be moved on its own.

    An unset `skill_names` means "whatever the other three left", which is today's behaviour, so
    this is a no-op on every shipped profile. A name no directory provides is absent rather than an
    error, for the same reason `EnabledSkills` degrades that way: this runs per turn, and a config
    typo must narrow the advertised set rather than break every live conversation.
    `make skill-validate` is where it is caught loudly.

    Args:
        names: The skill names this profile may reach; `None` narrows nothing.
    """

    def __init__(self, names: Iterable[str] | None = None) -> None:
        """Pre-normalize to a frozenset, keeping `None` distinct from the empty set."""
        self._names: frozenset[str] | None = None if names is None else frozenset(names)

    def _narrows(self) -> bool:
        """`None` narrows nothing; an **empty** set narrows everything away, and means to.

        The distinction is deliberate and is the one `EnabledSkills` does not make: there, an empty
        list is the unset default, because a deployment that names no skill means "all of them". A
        profile is the other way round — `skill_names: []` is a profile author writing down that
        this agent reaches no skill at all, which is a real configuration (a no-skills eval arm is
        exactly it) and is unrepresentable if empty silently means everything.
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

        The turn's roles are read here rather than hoisted into `__init__` and cached on `self`.
        One predicate object can outlive a turn, so any per-call state stored on it would be
        another turn's identity a moment later — the same lifetime rule that keeps connector MCP
        sessions per-turn. A `contextvar` read is a dict
        lookup, and it is always this turn's.
        """
        required = self._gates.get(name)
        return required is None or bool(get_current_roles() & required)


class UnreservedNames(_Narrowing):
    """Hide a **stored** skill whose name this deployment's reviewed trees already occupy.

    The fifth narrowing, and the only one that applies to one kind of tier rather than to one kind
    of question — which is why it is not composed into `SkillNarrowing.filed`: asking it of a filed
    tree would hide every shipped skill from itself.

    **It exists because the read side and the write side had different bases for one rule.** Both
    write doors into the stored tiers refuse a name `langgraph_agent.shipped_skill_names()`
    occupies, and upstream's `SkillsMiddleware` resolves a collision between two mounts by *listing*
    — so a shared skill that any narrowing removes is not in the listing to displace anything, and
    the stored document is served under the reserved name. `tests/test_local_skills.py::
    test_a_reviewed_skill_wins_a_name_a_personal_one_also_claims` puts `/mine` before `/skills`
    precisely so the reviewed skill wins that collision, and a hidden shared skill inverted it.

    **Which narrowing actually reaches this was measured, and the backlog row's answer was stale.**
    The row named a `skill_role_gates` entry, and that was true of the tier as it stood when the row
    was written: with the stored mounts ungated — narrowed in the prompt only, before
    `D-2026-09-20` gave them a backend predicate — a gate on `deep-research` served
    `/mine/deep-research/SKILL.md`. Driven again against the one *shared* predicate that replaced
    it: the gate hides **both** copies, because `RoleScopedSkills` is in `stored` too, so the row
    was already closed as a side effect. Every narrowing was, for a colliding name: the declaration
    map is keyed once, so a filed skill's own `tools:` decided the stored copy as well.

    **What re-opens it is the `EnabledSkills` fix in `SkillNarrowing` below**, which is why these
    two land together rather than separately. That narrowing is now `filed`-only, so an enable-list
    that hides `deep-research` in `skills/` no longer empties `/mine` along with it — driven, the
    listing moves to `/mine/deep-research/SKILL.md`. This narrowing is what makes the name safe
    *structurally*, rather than leaving it safe as a consequence of a narrowing whose real effect
    was deleting the tier.

    The write doors cannot close this on their own, and that is the whole reason a read-side rule is
    needed: they refuse a *new* stored skill under a shipped name, and say nothing about one stored
    before that tree shipped the name, or about a name a later commit moved *into* `skills/`.

    Args:
        reserved: Every name this deployment's reviewed trees occupy — `shipped_skill_names()`, the
            *discovered* set, which is the same basis the write doors use. Empty narrows nothing, so
            a deployment with no skills tree is unaffected.
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

    **Two fields rather than one predicate, because the tiers are not asked the same question — and
    three of the four narrowings were answering the wrong one.** Both stored tiers' module
    docstrings stated that `EnabledSkills` does not apply to them, each giving the same reason: the
    enable-list names *shipped* skills, so applying it would delete the tier outright rather than
    narrow it. Driven against `scratchpad_backend` before this type existed, it did exactly that —
    `CHEMCLAW_SKILLS_ENABLED=development-report` left `ls('/mine/')` and `ls('/org/')` both empty. A
    narrowing whose basis is a deployment's list of filed names cannot narrow a stored tier, because
    `make skill-validate` checks that list against the *discovered* trees and therefore no stored
    name can legally appear in it.

    **One tuple of narrowing objects builds both**, so this is a partition rather than a second
    composition: `skill_permits`' own docstring says a second implementation of "may this caller see
    this skill" is "not a gate, it is a coin flip with a config flag for a coin", and two
    independent compositions would be that. `stored` is the same objects minus the one that cannot
    answer, plus the one that only a stored tier is asked (`UnreservedNames`).

    **What deliberately stays in `stored`.** `ProfileScopedSkills` does — `skill_names: frozenset()`
    is a profile author writing down that this agent reaches no skill at all, which is what the
    `skills-removed.yaml` eval control arm is, and a tier escaping it would contaminate every arm
    that measured against it (`tests/test_org_skills.py`). `RoleScopedSkills` does too: its keys are
    validated against the filed trees, so it is a no-op on a stored name unless that name is also a
    shipped one — the case `UnreservedNames` removes from the listing anyway.

    The residual is recorded rather than hidden: a profile naming a *non-empty* `skill_names`
    removes both stored tiers as collateral, for the same structural reason the enable-list did. It
    is kept because the measurement case wants it — an A/B arm naming its skill surface means that
    surface, and a stored tier leaking in produces a delta nobody can attribute — and because no
    shipped profile sets the field at all, so it is a latent consequence of a deliberate rule rather
    than a live defect. `docs/decisions/` carries the argument.
    """

    filed: Callable[[str], bool]
    stored: Callable[[str], bool]

    @classmethod
    def permissive(cls) -> SkillNarrowing:
        """Narrow nothing, either tier — for a caller whose subject is not the narrowing.

        A named constructor rather than a defaulted argument, because the personal tier shipped with
        no backend predicate at all: it was narrowed in the prompt, so `skill_names: []` advertised
        nothing and still served every body to anyone who guessed a path. A default is how that
        reopens by omission; a caller writing `SkillNarrowing.permissive()` is saying so.

        **Every caller is a test**, and it ships here rather than in a fixture for that reason: the
        thing it has to stay in step with is this class, so a field added above without a value here
        is a build error in one place instead of a permissive answer six test files derive by hand.
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

    The one form now. It was one of two while MAF composed the same three as `SkillsSource`
    decorators, because that framework reached skills by asking a source for them; the backend
    `deepagents.SkillsMiddleware` reads has no source to decorate and needs the answer as a
    function, and both called these same three `permits` methods.

    That sharing was not tidiness. Role scoping is the one narrowing with a security posture, and a
    second implementation of "may this caller see this skill" is how a skill ends up hidden in one
    place and offered in another — which is not a gate, it is a coin flip with a config flag for a
    coin. The plumbing that made it two forms is gone; the rule that keeps it one stands.

    The order matches the request as it reads — what exists at all, then what this agent is about,
    then what it can do, then who may see it — though all four only ever remove, so composing them
    in any order gives the same answer.

    Args:
        enabled: The deployment's enable-list; empty means every discovered skill.
        declared: `{skill name: declared tool names}` from `skill_manifest.declared_tools`.
        required: `{skill name: required tool names}` from `skill_manifest.required_tools` —
            the subset without which a skill is misleading rather than narrower. Optional and
            almost always empty; `SkillManifest.requires` says why it is a second question.
        available: The tool names this agent advertises, both halves of the surface.
        gates: `{skill name: allowed roles}`; a skill absent from the map is ungated.
        names: The profile's `skill_names`; `None` narrows nothing, and an empty set narrows
            everything away — see `ProfileScopedSkills._narrows` for why those differ here and not
            in the enable-list.
        reserved: Every name this deployment's reviewed trees occupy, applied to the **stored**
            narrowing alone. See `UnreservedNames`, and `SkillNarrowing` for why the two tiers do
            not get one predicate.

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
