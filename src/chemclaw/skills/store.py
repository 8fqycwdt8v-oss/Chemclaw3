"""The skill store: three tiers behind one interface, over the process's durable store.

The reviewed tree (`Tier.REPO`) is files, changed by a commit. The organisation's tier (`Tier.ORG`)
and a chemist's own (`Tier.MINE`) are rows in the Postgres store under one key shape
(`/<name>/SKILL.md`), because pod filesystems are ephemeral and replicated. `SkillStore` is the one
way routes and the turn's mounts reach them; every write asks `access.may_write` first, and no
agent path holds a `SkillStore` writer. Namespaces are the erasure and tenancy key: a chemist's is
per actor (`agent/leaver.py` sweeps it by prefix), the organisation's is the tenant's, and the
version history of an organisation skill sits beside it so a revert names bytes already held.
`backends.py` is the read half a turn mounts; it is not imported here, so the front door stays free
of `deepagents`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter

from chemclaw.agent.audit import bounded_repr
from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.tenancy import namespace_component
from chemclaw.skills.access import SkillWriteRefused, Tier, Writer, may_write
from chemclaw.skills.manifest import (
    SKILL_FILENAME,
    UNREADABLE_DECLARATION,
    SkillRefused,
    declared_tools,
    declared_triple,
    storable_name,
)

if TYPE_CHECKING:
    from deepagents.backends import StoreBackend

    from chemclaw.skills.backends import PermittedStoreBackend

logger = logging.getLogger(__name__)

#: How many rows one page of a namespace walk asks for. `BaseStore.asearch` defaults to `limit=10`,
#: which would silently truncate a listing.
LISTING_PAGE = 100

#: The transaction-scoped advisory lock a stored tier's writes serialize on — see
#: `advisory_writer_lock`.
_WRITER_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"

#: The root a chemist's own skills are mounted at, and the label the model sees in their paths.
#: `mine` rather than an actor digest, so no person identifier appears in prompts or logs.
LOCAL_SKILLS_ROOT = "/mine/"

#: The same label without its slashes, for the skills middleware's source list.
LOCAL_SKILLS_LABEL = "mine"

#: The root the organisation's skills are mounted at, and the label the model sees in their paths;
#: it names no tenant because it appears in every prompt.
ORG_SKILLS_ROOT = "/org/"

#: The same label without its slashes, for the skills middleware's source list.
ORG_SKILLS_LABEL = "org"

#: In-process tools whose only outcome is a personal skill, so they are not bound where
#: `personal_skills_available()` is false.
PERSONAL_TIER_TOOLS = frozenset({"propose_skill"})


# --- what both stored tiers share: keys, paging and the writer lock ---


async def paged_items(store: Any, namespace: tuple[str, ...]) -> dict[str, Any]:
    """Every item in one namespace, keyed by store key — the whole tier rather than one page.

    One paging walk shared by each tier's listing, the organisation tier's version listing and cap,
    and `stored_skill_declarations`. (`agent/scratchpad.py`'s eviction walk stays separate because
    it deletes as it goes.) It stops on a page that adds nothing, which also terminates against a
    store that ignores `offset`.

    Args:
        store: The process's store.
        namespace: The namespace tuple to walk.

    Returns:
        `{store key: item}`, where an item carries both `.key` and `.value`, so a caller wanting the
        bodies needs no second round trip.
    """
    held: dict[str, Any] = {}
    while True:
        page = await store.asearch(namespace, limit=LISTING_PAGE, offset=len(held))
        fresh = {item.key: item for item in page if item.key not in held}
        if not fresh:
            break
        held.update(fresh)
    return held


def skill_key(name: str) -> str:
    """The store key one stored skill's body lives under, in either tier.

    Leading slash and trailing `SKILL.md`: the shape `StoreBackend` itself writes and reads.
    """
    return f"/{name}/{SKILL_FILENAME}"


def name_of_key(key: str) -> str | None:
    """The skill a store key names, or `None` for a key that is not a skill body.

    The strict inverse of `skill_key`: a key with no leading segment (`/SKILL.md`) or no leading
    slash is not a skill, so it never lists as an empty name.
    """
    suffix = f"/{SKILL_FILENAME}"
    if not key.startswith("/") or not key.endswith(suffix) or len(key) <= len(suffix) + 1:
        return None
    return key[1 : -len(suffix)]


def store_writer(store: Any, namespace: tuple[str, ...]) -> StoreBackend:
    """A writable backend over one stored tier's namespace, for the routes that change it.

    Plain `StoreBackend`: this is reached from an HTTP route a person calls, not from anything a
    model holds. Using upstream's writer rather than `store.aput` keeps the stored shape singly
    defined, and `tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly`
    holds that no module bypasses a backend.
    """
    from deepagents.backends import StoreBackend

    return StoreBackend(namespace=lambda _runtime: namespace, store=store)


@asynccontextmanager
async def advisory_writer_lock(lock_key: str) -> AsyncIterator[None]:
    """Serialize a stored tier's writes under `lock_key`, so a row cap is a bound, not a suggestion.

    Without it, concurrent writers all read the same pre-write count and all pass the cap. Each tier
    keys the lock on what its cap counts (a chemist, an org skill), so unrelated writers never
    contend. A transaction-scoped advisory lock on the session-store database, because the count and
    the write go through the store backend and cannot share a transaction. Skipped explicitly when
    there is no Postgres session store (in-memory tests), where there is nothing to lock on.
    """
    if settings.session_store != "postgres":
        yield
        return
    async with _session_connection(_session_dsn()) as conn:
        await conn.execute(_WRITER_LOCK, (lock_key,))
        try:
            yield
        finally:
            await conn.commit()


async def list_skill_names(store: Any, namespace: tuple[str, ...]) -> list[str]:
    """The names of every skill one stored tier holds, sorted — **all** of them.

    Paged through `paged_items` and parsed through `name_of_key`, so a key that is not a skill body
    lists as nothing.
    """
    held = await paged_items(store, namespace)
    return sorted(name for key in held if (name := name_of_key(key)) is not None)


async def read_skill_body(store: Any, namespace: tuple[str, ...], name: str) -> str | None:
    """One stored skill's body, verbatim, or `None` if the tier has no skill by that name.

    A name the writer would have refused (`storable_name`) is answered as absent rather than passed
    to the store.
    """
    if not storable_name(name):
        return None
    item = await store.aget(namespace, skill_key(name))
    if item is None:
        return None
    content = item.value.get("content")
    return content if isinstance(content, str) else None


# --- a chemist's own tier ---


def personal_skills_available() -> bool:
    """Whether this deployment can keep a chemist's own skill at all.

    Requires `agent_memory_enabled` (agent-authored files may outlive a session) and a Postgres
    `session_store` (the store shares its pool). One predicate for the mount, the route and the
    binding of `PERSONAL_TIER_TOOLS`, so a tool whose only outcome is unreachable is not offered.

    Returns:
        True where a proposal has somewhere durable to land and a route that can accept it.
    """
    return settings.agent_memory_enabled and settings.session_store == "postgres"


def local_skills_namespace(actor: str) -> tuple[str, ...]:
    """The store namespace one person's own skills live under.

    Digested like `scratchpad.memory_namespace`: always a legal namespace component, and an actor's
    two spellings stay distinct. A different first component from `memories`, so the tiers are
    separately erasable and countable.

    Args:
        actor: The turn's actor id, in whichever spelling the caller holds.

    Returns:
        The namespace tuple, stable for one actor across processes and restarts.
    """
    return (namespace_component("local-skills"), stable_hash(actor))


def local_skills_prefix(actor: str) -> str:
    """The `store.prefix` value naming one person's own skills, for the erasure sweep.

    Exposed so `agent/leaver.py` uses this module's key rather than re-deriving it.
    """
    return ".".join(local_skills_namespace(actor))


def _one_writer_per_chemist(actor: str) -> AbstractAsyncContextManager[None]:
    """Serialize this chemist's saves, so the row cap is a bound rather than a suggestion.

    Per chemist via `advisory_writer_lock`, so two people never contend.
    """
    return advisory_writer_lock(f"local-skills\x1f{actor}")


async def save_local_skill(store: Any, actor: str, name: str, body: str) -> None:
    """Write one of a chemist's own skills, replacing any earlier version of that name.

    No version history: a skill is what its owner asserts now. This write comes from a route a
    person calls, not a tool call, so it crosses no audit middleware; authorization is the route's
    `CurrentUser` plus a namespace derived from the caller, and an INFO line records who changed
    which skill.

    Args:
        store: The process's store.
        actor: Whose tier to write, in the turn's own actor spelling.
        name: The skill's name, which is also its directory.
        body: The whole `SKILL.md`, frontmatter included.
    """
    async with _one_writer_per_chemist(actor):
        held = await list_local_skills(store, actor)
        # Refused rather than evicted, counted inside the lock. Replacing a held skill is allowed at
        # the cap so one can always be corrected.
        if name not in held and len(held) >= settings.agent_local_skills_max:
            raise SkillRefused(
                f"you already keep {len(held)} personal skills, which is this deployment's limit "
                f"of {settings.agent_local_skills_max}: every one of them is in the prompt of "
                "every turn you take, so remove one before adding another",
                conflict=True,
            )
        # A name the organisation already publishes is refused here (it would never act, since
        # `/org` is mounted after `/mine`). The reverse is allowed, so one person's private name
        # cannot block a deployment-wide publication.
        if name in await list_org_skills(store):
            raise SkillRefused(
                f"{name!r} is the name of a skill your organisation publishes to everyone, so a "
                "personal one by that name would never act — give yours a different name",
                conflict=True,
            )
        await store_writer(store, local_skills_namespace(actor)).awrite(skill_key(name), body)
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


async def list_local_skills(store: Any, actor: str) -> list[str]:
    """The names of one chemist's own skills, sorted — **all** of them.

    Read off the store because the route has no turn and so no mount. Paged
    (`list_skill_names`), since the store's default page is 10 and a chemist must be able to see and
    delete every skill acting on their turns.
    """
    return await list_skill_names(store, local_skills_namespace(actor))


async def read_local_skill(store: Any, actor: str, name: str) -> str | None:
    """One of a chemist's own skills, verbatim, or `None` if they have no skill by that name.

    A name the writer would refuse is answered as absent (see `storable_name`).
    """
    return await read_skill_body(store, local_skills_namespace(actor), name)


async def delete_local_skill(store: Any, actor: str, name: str) -> bool:
    """Remove one of a chemist's own skills. Returns whether there was one to remove."""
    if not storable_name(name) or (
        await store.aget(local_skills_namespace(actor), skill_key(name)) is None
    ):
        return False
    # Through the same backend the write uses, so no first-party module reaches the store's write
    # verbs directly.
    await store_writer(store, local_skills_namespace(actor)).adelete(skill_key(name))
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


