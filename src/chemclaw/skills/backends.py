"""What a turn reads skills through: backends that reach only permitted skills, and refuse writes.

`deepagents.SkillsMiddleware` publishes each skill's path in the system prompt and the model reads
bodies with ordinary filesystem tools over the same backend, so narrowing the listing is not
enough: a gated skill would be one guessed path away. The narrowing therefore lives in the backend,
applying `access`'s predicate on every reach path (`ls`, `read`, `glob`, `grep`, `download_files`
and the native async `aread`); the async twins that are thread wrappers dispatch through these.
`NarrowedSkillsBackend` serves the reviewed tree (`virtual_mode=True` roots every path at `/` and
refuses traversal, so a path's first segment is its skill); `PermittedStoreBackend` serves the two
stored tiers. Every write verb of every backend is refused as `SkillsReadOnlyRefusal`, so an agent
can never rewrite its own instructions. This module imports `deepagents`; `store.py` does not.
"""

import dataclasses
import logging
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Annotated, Any, NotRequired, TypeVar, cast

from deepagents.backends import FilesystemBackend, StoreBackend
from deepagents.backends.protocol import GlobResult, GrepResult, LsResult, ReadResult
from deepagents.middleware.skills import SkillMetadata, SkillsMiddleware, SkillsState
from langchain.agents.middleware.types import PrivateStateAttr
from langgraph.channels.untracked_value import UntrackedValue

from chemclaw.agent.audit import bounded_repr
from chemclaw.agent.langgraph_agent import NO_SKILLS
from chemclaw.agent.refusal_route import routed
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.model_prose import ModelProse
from chemclaw.core.turn_signals import record_skill_loaded
from chemclaw.skills.access import SkillWriteRefused, Tier, Writer, may_write
from chemclaw.skills.store import local_skills_namespace, org_skills_namespace

logger = logging.getLogger(__name__)

# What a refused read returns: a readable result keeps the turn going. It does not say whether the
# skill exists, so the gate is not an enumeration oracle.
REFUSED = ModelProse("This path is not part of the skills available to you.")

# What a refused write to the reviewed tree says, worded for the model: nothing changed, and
# working notes belong under the scratch root.
_READ_ONLY = routed(
    "the skills tree is read-only — a skill is reviewed judgment, not something a turn may "
    "rewrite. Nothing was changed; keep working notes under /scratch/ instead.",
    code="skills_read_only",
    boundary="the skills tree, which is read-only to every turn in every deployment",
    who_can_act="nobody at run time — a skill changes only by a reviewed commit to skills/",
    sanctioned_path="write the same content under /scratch/, which this turn owns",
)

#: What a refused write to a chemist's own tier says; the sanctioned path is the owner's route.
_LOCAL_READ_ONLY = routed(
    "your own skills are read-only to a turn — a skill is judgment that reshapes later answers, "
    "so it changes only when you decide it does, not when a turn decides. Nothing was changed.",
    code="local_skills_read_only",
    boundary="the chemist's own skills tier, which no turn may write",
    who_can_act="the chemist who owns it, through the skills route",
    sanctioned_path="draft the skill in your answer and say it can be saved from there",
)

#: What a refused write to the organisation's tier says; the route it names is this tier's own.
_ORG_READ_ONLY = routed(
    "the organisation's skills are read-only to a turn — a skill acts on everyone's answers, so it "
    "changes only when an administrator decides it does. Nothing was changed.",
    code="org_skills_read_only",
    boundary="the organisation's skills tier, which no turn may write",
    who_can_act="an administrator, through the organisation's skills route",
    sanctioned_path="draft the skill in your answer and say an administrator can publish it",
)

# The tool the skills prompt tells the model to call: deepagents' `SKILLS_SYSTEM_PROMPT` says "use
# `read_file` on the path shown", so any other name would leave skills unloadable. Pinned against
# the prompt by `tests/test_skill_backend.py`.
SKILL_READ_TOOL = "read_file"


class SkillsReadOnlyRefusal(SkillWriteRefused):
    """A turn tried to change a skills tier, which no deployment permits.

    An `AuthorizationError` through `SkillWriteRefused`, so `surface_authorization_denials` relays
    it behind `Refused:` (an access-control decision, not a fault to retry) and the audit row
    records a refusal.
    """


