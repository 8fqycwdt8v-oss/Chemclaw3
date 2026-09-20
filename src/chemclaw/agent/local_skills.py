"""A chemist's own skills: judgment that shapes their turns and nobody else's.

`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` §3 designed this tier and recorded it
unbuilt, with the reason it was blocked: *"there is no distiller, so nothing writes into a per-actor
directory and nobody can populate one."* That is still true of a *distiller* — `make
trajectory-census` reports zero on a database that has never served a user — so the tier ships with
the other writer that argument overlooked: **the chemist**. They ask for a procedure to be
remembered, the agent drafts the text into its answer, and the person posts it through a route.

**No agent path writes a skill, and that is unchanged rather than relaxed.** `SkillsReadOnlyRefusal`
still refuses every write verb, here as on the shared tree — `_LOCAL_READ_ONLY` below is the same
refusal worded for this root, and `agent/skill_store.PermittedStoreBackend` is what raises it. The
write is an HTTP route a person calls, which is the same shape `api/routes/plan.py` uses for exactly
the same reason: a model must never be able to authorize its own behaviour change.

**The read half moved out, and the tier gained the gate it was missing.** This module used to hold
its own `ReadOnlyStoreBackend`, narrowed in the prompt and not at the backend — so the eval control
arm that removes every skill still handed over the bodies. The backend is now shared with the
organisation's tier and takes a `permits` predicate; `local_skills_backend` below is where this
tier's half of it is decided.

**The invariants this tier owes**, from that ADR, and where each one lives:

- **Per-actor, resolved per turn.** The namespace closes over `get_current_actor()` at backend
  construction, which happens inside `build_langgraph_agent` — per turn, because the graph is. A
  directory resolved once at startup would be one chemist's skills served to everyone.
- **Never a source of shared truth.** Nothing promotes one, nothing cites one, and no other actor's
  turn can name the namespace: `local_skills_namespace` takes the actor and the route is only
  mounted when a turn has one.
- **The user can see what it does.** `api/routes/skills.py` lists, reads and deletes; a behaviour
  change nobody can inspect is the property that makes the *shared* tree need a gate, and this tier
  earns its exemption by being visible to the one person it affects.
- **`SkillsReadOnlyRefusal` unchanged.** See above.

**Storage is the store rather than a directory, and this is the one place that ADR's text is
departed from.** It says "the chemist's own skills directory", and a directory is wrong for the
deployment this ships into: a pod's filesystem is ephemeral and the chart runs `serverReplicas` of
them, so a local skill written to disk would vanish on the next restart and differ between replicas
answering the same chemist. `StoreBackend` over the `AsyncPostgresStore` that already serves
`/memories/` is multi-replica-safe, and — the reason that matters most here — its namespace is the
erasure key `agent/leaver.py` already sweeps by prefix, so a departing person's local skills leave
with their memories instead of needing a second mechanism that could disagree.

**None of the four narrowings applies here, and three of them must not.** `EnabledSkills`,
`ProfileScopedSkills` and `RoleScopedSkills` answer governance questions about a *shared* corpus —
did this deployment turn it on, is this agent about it, may this caller see it — and none has an
answer for a skill whose only reader is the person who wrote it. The first would delete the tier
outright: a deployment that sets `CHEMCLAW_SKILLS_ENABLED` is naming shared skills, so every local
one would fall out of a list it was never going to be in. What bounds this tier instead is
structural and stronger than a predicate — the namespace closes over one actor, so another chemist's
turn cannot reach it at all.

**`ToolScopedSkills` is the one that would have been worth keeping, and it is not applied. Said
plainly because the alternative is prose claiming a narrowing that is not there.** It asks whether
this agent can do any of what a skill teaches, which is as true of a personal skill as a shipped
one, and a local skill about tools the turn cannot reach is misleading in exactly the way that
narrowing exists to prevent. It is absent because the shared tree gets it from `declared_tools`
reading frontmatter off a *directory*, and this tier is stored rather than filed: applying it would
mean parsing every local skill's frontmatter out of the store synchronously, inside a backend whose
whole reason for existing is that it is not a filesystem. The cost of the gap is bounded and
one-sided — a chemist may be offered their own skill about a tool this profile lacks, and the worst
outcome is judgment they wrote being unhelpful to them. `docs/planning/BACKLOG.md` carries the row.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import frontmatter
from deepagents.backends import StoreBackend
from pydantic import ValidationError

from chemclaw.agent.audit import bounded_repr
from chemclaw.agent.refusal_route import routed
from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.agent.skill_manifest import SkillManifest
from chemclaw.agent.skill_store import PermittedStoreBackend
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.turn_signals import record_skill_loaded

logger = logging.getLogger(__name__)

#: The root a chemist's own skills are mounted at, and the label the model sees in their paths.
#:
#: `mine` rather than the actor's digest, which is what the namespace uses: a digest in a path is a
#: stable identifier for a person appearing in the system prompt of every turn and in every log line
#: that quotes one. The route is per-actor by construction — the namespace closes over the turn's
#: own actor — so the path needs to carry no identity at all.
LOCAL_SKILLS_ROOT = "/mine/"

#: The advisory lock one chemist's writes serialize on — see `_one_writer_per_chemist`.
_WRITER_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"

#: The same label without its slashes, for the skills middleware's source list.
LOCAL_SKILLS_LABEL = "mine"

#: What a refused *write* to this tier says. The shared tree's wording names a reviewed commit as
#: the way in, which is wrong here — a local skill changes through a route its owner calls — so the
#: sanctioned path differs while the refusal does not.
_LOCAL_READ_ONLY = routed(
    "your own skills are read-only to a turn — a skill is judgment that reshapes later answers, "
    "so it changes only when you decide it does, not when a turn decides. Nothing was changed.",
    code="local_skills_read_only",
    boundary="the chemist's own skills tier, which no turn may write",
    who_can_act="the chemist who owns it, through the skills route",
    sanctioned_path="draft the skill in your answer and say it can be saved from there",
)


class SkillRefused(ChemclawError):
    """A document this tier will not keep, and whether the reason is a conflict or a fault.

    One exception rather than two, carrying `conflict`, because every caller has to translate it to
    its own surface anyway — a 409 or a 422 at a route, prose at a tool — and two classes would be
    two things to keep in step for one branch.
    """

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        """Refuse, saying whether the name is taken (`conflict`) or the document is malformed."""
        super().__init__(message)
        self.conflict = conflict


def storable_name(name: str) -> bool:
    r"""Whether this name could ever have been written, which is what a read of it may assume.

    **The writer's rule, asked by the readers, because the readers are reachable with anything.**
    `GET /skills/mine/{name}` and its `DELETE` take a path parameter: any byte a URL can carry
    reaches the store. Measured against the shipped Postgres store, `a\x00b` raised
    `psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes` out of both — a
    **500** on a name that cannot exist, where the honest answer is 404 and is what the in-memory
    store already gave. A 500 on caller input is an availability question dressed as a bug report.

    It is a predicate rather than a second copy of the check for the reason the whole tier is
    arranged around: a rule stated at one surface is a rule the other surface does not have, which
    is exactly the hole `validated_skill`'s docstring measures for the two write doors.
    """
    return all(not character.isspace() and character.isprintable() for character in name)


def validated_skill(body: str, *, expected_name: str | None = None) -> str:
    """The name this body declares, or a refusal naming what is wrong with it.

    **Every door into this tier goes through here, and the reason is a hole that was measured.**
    `POST /skills/mine` had these checks and the *acceptance* of a proposal did not, so a document
    refused at one door was written at the other. Driven before this function existed, all three
    refused bodies landed: a skill taking a shipped skill's name, a body that is not a `SKILL.md`
    at all, and one at 40,000 characters against a 16,000 cap — 409, 422, 422 at the save route and
    **200, written** at the accept route. The module that opened the second door argued in its own
    docstring that "a second door into one bound is a hole in it" and then closed one bound of four.

    So the admission rules live with the tier rather than with a surface, and a new way in gets them
    by calling this. The callers are the save route, the accept route, `propose_skill`, the
    distiller before it files, and — since the organisation's tier shipped — its publish and revert
    routes too.

    **It is deliberately not per tier.** `agent_local_skill_max_chars` reads as the personal tier's
    number and bounds what a *skill* is — judgment rather than a transcript — which is as true of
    one an administrator publishes as of one a chemist keeps. A second char cap for the second tier
    would be the second door into one bound that this function exists to prevent, so the wording
    above names no tier.

    Args:
        body: The whole `SKILL.md`, frontmatter included.
        expected_name: What the caller believes the name is, when it has an independent opinion —
            `propose_skill` takes a `name` argument beside the body. Two sources of one name can
            disagree and the reader believes whichever the code consults, so they are compared
            rather than one being preferred.

    Returns:
        The validated name, which is also the skill's directory and its store key.

    Raises:
        SkillRefused: With `conflict` set when the name is a shipped skill's — the one refusal that
            is about the deployment rather than about the document.
    """
    if len(body) > settings.agent_local_skill_max_chars:
        raise SkillRefused(
            f"a skill may be at most {settings.agent_local_skill_max_chars} characters and this "
            f"one is {len(body)}. A skill is judgment, not a transcript."
        )
    try:
        parsed = frontmatter.loads(body)
    except Exception as error:
        raise SkillRefused(
            f"the skill's frontmatter could not be parsed: {error}. It must open with `---`, a "
            "`name:` and a `description:`, then `---`."
        ) from error
    try:
        manifest = SkillManifest.model_validate(parsed.metadata)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors()
        )
        raise SkillRefused(f"the skill's frontmatter is not valid: {problems}") from error
    if expected_name is not None and manifest.name != expected_name.strip():
        raise SkillRefused(
            f"the frontmatter declares the name {manifest.name!r} and the name given beside it is "
            f"{expected_name.strip()!r}. Send the same name in both, since the frontmatter is what "
            "a later turn reads."
        )
    if "/" in manifest.name or manifest.name.startswith("."):
        raise SkillRefused("a skill name may not contain '/' or start with '.'")
    if not storable_name(manifest.name):
        raise SkillRefused(
            "a skill name may not contain whitespace or control characters — use hyphens."
        )
    # Imported here rather than at module scope because `langgraph_agent` imports *this* module —
    # the cycle is real, and the call is per save rather than per turn. `declared_tools` is cached
    # on the directory tuple, so the cost is one `Path.is_dir()` fan-out.
    from chemclaw.agent.langgraph_agent import shipped_skill_names

    if manifest.name in shipped_skill_names():
        raise SkillRefused(
            f"{manifest.name!r} is the name of a skill this deployment already ships; give yours a "
            "different name so it is clear which judgment is acting",
            conflict=True,
        )
    return manifest.name


#: The in-process tools whose only outcome is a personal skill, and which are therefore not bound
#: where `personal_skills_available()` is false. A name rather than a literal in the builder,
#: because the set belongs to the tier: whoever adds the second such tool adds it here and the
#: builder needs no edit.
PERSONAL_TIER_TOOLS = frozenset({"propose_skill"})


def personal_skills_available() -> bool:
    """Whether this deployment can keep a chemist's own skill at all.

    Two gates, both necessary. `agent_memory_enabled` is the deployment's decision that
    agent-authored files may outlive a session; `session_store` is the same condition the
    checkpointer reads, because the store shares its pool and a process on the in-memory store has
    no Postgres to put one in.

    **One function because three surfaces read it, and the third read it by not reading it.**
    `api/runner.turn_store` mounts `/mine` on these conditions and `api/routes/skills.py` refuses on
    them, but `propose_skill` was bound on every model call with no condition at all — so under the
    shipped defaults of the day (`agent_memory_enabled` was False) the model spent the tool's schema
    on every request, wrote a row into a store that dies with the process, and told the chemist to
    go accept something `POST /proposals/...` answers 503 to. A tool whose only outcome is
    unreachable is not a capability, and this is the predicate that says so.

    **Both halves are now True on the shipped configuration**
    (`D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius` flipped the first; the chart has
    always pinned the second), so what this predicate guards today is a deployment that turned
    durable memory *off* — which loses the whole human gate on agent-proposed behaviour with it, and
    is why the chart states the posture rather than inheriting it.

    Returns:
        True where a proposal has somewhere durable to land and a route that can accept it.
    """
    return settings.agent_memory_enabled and settings.session_store == "postgres"


def local_skills_namespace(actor: str) -> tuple[str, ...]:
    """The store namespace one person's own skills live under.

    Digested for the two reasons `scratchpad.memory_namespace` gives and shares its shape with:
    `store` validates namespace components against a character class that rejects most punctuation,
    and an actor has more than one spelling in this system (`unverified:<id>` is the other), so a
    digest is always legal and collapses neither spelling into the other.

    A **different first component** from `memories`, so the two tiers are separately erasable and
    separately countable, and so a bug in one cannot serve the other's rows.

    Args:
        actor: The turn's actor id, in whichever spelling the caller holds.

    Returns:
        The namespace tuple, stable for one actor across processes and restarts.
    """
    return ("local-skills", stable_hash(actor))


def local_skills_prefix(actor: str) -> str:
    """The `store.prefix` value naming one person's own skills, for the erasure sweep.

    Exposed so `agent/leaver.py` builds the same string this module writes under rather than
    re-deriving the join — the defect class where two modules agree about a key until one is edited.

    Args:
        actor: The departing person's id.

    Returns:
        The dotted prefix under which every local skill of theirs is stored.
    """
    return ".".join(local_skills_namespace(actor))


def _count_a_local_load(name: str) -> None:
    """Book one delivered personal-skill body, on the two channels this tier may use.

    **A separate bare counter rather than the labelled one**, for the reason the metric's own HELP
    gives: a local skill's name is a person's words, and a label would mint a series per private
    project name in a shared exposition that no erasure reaches.

    `record_skill_loaded` carries the name in process only — the ledger digests it before it reaches
    `turn_costs` (`agent/skill_fingerprint.py`), because that row is retained through erasure. It is
    what `agent/distiller.py`'s self-confirmation guard reads, so a tier that did not call it would
    let a trajectory this skill was already teaching count as evidence for proposing it.
    """
    record_metric(lambda m: m.increment("chemclaw_local_skill_loads_total"))
    record_skill_loaded(name)


def local_skills_backend(
    store: Any, actor: str, permits: Callable[[str], bool]
) -> PermittedStoreBackend:
    """The mounted read half of one chemist's own tier.

    A factory rather than a constructor call at the mount point, because three of the five arguments
    are *this tier's* rather than the caller's — the namespace, the refusal wording and which
    counter a load books against. `agent/scratchpad.py` composes routes; it should not also be the
    place that knows a personal skill's name is private.

    Args:
        store: The process's store.
        actor: Whose tier this is, in the turn's own actor spelling.
        permits: `agent/skill_access.skill_permits`' composed narrowing, applied per reach.
    """
    namespace = local_skills_namespace(actor)
    return PermittedStoreBackend(
        namespace=lambda _runtime: namespace,
        store=store,
        permits=permits,
        refusal=_LOCAL_READ_ONLY,
        on_load=_count_a_local_load,
    )


#: The document inside a skill directory that makes it a skill, mirroring the shared tree's shape so
#: one chemist's own tier is discovered by the same rule as `skills/` and reads the same to a model.
LOCAL_SKILL_FILENAME = "SKILL.md"


def _key(name: str) -> str:
    """The store key one local skill's body lives under.

    Leading slash and trailing `SKILL.md`, because that is the shape `StoreBackend` writes and reads
    — measured rather than assumed, since a key this module invented would be a second definition
    that upstream could walk away from on a bump.
    """
    return f"/{name}/{LOCAL_SKILL_FILENAME}"


def _writer(store: Any, actor: str) -> StoreBackend:
    """A writable backend over one person's own skills, for the route that saves them.

    **Plain `StoreBackend`, deliberately, and this is the only place one is built.** The turn's
    backend is a `PermittedStoreBackend` over the same namespace; this is the other side of the same
    tier, reached from an HTTP route a person calls rather than from anything a model holds. Using
    upstream's writer rather than putting rows in directly is what keeps the stored shape — the
    key, the `content`/`encoding` pair, the two timestamps — with exactly one definition, so a
    bump that changes it changes both halves together.
    """
    namespace = local_skills_namespace(actor)
    return StoreBackend(namespace=lambda _runtime: namespace, store=store)


@asynccontextmanager
async def _one_writer_per_chemist(actor: str) -> AsyncIterator[None]:
    """Serialize this chemist's saves, so the row cap is a bound rather than a suggestion.

    **The cap was check-then-write with nothing between the two.** Measured against a real
    `AsyncPostgresStore` at a cap of 3: twelve concurrent `POST /skills/mine` calls all read the
    same pre-write count, all passed, and all twelve were written. The acceptance door had the same
    shape at a cap of 2 and wrote eight. The bound's stated purpose is prompt-prefix spend — every
    personal skill is in the prompt of every turn its owner takes — so a cap that concurrency lifts
    is not bounding the thing it exists to bound.

    A transaction-scoped advisory lock keyed on the actor, which is the shape
    `agent/behaviour_proposals.py` uses one table over and releases on commit. It is per chemist, so
    two people never contend, and it is taken on the session-store database rather than through the
    `store` backend because the count and the write are two calls through somebody else's object and
    cannot be one transaction — `tests/test_scratchpad.py` is what forbids reaching past the backend
    to do it the other way.

    **Conditioned on the backend rather than guarded by an `except`.** The tier is only reachable
    when `turn_store()` answers, which needs `session_store="postgres"`; a test driving the writer
    directly over an in-memory store has no database to lock on and no concurrency to lose, so the
    lock is skipped explicitly rather than attempted and swallowed.
    """
    if settings.session_store != "postgres":
        yield
        return
    async with _session_connection(_session_dsn()) as conn:
        await conn.execute(_WRITER_LOCK, (f"local-skills\x1f{actor}",))
        try:
            yield
        finally:
            await conn.commit()


async def save_local_skill(store: Any, actor: str, name: str, body: str) -> None:
    """Write one of a chemist's own skills, replacing any earlier version of that name.

    No merge and no version history: a skill is judgment its owner is asserting *now*, and a tier
    that accumulated every draft would make "what is acting on my turns" a question with a list for
    an answer. The route validates `body` as a `SkillManifest` before calling this, so a malformed
    skill is a 422 rather than a file the listing then skips.

    **This write is outside the tool-call chain, and what stands in its place is stated rather than
    assumed.** `tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly`
    holds that every store write arrives as a `write_file` tool call, because that is what crosses
    the audit row, the authorization gate, the dry-run refusal and the repeat guard — and the reason
    it gives is that a direct write "would do so silently: nothing fails, the memory is simply
    written with no record that it was."

    There is no tool call here to cross anything: the write comes from an HTTP route a *person*
    calls, which is the whole design (`D-2026-09-18-…`), and three of those four controls have no
    subject without a turn. The fourth — authorization — is the route's `CurrentUser` plus the fact
    that the namespace is derived from the caller rather than taken from them, so there is no
    decision here to get wrong. What would otherwise be lost is the *record*, so it is written: an
    INFO naming who changed which skill, which is the same thing `skill_backend.read` does for the
    other direction and the only reason this write is not silent.

    Args:
        store: The process's store.
        actor: Whose tier to write, in the turn's own actor spelling.
        name: The skill's name, which is also its directory.
        body: The whole `SKILL.md`, frontmatter included.
    """
    async with _one_writer_per_chemist(actor):
        held = await list_local_skills(store, actor)
        # Refused rather than evicted, and counted inside the lock so the cap binds the tier rather
        # than trailing it by however many requests arrived together. Replacing a skill already held
        # is not a new row, so it is allowed at the cap — otherwise a chemist at the limit could not
        # correct any of them.
        if name not in held and len(held) >= settings.agent_local_skills_max:
            raise SkillRefused(
                f"you already keep {len(held)} personal skills, which is this deployment's limit "
                f"of {settings.agent_local_skills_max}: every one of them is in the prompt of "
                "every turn you take, so remove one before adding another",
                conflict=True,
            )
        # **A name the organisation already publishes is refused here, and the reverse is not.**
        # The two tiers mount side by side and a collision resolves by source order, so one of them
        # loses silently; this makes the direction a decision instead of an accident. A person
        # cannot take a name the whole deployment is using — they would be writing judgment that
        # never acts, since `_skills_middleware` puts `/org` after `/mine`. An administrator *can*
        # take a name somebody already uses privately, because the alternative is a deployment-wide
        # publication blocked by one person's private vocabulary, which nobody could have seen
        # coming and nobody can resolve without being told whose it is.
        from chemclaw.agent.org_skills import list_org_skills

        if name in await list_org_skills(store):
            raise SkillRefused(
                f"{name!r} is the name of a skill your organisation publishes to everyone, so a "
                "personal one by that name would never act — give yours a different name",
                conflict=True,
            )
        await _writer(store, actor).awrite(_key(name), body)
    log_event(
        logger,
        "local_skill.saved",
        "%s saved their own skill %s",
        bounded_repr(actor),
        bounded_repr(name),
        actor=bounded_repr(actor),
        skill=bounded_repr(name),
        chars=len(body),
    )


#: How many rows one page of the listing walk asks for.
#:
#: The same shape — and the same reason — as `scratchpad._EVICTION_PAGE`: `BaseStore.asearch`
#: defaults to `limit=10`, which is not a page size a caller chose, it is a default a caller who
#: passed nothing inherited. This tier's cap is `agent_local_skills_max`, so one page normally
#: answers the whole namespace and the loop runs once.
_LISTING_PAGE = 100


async def list_local_skills(store: Any, actor: str) -> list[str]:
    """The names of one chemist's own skills, sorted — **all** of them.

    Read off the store rather than off the mounted backend, because this answers a *route's*
    question — "what is acting on my turns" — and the route has no turn and therefore no mount.

    **Paged, because the un-paged spelling answered ten.** `store.asearch(namespace)` with no
    `limit` is not "everything", it is `BaseStore.asearch`'s default of 10 — so a chemist with
    twelve saved skills was listed ten while the prompt carried twelve, and the two beyond the page
    were undeletable through the route that exists to remove them. That falsifies the licence this
    tier holds its exemption under: the ADR grants it *on the condition* that a person can see what
    is acting on their turns and withdraw it. A listing that is confidently short is worse than no
    listing, because it answers the question wrongly rather than not at all.

    The walk terminates on a page that adds nothing, which also ends it against a store that
    ignores `offset` — the same guard `scratchpad.BoundedStoreBackend` carries for the same reason.
    """
    namespace = local_skills_namespace(actor)
    held: dict[str, Any] = {}
    while True:
        page = await store.asearch(namespace, limit=_LISTING_PAGE, offset=len(held))
        fresh = {item.key: item for item in page if item.key not in held}
        if not fresh:
            break
        held.update(fresh)
    suffix = f"/{LOCAL_SKILL_FILENAME}"
    return sorted(key[1 : -len(suffix)] for key in held if key.endswith(suffix))


async def read_local_skill(store: Any, actor: str, name: str) -> str | None:
    """One of a chemist's own skills, verbatim, or `None` if they have no skill by that name.

    A name the writer would have refused is answered as absent rather than passed to the store —
    see `storable_name`, which measured a 500 on the shipped backend for a name that cannot exist.
    """
    if not storable_name(name):
        return None
    item = await store.aget(local_skills_namespace(actor), _key(name))
    if item is None:
        return None
    content = item.value.get("content")
    return content if isinstance(content, str) else None


async def delete_local_skill(store: Any, actor: str, name: str) -> bool:
    """Remove one of a chemist's own skills. Returns whether there was one to remove.

    The half of "the user can see what it does" that makes the rest worth having: an inspectable
    behaviour change nobody can withdraw is a worse bargain than one nobody can see, because the
    person has learned there is something acting on them and still cannot stop it.
    """
    if not storable_name(name) or (
        await store.aget(local_skills_namespace(actor), _key(name)) is None
    ):
        return False
    # Through the same backend the write uses rather than `store.adelete`, so both halves of this
    # tier's lifecycle go through upstream's own code and the stored shape keeps one definition.
    # It is also what `tests/test_scratchpad.py` asks for structurally — no first-party module
    # reaches past the backend to the store's write verbs.
    await _writer(store, actor).adelete(_key(name))
    log_event(
        logger,
        "local_skill.removed",
        "%s removed their own skill %s",
        bounded_repr(actor),
        bounded_repr(name),
        actor=bounded_repr(actor),
        skill=bounded_repr(name),
    )
    return True


# **The tier's two bounds are `agent_local_skill_max_chars` and `agent_local_skills_max`**, and
# they live in `core/config/agent.py` with their arithmetic rather than here, because this
# repository's rule is that a threshold is a setting. The route enforces both, and the second one
# is the one that was missing: `agent_memory_max_files` is enforced by
# `scratchpad.BoundedStoreBackend`, which mounts `/memories/` and not this root, so nothing on
# either half of this tier ever counted a row. The row cap is a bound on **prefix** spend rather
# than on storage — a local skill's name and description sit in the system message of every model
# call its owner makes — and it is refused rather than evicted, because judgment a person authored
# may not vanish because they wrote one more.