# --- the organisation's tier ---


def org_skills_namespace() -> tuple[str, ...]:
    """The store namespace the active bodies live under.

    No actor component: the tier is the organisation's, and every session's prefix stays
    byte-identical. A distinct first component from `memories` and `local-skills` keeps the tiers
    separately countable and erasable. With tenancy on, a tenant other than `default` has a tier of
    its own: the organisation is the tenant.
    """
    return (namespace_component("org-skills"),)


def org_versions_namespace(name: str) -> tuple[str, ...]:
    """The store namespace one org skill's activated bodies live under.

    Per name, so a listing walks one skill's versions and the cap is per skill. Never mounted; a
    route reads it, so its documents may be JSON.
    """
    return (namespace_component("org-skills-versions"), name)


@dataclass(frozen=True)
class OrgSkillVersion:
    """One body that was once the organisation's judgment, as the version store answers it."""

    content_hash: str
    body: str
    activated_by: str
    activated_at: str


def _version_key(digest: str) -> str:
    """The key one activated body is held under.

    Its content hash, so re-activating the same bytes writes the same row.
    """
    return f"/{digest}"


def content_hash(body: str) -> str:
    """The identity of one document.

    `stable_hash`, the same function proposals and the note index use, so a revert target has one
    spelling.
    """
    return stable_hash(body)


