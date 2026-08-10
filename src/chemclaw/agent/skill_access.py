"""Skill visibility: admin enablement, capability scoping, then Phase-6 RBAC (plan step 6.2).

Three independent narrowings, deliberately kept as separate `SkillsSource` decorators because they
answer different questions. `EnabledSkillsSource` answers *"is this skill turned on in this
deployment?"* (an admin/config concern); `ToolScopedSkillsSource` answers *"can this agent do any
of what the skill teaches?"* (a capability concern); `RoleScopedSkillsSource` answers *"may this
caller see it?"* (an identity concern). All three only ever remove skills, so chaining them in any
order is safe; `build_agent` wraps them in the order the request reads — what exists at all, then
what this agent can do, then who may see it.

All three are the same loop over a different predicate, which is what `_NarrowingSkillsSource`
holds: await the inner source, return it untouched when this narrowing is unconfigured, otherwise
filter. Extracted at the third copy (Rule of Three), and the short-circuit is the part worth
sharing — it is what keeps an unconfigured decorator from paying for itself on every turn.

**Why capability scoping exists.** The tool surface is already narrowed three ways — a deployment's
`connectors_enabled`, a profile's `tool_names`/`mcp_server_names` — and the skill surface was
narrowed by none of them. Measured against the shipped `property-lookup` profile (5 callable
tools), 8 of 28 advertised skills had *no* reachable tool at all: the model was handed judgment
about `suggest_next_experiment`, `compute_dft_energy` and the three fingerprint tools, none of
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
`chemclaw.agent.authz` read. So no identity is threaded through `build_agent`, and off the request
path (tests, the classic non-service caller) there are simply no roles, so only ungated skills show
— and with no gates configured, that is still every skill.
"""

from abc import abstractmethod
from collections.abc import Callable, Iterable, Mapping

from agent_framework import Skill, SkillsSource, SkillsSourceContext

from chemclaw.core.identity_context import get_current_roles


class _NarrowingSkillsSource(SkillsSource):
    """Wrap a `SkillsSource` and drop the skills a subclass's predicate rejects.

    The shared half of the three decorators below: fetch, short-circuit, filter. A subclass says
    only *whether it is configured to narrow at all* (`_narrows`) and *which skills survive*
    (`_permits`), so a new narrowing is a predicate rather than another copy of this loop.

    **`permits` is the whole decision, and it is deliberately separable from this class.** The
    LangGraph engine reaches skills through a backend rather than a `SkillsSource`
    (`chemclaw.agent.skill_backend`), so it needs the same three answers with none of this
    plumbing. Both engines therefore call `permits`; only `get_skills` is MAF-shaped. A second
    implementation of "may this caller see this skill" would be the one duplication a role gate
    cannot survive — a skill hidden under one engine and offered under the other is not a gate.

    Args:
        inner: The wrapped source (e.g. a `FileSkillsSource`, or another narrowing source).
    """

    def __init__(self, inner: SkillsSource) -> None:
        """Hold the wrapped source; subclasses add their own pre-normalized configuration."""
        self._inner = inner

    async def get_skills(self, context: SkillsSourceContext) -> list[Skill]:
        """The inner source's skills, minus the ones this narrowing rejects."""
        skills = await self._inner.get_skills(context)
        return [skill for skill in skills if self.permits(skill.frontmatter.name)]

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


class EnabledSkillsSource(_NarrowingSkillsSource):
    """Advertise only the explicitly enabled skills.

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
        super().__init__(inner)
        self._enabled: frozenset[str] = frozenset(enabled or ())

    def _narrows(self) -> bool:
        """An empty enable-list means "everything discovered" — the default."""
        return bool(self._enabled)

    def _permits(self, name: str) -> bool:
        """A skill survives if this deployment turned it on."""
        return name in self._enabled


class ToolScopedSkillsSource(_NarrowingSkillsSource):
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
        inner: The wrapped source (e.g. a `FileSkillsSource`).
        declared: `{skill name: declared tool names}` (`chemclaw.agent.skill_manifest.
            declared_tools`) — read from disk once, because MAF's `SkillFrontmatter` drops the
            `tools:` key and a `Skill` object therefore cannot answer this question.
        available: The tool names this agent actually advertises, both halves of the surface
            (`chemclaw.agent.chemclaw_agent.advertised_tool_names`).
    """

    def __init__(
        self,
        inner: SkillsSource,
        declared: Mapping[str, frozenset[str]],
        available: Iterable[str],
    ) -> None:
        """Wrap `inner` and pre-normalize the declaration map and the available tool set."""
        super().__init__(inner)
        self._declared = dict(declared)
        self._available: frozenset[str] = frozenset(available)

    def _narrows(self) -> bool:
        """Nothing declares a dependency ⇒ nothing to scope, whatever the tool surface is."""
        return any(self._declared.values())

    def _permits(self, name: str) -> bool:
        """A skill survives if it declares no tools, or at least one of them is reachable."""
        required = self._declared.get(name)
        return not required or bool(required & self._available)