#: The writer every backend verb stands for: a model, reaching a tier through a filesystem tool.
_MODEL = Writer(by_model=True)


def _refusal(tier: Tier, text: str) -> SkillsReadOnlyRefusal:
    """The refusal a write verb raises, derived from `access.may_write`.

    The text is the tier's own and unchanged. `may_write` answers no for a model on every tier; if
    it ever answered yes, this refuses all the same, loudly, rather than letting a model write.
    """
    if may_write(tier, _MODEL):
        raise RuntimeError(f"access.may_write admits a model turn to the {tier.value} tier")
    return SkillsReadOnlyRefusal(text)


def skill_of(path: str) -> str:
    """The skill a path belongs to — its first segment, which is what `_allows` gates on.

    Empty for the tree root. One definition, so the logged name and the gated name agree; the stored
    tiers import it too.
    """
    parts = PurePosixPath(path.strip("/")).parts
    return parts[0] if parts else ""


def is_a_skill_body(path: str) -> bool:
    """Whether `path` names a document *inside* a skill, rather than one at the tree root.

    The clamp on `chemclaw_skill_loads_total`'s label. A root-level file such as `README.md`
    resolves and is gated under its own name, but it is evidence about no skill. A path with a
    directory segment before its filename belongs to a skill; the read already resolved, so no
    `stat` is needed.
    """
    return len(PurePosixPath(path.strip("/")).parts) > 1


def path_of(hit: Any) -> str:
    """The path a glob or grep hit names — a `FileInfo` mapping, a `GrepMatch`, or a bare string."""
    if isinstance(hit, str):
        return hit
    if isinstance(hit, dict):
        return str(hit.get("path", ""))
    return str(getattr(hit, "path", ""))


class NarrowedSkillsBackend(FilesystemBackend):
    """A skills backend whose every read path is bounded by one `permits` predicate.

    Args:
        root_dir: The skills tree.
        permits: Whether a skill *name* is visible to this turn — `access`'s composed narrowing.
        Called per reach, because the role gate reads the turn's ambient identity and one backend
        serves every concurrent turn.
    """

    def __init__(self, root_dir: str, permits: Callable[[str], bool]) -> None:
        """Hold the tree and the predicate; no filesystem access happens here."""
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self._permits = permits

    def _allows(self, path: str) -> bool:
        """Whether this turn may reach `path` at all.

        A path's first segment names its skill. The tree root is neither permitted nor refused:
        listing it starts discovery, and `ls` filters what comes back.
        """
        skill = skill_of(path)
        return not skill or self._permits(skill)

    def ls(self, path: str) -> LsResult:
        """List the tree, keeping only entries belonging to a permitted skill."""
        result = super().ls(path)
        if not result.entries:
            return result
        kept = [entry for entry in result.entries if self._allows(str(entry.get("path", "")))]
        return LsResult(error=result.error, entries=kept)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Read a file, refusing anything outside a permitted skill — and say which, either way.

        The half that makes this a gate: the model could otherwise ask for a path it was never
        shown.

        A delivered body logs at INFO (which procedure the model actually opened) and increments
        `chemclaw_skill_loads_total`. The counter's label is model-authored, so it is booked only
        when the read resolved, `is_a_skill_body` holds, and lines were actually requested. A
        refusal logs a WARNING and a denial count; it names the path, since a refused path may name
        no real skill. The path is bounded with `audit.bounded_repr` before logging on both
        branches, since it arrives verbatim from the model.
        """
        skill = skill_of(file_path)
        recorded = bounded_repr(file_path)
        if not self._allows(file_path):
            record_metric(lambda m: m.increment("chemclaw_skill_reads_denied_total"))
            log_event(
                logger,
                "skill.read_denied",
                "refused a read of %s: it is outside the skills this turn may reach",
                recorded,
                level=logging.WARNING,
                path=recorded,
            )
            return ReadResult(error=REFUSED, file_data=None)
        log_event(logger, "skill.read", "the model read %s", recorded, skill=skill, path=recorded)
        result = super().read(file_path, offset, limit)
        if is_a_skill_body(file_path) and not result.error and not result.no_lines_requested:
            record_metric(
                lambda m: m.increment("chemclaw_skill_loads_total", labels={"skill": skill})
            )
            # Also record the load for this turn's cost row (a counter cannot say which turn), on
            # the same condition as the counter.
            record_skill_loaded(skill)
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Match files, dropping every hit outside a permitted skill.

        Narrowed with `replace` so upstream's `truncated` flag (and any future field) survives; this
        gate only removes matches.
        """
        result = super().glob(pattern, path)
        if not result.matches:
            return result
        return dataclasses.replace(result, matches=self._permitted(result.matches))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        """Search files, dropping every hit outside a permitted skill.

        The keyword-only arguments are forwarded because upstream introspects for them to decide
        where `max_count` applies. Narrowed with `replace` so `truncated` survives, as in `glob`.
        """
        result = super().grep(pattern, path, glob, max_count=max_count, context_lines=context_lines)
        if not result.matches:
            return result
        return dataclasses.replace(result, matches=self._permitted(result.matches))

    def _permitted(self, hits: list[Any]) -> list[Any]:
        """The hits naming a path this turn may reach."""
        return [hit for hit in hits if self._allows(path_of(hit))]

    def download_files(self, paths: list[str]) -> Any:
        """Return the bodies of the permitted paths only.

        This returns full bytes, so it must be gated like `read`. Filtered rather than refused,
        since results are per path, as in `glob` and `grep`.
        """
        return super().download_files([path for path in paths if self._allows(path)])

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: a skill is reviewed judgment, never something a turn may rewrite."""
        raise _refusal(Tier.REPO, _READ_ONLY)

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise _refusal(Tier.REPO, _READ_ONLY)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives.

        A turn that can remove a `SKILL.md` decides what judgment the next turn can load.
        """
        raise _refusal(Tier.REPO, _READ_ONLY)

    def upload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise _refusal(Tier.REPO, _READ_ONLY)