def _one_writer_per_org(name: str) -> AbstractAsyncContextManager[None]:
    """Serialize writes to one org skill, so the row cap is a bound and an activation is not split.

    `advisory_writer_lock`, keyed per name so two administrators publishing different skills never
    contend; the cap is re-read under each lock.
    """
    return advisory_writer_lock(f"org-skills\x1f{name}")


async def list_org_skills(store: Any) -> list[str]:
    """The names of every skill the organisation keeps, sorted — **all** of them.

    Paged through `paged_items`; the store's default page is 10.
    """
    return await list_skill_names(store, org_skills_namespace())


async def read_org_skill(store: Any, name: str) -> str | None:
    """One org skill's active body, verbatim, or `None` if there is no skill by that name.

    A name the writer would refuse is answered as absent rather than passed to the store.
    """
    return await read_skill_body(store, org_skills_namespace(), name)


async def list_org_versions(store: Any, name: str) -> list[OrgSkillVersion]:
    """Every body ever activated under `name`, newest activation first.

    The blame half of rollback: what changed, when, and by whom. Open to every authenticated caller,
    since the tier is in everyone's prompt.
    """
    if not storable_name(name):
        return []
    held = await paged_items(store, org_versions_namespace(name))
    versions = [_version_of(item) for item in held.values()]
    return sorted(
        (version for version in versions if version is not None),
        key=lambda version: version.activated_at,
        reverse=True,
    )