class RoleScopedSkillsSource(_NarrowingSkillsSource):
    """Advertise a gated skill only to callers holding one of its roles.

    Args:
        inner: The wrapped source (e.g. a `FileSkillsSource`).
        gates: Maps a skill name to the app-roles allowed to see it. A skill absent from the map
            is ungated (visible to all); an empty map leaves every skill visible.
    """

    def __init__(self, inner: SkillsSource, gates: Mapping[str, list[str]] | None = None) -> None:
        """Wrap `inner` and pre-normalize the gate map to frozensets for cheap lookups."""
        super().__init__(inner)
        self._gates: dict[str, frozenset[str]] = {
            name: frozenset(roles) for name, roles in (gates or {}).items()
        }

    def _narrows(self) -> bool:
        """An empty gate map leaves every skill visible — the default."""
        return bool(self._gates)

    def _permits(self, name: str) -> bool:
        """A skill is permitted if it is ungated, or the caller holds one of its gate roles.

        The turn's roles are read here rather than hoisted into `get_skills` and cached on `self`.
        One `SkillsProvider` is built per *process* and serves every concurrent turn, so any
        per-call state stored on this object would be another turn's identity a moment later — the
        same lifetime rule that keeps connector MCP tools per-turn. A `contextvar` read is a dict
        lookup, and it is always this turn's.
        """
        required = self._gates.get(name)
        return required is None or bool(get_current_roles() & required)


def skill_permits(
    *,
    enabled: Iterable[str] | None,
    declared: Mapping[str, frozenset[str]],
    available: Iterable[str],
    gates: Mapping[str, list[str]] | None,
) -> Callable[[str], bool]:
    """The three narrowings as one predicate over a skill name — the engine-neutral form.

    `chemclaw_agent.skills_source` composes the same three as `SkillsSource` decorators, because
    MAF reaches skills by asking a source for them. The LangGraph engine reaches them through a
    backend (`chemclaw.agent.skill_backend`), which has no source to decorate and needs the answer
    as a function. Both call the same three `permits` methods on the same three objects, so the
    only thing that differs is which shape the caller wanted it in.

    That sharing is not tidiness. Role scoping is the one narrowing with a security posture, and a
    second implementation of "may this caller see this skill" is how a skill ends up hidden under
    one engine and offered under the other — which is not a gate, it is a coin flip with a config
    flag for a coin.

    The order matches the request as it reads — what exists at all, then what this agent can do,
    then who may see it — though all three only ever remove, so composing them in any order gives
    the same answer.

    Args:
        enabled: The deployment's enable-list; empty means every discovered skill.
        declared: `{skill name: declared tool names}` from `skill_manifest.declared_tools`.
        available: The tool names this agent advertises, both halves of the surface.
        gates: `{skill name: allowed roles}`; a skill absent from the map is ungated.

    Returns:
        A predicate answering "is this skill visible to the turn in flight". Evaluated per call,
        never cached, because the role gate reads the turn's ambient identity and one agent serves
        every concurrent turn.
    """
    narrowings = (
        EnabledSkillsSource(_NOTHING, enabled),
        ToolScopedSkillsSource(_NOTHING, declared, available),
        RoleScopedSkillsSource(_NOTHING, gates),
    )
    return lambda name: all(narrowing.permits(name) for narrowing in narrowings)


class _EmptySkillsSource(SkillsSource):
    """A source with no skills, to satisfy the decorators when only their predicate is wanted.

    The three narrowings are decorators because that is what MAF's loading model needs; their
    *decision* takes no inner source at all. Rather than split each class in two to express that,
    the predicate-only caller passes this — one object, never consulted, and the alternative was
    six classes where three plus a sentinel do.
    """

    async def get_skills(self, context: SkillsSourceContext) -> list[Skill]:
        """No skills — this source exists to be decorated, never to be read."""
        return []


_NOTHING = _EmptySkillsSource()