class PermittedStoreBackend(StoreBackend):
    """A stored skills tier: every read bounded by `permits`, every write refused.

    The `StoreBackend` twin of `NarrowedSkillsBackend`, a separate class because the two base
    classes differ in which verbs are native: `aread` and the async write verbs are native on
    `StoreBackend` and are overridden explicitly (the coverage test derives its method list from
    both the protocol and `StoreBackend`). Nothing reaches another person's tier regardless: the
    namespace closes over the actor and mounts are per turn.

    Args:
        namespace: The store namespace factory this tier lives under, per upstream's signature.
        store: The store, passed explicitly because these backends are built outside a graph run.
        tier: Which stored tier this is, so a refused write is derived from `access.may_write`.
        permits: Whether a skill *name* is reachable on this turn, called per reach because the role
        gate reads the turn's ambient identity.
        refusal: What a refused write says, worded for the tier (the personal one names its owner's
        route, the organisation's names an administrator's).
        on_load: Booked once per delivered body, with the skill's name. The tiers count differently:
        a personal skill's name is one person's words and gets a bare counter, an organisation's can
        carry a label.

    Every argument is keyword-only and required, so the narrowing can never be omitted.
    """

    def __init__(
        self,
        *,
        namespace: Any,
        store: Any,
        tier: Tier,
        permits: Callable[[str], bool],
        refusal: str,
        on_load: Callable[[str], None],
    ) -> None:
        """Hold the namespace, the tier, the predicate, the refusal text and the counting hook."""
        super().__init__(namespace=namespace, store=store)
        self._tier = tier
        self._permits = permits
        self._refusal = refusal
        self._on_load = on_load

    def _allows(self, path: str) -> bool:
        """Whether this turn may reach `path` at all — the one question every read asks."""
        skill = skill_of(path)
        return not skill or self._permits(skill)

    def _permitted(self, hits: list[Any]) -> list[Any]:
        """The hits naming a path this turn may reach."""
        return [hit for hit in hits if self._allows(path_of(hit))]

    def _delivered(self, result: Any, path: str) -> None:
        """Book one delivered body, or nothing.

        Derived from the result: a failed read, or one that asked for no lines, delivered nothing.
        """
        if getattr(result, "error", None) is not None:
            return
        if getattr(result, "no_lines_requested", False):
            return
        if is_a_skill_body(path) and (name := skill_of(path)):
            self._on_load(name)

    def ls(self, path: str) -> LsResult:
        """List the tier, keeping only entries belonging to a skill this turn may reach."""
        result = super().ls(path)
        if not result.entries:
            return result
        kept = [entry for entry in result.entries if self._allows(str(entry.get("path", "")))]
        return LsResult(error=result.error, entries=kept)

    def read(self, *args: Any, **kwargs: Any) -> Any:
        """Read one body, refusing anything outside what this turn may reach, and count it.

        Forwarded with `*args, **kwargs` so no copy of upstream's signature can go stale (see
        `_first_argument`).
        """
        path = _first_argument(args, kwargs)
        if not self._allows(path):
            return ReadResult(error=REFUSED, file_data=None)
        result = super().read(*args, **kwargs)
        self._delivered(result, path)
        return result

    async def aread(self, *args: Any, **kwargs: Any) -> Any:
        """The async twin, overridden rather than inherited.

        `StoreBackend.aread` is native rather than a `to_thread` wrapper, so gating `read` alone
        would leave the async path open.
        """
        path = _first_argument(args, kwargs)
        if not self._allows(path):
            return ReadResult(error=REFUSED, file_data=None)
        result = await super().aread(*args, **kwargs)
        self._delivered(result, path)
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Match files, dropping every hit outside what this turn may reach.

        Narrowed with `dataclasses.replace` so `truncated` survives.
        """
        result = super().glob(pattern, path)
        if not result.matches:
            return result
        return _replaced(result, self._permitted(result.matches))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        """Search the tier, dropping every hit outside what this turn may reach.

        The keyword-only arguments are forwarded because upstream introspects for them
        (`protocol._method_accepts_max_count`) to decide where `max_count` applies.
        """
        result = _call_grep(
            super().grep, pattern, path, glob, max_count=max_count, context_lines=context_lines
        )
        if not result.matches:
            return result
        return _replaced(result, self._permitted(result.matches))

    def download_files(self, paths: list[str]) -> Any:
        """Return the bodies of the permitted paths only.

        Filtered rather than refused, since results are per path, as in `glob` and `grep`.
        """
        return super().download_files([path for path in paths if self._allows(path)])

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: a skill is judgment somebody approved, never something a turn may rewrite."""
        raise _refusal(self._tier, self._refusal)

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise _refusal(self._tier, self._refusal)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives.

        A turn that could delete would be one bad inference away from removing judgment somebody
        relies on.
        """
        raise _refusal(self._tier, self._refusal)

    def upload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise _refusal(self._tier, self._refusal)

    async def awrite(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse.

        Overridden because `StoreBackend.awrite` is native rather than a thread wrapper, and it is
        the path an async agent takes.
        """
        raise _refusal(self._tier, self._refusal)

    async def aedit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `awrite` gives."""
        raise _refusal(self._tier, self._refusal)

    async def adelete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `awrite` gives."""
        raise _refusal(self._tier, self._refusal)

    async def aupload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse.

        Stated explicitly rather than relying on the base class falling through to `upload_files`,
        since which async verbs are wrappers is not something to depend on.
        """
        raise _refusal(self._tier, self._refusal)


#: A glob or grep result, narrowed in place so its other fields survive — see `_replaced`.
_Result = TypeVar("_Result", GlobResult, GrepResult)


def _replaced(result: _Result, matches: list[Any]) -> _Result:
    """`result` with its matches narrowed and every other field carried over.

    `dataclasses.replace`, so `truncated` and any future upstream field survive.
    """
    return dataclasses.replace(result, matches=matches)


def _call_grep(
    grep: Any,
    pattern: str,
    path: str | None,
    glob: str | None,
    *,
    max_count: int | None,
    context_lines: int,
) -> GrepResult:
    """Call `grep` with the two keyword arguments upstream may or may not accept.

    Which backend accepts `context_lines` and `max_count` varies, so the call is attempted with both
    and retried without whichever is refused, rather than transcribing the matrix.
    """
    try:
        result = grep(pattern, path, glob, max_count=max_count, context_lines=context_lines)
    except TypeError:
        result = grep(pattern, path, glob, max_count=max_count)
    return cast(GrepResult, result)


def _first_argument(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """The path a `read`/`aread` was given, whichever way upstream's caller spelled the call.

    `file_path` is upstream's own keyword, pinned for the write verbs by
    `tests/test_upstream_surface.py`.
    """
    if args:
        return str(args[0])
    return str(kwargs.get("file_path", ""))


def _count_a_local_load(name: str) -> None:
    """Book one delivered personal-skill body, on the two channels this tier may use.

    A bare counter, because a personal skill's name is private vocabulary and a label would put it
    in a shared exposition. `record_skill_loaded` carries the name in process only (digested before
    it reaches `turn_costs`) for the distiller's self-confirmation guard.
    """
    record_metric(lambda m: m.increment("chemclaw_local_skill_loads_total"))
    record_skill_loaded(name)


def _count_an_org_load(name: str) -> None:
    """Book one delivered org-skill body.

    Labelled by name, unlike the personal tier's bare counter: an org skill's name is deployment
    configuration, not private vocabulary, and cardinality is bounded by `agent_org_skills_max`. It
    gives retirement decisions a usage signal.
    """
    record_metric(lambda m: m.increment("chemclaw_skill_loads_total", labels={"skill": name}))
    record_skill_loaded(name)


def local_skills_backend(
    store: Any, actor: str, permits: Callable[[str], bool]
) -> PermittedStoreBackend:
    """The mounted read half of one chemist's own tier.

    A factory because the namespace, refusal wording and counter are this tier's knowledge.

    Args:
        store: The process's store.
        actor: Whose tier this is, in the turn's own actor spelling.
        permits: `access.SkillNarrowing.stored`, the stored half of this turn's narrowing, applied
            per reach.
    """
    namespace = local_skills_namespace(actor)
    return PermittedStoreBackend(
        namespace=lambda _runtime: namespace,
        store=store,
        tier=Tier.MINE,
        permits=permits,
        refusal=_LOCAL_READ_ONLY,
        on_load=_count_a_local_load,
    )


def org_skills_backend(store: Any, permits: Callable[[str], bool]) -> PermittedStoreBackend:
    """The mounted read half of the organisation's tier.

    A factory here because the refusal wording and the counter are this tier's knowledge.

    Args:
        store: The process's store.
        permits: `access.skill_permits`' composed narrowing, applied per reach.
    """
    return PermittedStoreBackend(
        namespace=lambda _runtime: org_skills_namespace(),
        store=store,
        tier=Tier.ORG,
        permits=permits,
        refusal=_ORG_READ_ONLY,
        on_load=_count_an_org_load,
    )


class ReloadingSkillsState(SkillsState):
    """`SkillsState` with its cached listing moved to a channel the checkpointer cannot restore.

    One redeclared field, and it is the whole reloading mechanism. Upstream annotates
    `skills_metadata` with `PrivateStateAttr` only, so it resolves to a checkpointed `LastValue`;
    `UntrackedValue` is never written to a checkpoint (`checkpoint()` returns `MISSING`), so the
    key is absent at the start of every run of the graph and upstream's own
    `if "skills_metadata" in state: return None` short-circuit simply does not fire.

    Measured over three turns on one thread, counting whether the key was already in state when
    `before_agent` ran: `[False, True, True]` on upstream's channel and `[False, False, False]` on
    this one.

    The same mechanism `ChemclawState.loop_capped` uses, and for the same reason: "per turn" is a
    property of the channel, not of a caller who remembers to clear it.
    """

    skills_metadata: NotRequired[Annotated[list[SkillMetadata], UntrackedValue, PrivateStateAttr]]


class ReloadingSkillsMiddleware(SkillsMiddleware):
    """`SkillsMiddleware` that re-narrows its listing every turn instead of caching it.

    The role gate reads the turn's identity, so a listing cached from one caller or an earlier role
    would be wrong. The subclass is one redeclared state field (`ReloadingSkillsState`), depending
    only on the field name pinned by `tests/test_upstream_surface.py`. A staleness fix, not the
    gate: the backends refuse reads on every call regardless.
    """

    state_schema = ReloadingSkillsState

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """Upstream's listing, except when there is nothing to list — see `NO_SKILLS`.

        Upstream's empty-listing text invites writing a skill, which the backends refuse. A
        private-method override, pinned by `tests/test_upstream_surface.py`.
        """
        if not skills:
            return NO_SKILLS
        return str(super()._format_skills_list(skills))