def _version_of(item: Any) -> OrgSkillVersion | None:
    """One stored version record, or `None` for a document this module did not write.

    Tolerant so one malformed row cannot take the listing route down.
    """
    content = item.value.get("content")
    if not isinstance(content, str):
        return None
    try:
        held = json.loads(content)
    except ValueError:
        return None
    if not isinstance(held, dict) or not isinstance(held.get("body"), str):
        return None
    return OrgSkillVersion(
        content_hash=str(item.key).lstrip("/"),
        body=held["body"],
        activated_by=str(held.get("activated_by", "")),
        activated_at=str(held.get("activated_at", "")),
    )


async def _record_a_version(store: Any, name: str, body: str, activated_by: str) -> None:
    """Hold these bytes as a revert target, and evict the least recently activated past the cap.

    Written before the active pointer moves, so a failure in between leaves an orphan version rather
    than losing bytes. Evicted rather than refused: a skill edited often must still accept a fix.
    """
    versions = store_writer(store, org_versions_namespace(name))
    digest = content_hash(body)
    await versions.awrite(
        _version_key(digest),
        json.dumps(
            {
                "body": body,
                "activated_by": activated_by,
                "activated_at": datetime.now(UTC).isoformat(),
            }
        ),
    )
    held = await paged_items(store, org_versions_namespace(name))
    cap = settings.agent_org_skill_versions_max
    if len(held) <= cap:
        return
    # `updated_at` is the store's own, written by the backend above, so "least recently activated"
    # is a fact the store holds rather than one this module would have to maintain beside it.
    stale = sorted(held.items(), key=lambda pair: str(pair[1].updated_at))[: len(held) - cap]
    for key, _item in stale:
        await versions.adelete(key)
    log_event(
        logger,
        "org_skill.versions_evicted",
        "dropped %d old version(s) of %s past the cap of %d",
        len(stale),
        bounded_repr(name),
        cap,
        level=logging.WARNING,
        skill=bounded_repr(name),
        dropped=len(stale),
        cap=cap,
    )


async def save_org_skill(store: Any, name: str, body: str, *, activated_by: str) -> None:
    """Publish one skill to the whole deployment, holding the bytes it replaces.

    The route validates `body` through `manifest.validated_skill` first.

    Args:
        store: The process's store.
        name: The skill's name, which is also its directory and its key.
        body: The whole `SKILL.md`, frontmatter included.
        activated_by: The administrator's oid, for the version record's blame half.

    Raises:
        SkillRefused: The tier already holds `agent_org_skills_max` other skills.
    """
    async with _one_writer_per_org(name):
        held = await list_org_skills(store)
        # Refused rather than evicted, counted inside the lock. Replacing a held skill is allowed at
        # the cap so one can always be corrected.
        if name not in held and len(held) >= settings.agent_org_skills_max:
            raise SkillRefused(
                f"this deployment already keeps {len(held)} organisation skills, which is its "
                f"limit of {settings.agent_org_skills_max}: every one of them is in the prompt of "
                "every turn every chemist takes, and again in every helper a turn spawns, so "
                "retire one before publishing another",
                conflict=True,
            )
        await _record_a_version(store, name, body, activated_by)
        await store_writer(store, org_skills_namespace()).awrite(skill_key(name), body)
    log_event(
        logger,
        "org_skill.published",
        "%s published the organisation skill %s",
        bounded_repr(activated_by),
        bounded_repr(name),
        actor=bounded_repr(activated_by),
        skill=bounded_repr(name),
        chars=len(body),
    )


async def activate_org_version(store: Any, name: str, digest: str, *, activated_by: str) -> bool:
    """Make a body this tier already holds the active one again. Returns whether it was found.

    A revert names a held hash, so what becomes active is byte-for-byte what stood before. It goes
    through `save_org_skill`, so it spends the same cap, takes the same lock and records a version.
    """
    if not storable_name(name):
        return False
    item = await store.aget(org_versions_namespace(name), _version_key(digest))
    version = _version_of(item) if item is not None else None
    if version is None:
        return False
    await save_org_skill(store, name, version.body, activated_by=activated_by)
    log_event(
        logger,
        "org_skill.reverted",
        "%s reverted the organisation skill %s to %s",
        bounded_repr(activated_by),
        bounded_repr(name),
        bounded_repr(digest),
        actor=bounded_repr(activated_by),
        skill=bounded_repr(name),
        content_hash=bounded_repr(digest),
    )
    return True


async def retire_org_skill(store: Any, name: str, *, retired_by: str) -> bool:
    """Stop one org skill acting, keeping its history. Returns whether there was one to retire.

    Retiring is not reverting; the version namespace is left alone so `activate_org_version` can
    bring any held body back.
    """
    if not storable_name(name) or (
        await store.aget(org_skills_namespace(), skill_key(name)) is None
    ):
        return False
    await store_writer(store, org_skills_namespace()).adelete(skill_key(name))
    log_event(
        logger,
        "org_skill.retired",
        "%s retired the organisation skill %s",
        bounded_repr(retired_by),
        bounded_repr(name),
        actor=bounded_repr(retired_by),
        skill=bounded_repr(name),
    )
    return True


# --- what the stored tiers declare about tools ---


@dataclass(frozen=True)
class StoredSkillTools:
    """The `tools:` and `requires:` declarations of every stored skill a turn can reach.

    The same pair of maps the filed trees produce, so `access.skill_permits` merges them without
    knowing two kinds of tier exist. Treat both as read-only: `frozen=True` does not freeze the
    dicts. A type rather than a tuple, so call sites say which map is which.
    """

    declared: dict[str, frozenset[str]] = field(default_factory=dict)
    required: dict[str, frozenset[str]] = field(default_factory=dict)


async def stored_skill_declarations(store: Any | None) -> StoredSkillTools:
    """Read both stored tiers' declarations, for the turn in flight.

    Without this a stored skill had no entry, and a missing entry reads as "declares nothing",
    leaving every stored skill visible regardless of the bound tools. The declarations are parsed
    from the bodies on the same paged walk a listing costs, so the body is the one source of truth;
    it runs in the async caller that builds the store (`api/runner.turn_store`), keeping
    `build_langgraph_agent` synchronous. Fails closed, and no stored skill name reaches a log line
    or metric label, since a personal skill's name is a person's own words.

    Mirrors the mounts in `scratchpad_backend`: the organisation's tier needs a store, the chemist's
    needs a store and an actor. The actor comes from the same ambient reader the mount uses
    (`get_current_actor()`, which normalizes), so both resolve the same namespace.

    A name held by both tiers keeps the organisation's declaration: `_skills_middleware` orders
    sources `/mine`, `/org`, then the reviewed trees, and upstream resolves collisions
    last-source-wins, so the org body is what the turn reads. Reading `/mine` first and `/org` last
    matches that, so one person's private document cannot decide an org skill's visibility.

    Args:
        store: The process's store, or `None` for a deployment with no durable memory — in which
        case neither tier is mounted and there is nothing to read.

    Returns:
        The two declaration maps, empty when no tier is reachable. Off the request path there is no
        actor, so only the organisation's tier is read, which is also what is mounted there.
    """
    if store is None:
        return StoredSkillTools()
    actor = get_current_actor()
    declared: dict[str, frozenset[str]] = {}
    required: dict[str, frozenset[str]] = {}
    # `/mine` first and `/org` last, so the later write wins for a name both hold, matching the body
    # a turn reads.
    namespaces = [local_skills_namespace(actor)] if actor else []
    namespaces.append(org_skills_namespace())
    for namespace in namespaces:
        for key, item in (await paged_items(store, namespace)).items():
            # Keyed by the store key's name, never the frontmatter's: the frontmatter may be
            # unreadable, and `validated_skill` checked the two agree before the body was written. A
            # missing entry must never happen, since it reads as "declares nothing".
            name = name_of_key(key)
            if name is None:
                continue
            declared[name], required[name] = _declaration(item)
    return StoredSkillTools(declared=declared, required=required)


def _declaration(item: Any) -> tuple[frozenset[str], frozenset[str]]:
    """One stored skill's two declarations, scoped to nothing when its body cannot be read.

    Reads `item.value["content"]` off the search result, so there is no extra round trip. The parsed
    name is discarded; the caller keys by the store key.
    """
    body = (item.value or {}).get("content") if isinstance(item.value, dict) else None
    if not isinstance(body, str):
        return _unreadable("its stored value carries no body")
    try:
        _declared_name, tools, requires = declared_triple(frontmatter.loads(body).metadata)
    except Exception as exc:
        # The exception's type, never its message: a parser quotes what it choked on, which could
        # put a person's own words into a shared log.
        return _unreadable(type(exc).__name__)
    return tools, requires


def _unreadable(why: str) -> tuple[frozenset[str], frozenset[str]]:
    """Scope one stored skill to nothing, and say so once.

    WARNING via `degraded`, matching `manifest._declared_pair`: both write doors validate bodies, so
    this is a document stored before a rule tightened, and scoping to nothing is safe. Unlike the
    filed path the name is not logged, since a personal skill's name is a person's words.
    """
    degraded(
        logger,
        "stored_skill_manifest",
        "a stored skill's frontmatter could not be read, scoping it to nothing: %s",
        why,
        level=logging.WARNING,
        exc_info=False,
    )
    return UNREADABLE_DECLARATION, UNREADABLE_DECLARATION


# --- the interface ---


class SkillStore:
    """The three tiers behind one interface: list, read, write, version and mount.

    Args:
        store: The process's store, holding the organisation's and the chemist's tiers.
        actor: Whose own tier `Tier.MINE` means, in the turn's actor spelling.
        repo_dirs: The directories of the reviewed tree, for `Tier.REPO` reads.

    Every write first asks `access.may_write` and raises `SkillWriteRefused` on a no; the reviewed
    tree has no writer here at all, and nothing a model holds can obtain one.
    """

    def __init__(self, store: Any, *, actor: str = "", repo_dirs: Iterable[str] = ()) -> None:
        """Hold the store, the owner of `Tier.MINE` and the reviewed tree's directories."""
        self._store = store
        self._actor = actor
        self._repo_dirs = tuple(repo_dirs)

    async def names(self, tier: Tier) -> list[str]:
        """The names `tier` holds, sorted — all of them."""
        if tier is Tier.REPO:
            return sorted(declared_tools(self._repo_dirs))
        if tier is Tier.ORG:
            return await list_org_skills(self._store)
        return await list_local_skills(self._store, self._actor)

    async def read(self, tier: Tier, name: str) -> str | None:
        """One skill's body verbatim, or `None` if `tier` holds no skill by that name."""
        if tier is Tier.REPO:
            return self._read_repo(name)
        if tier is Tier.ORG:
            return await read_org_skill(self._store, name)
        return await read_local_skill(self._store, self._actor, name)

    async def save(self, tier: Tier, name: str, body: str, *, writer: Writer) -> None:
        """Write one skill, replacing any earlier body of that name.

        The caller validates `body` first (`manifest.validated_skill`), so a refusal here is only
        about who is writing or how much the tier holds.

        Raises:
            SkillWriteRefused: `writer` may not change `tier`.
            SkillRefused: The tier is full.
        """
        self._admit(tier, writer)
        if tier is Tier.ORG:
            await save_org_skill(self._store, name, body, activated_by=writer.actor)
        else:
            await save_local_skill(self._store, self._actor, name, body)

    async def remove(self, tier: Tier, name: str, *, writer: Writer) -> bool:
        """Stop one skill acting. Returns whether there was one; the organisation keeps history."""
        self._admit(tier, writer)
        if tier is Tier.ORG:
            return await retire_org_skill(self._store, name, retired_by=writer.actor)
        return await delete_local_skill(self._store, self._actor, name)

    async def versions(self, name: str) -> list[OrgSkillVersion]:
        """Every body the organisation ever activated under `name`, newest first.

        Only the organisation's tier keeps history; a chemist's own is what its owner asserts now.
        """
        return await list_org_versions(self._store, name)

    async def revert(self, name: str, digest: str, *, writer: Writer) -> bool:
        """Make a held organisation body the active one again. Returns whether it was held."""
        self._admit(Tier.ORG, writer)
        return await activate_org_version(self._store, name, digest, activated_by=writer.actor)

    def mount(self, tier: Tier, permits: Callable[[str], bool]) -> PermittedStoreBackend:
        """The read half of a stored tier as a backend a turn mounts, narrowed by `permits`.

        The reviewed tree is mounted per bundle by the graph builder (`skills_backend`).
        """
        from chemclaw.skills.backends import local_skills_backend, org_skills_backend

        if tier is Tier.ORG:
            return org_skills_backend(self._store, permits)
        if tier is Tier.MINE:
            return local_skills_backend(self._store, self._actor, permits)
        raise ValueError("the reviewed tree is mounted per bundle by the graph builder")

    def _admit(self, tier: Tier, writer: Writer) -> None:
        """Refuse a writer the tier's rule does not admit."""
        if not may_write(tier, writer, owner=self._actor):
            raise SkillWriteRefused(
                f"the {tier.value} skills tier cannot be changed by this writer"
            )

    def _read_repo(self, name: str) -> str | None:
        """One reviewed skill's `SKILL.md`, or `None` for an absent or unsafe name."""
        if not storable_name(name) or "/" in name or name.startswith("."):
            return None
        for directory in self._repo_dirs:
            path = Path(directory) / name / SKILL_FILENAME
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return None
