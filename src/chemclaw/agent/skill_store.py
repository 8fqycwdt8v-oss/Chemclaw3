"""What both stored skills tiers share: keys, paging and the writer lock.

A chemist's own skills (`agent/local_skills.py`) and the organisation's (`agent/org_skills.py`) live
in the store under the same key shape. The backend that narrows their reads and refuses their writes
is `agent/skill_store_backend.PermittedStoreBackend`, in its own module because it subclasses a
`deepagents` backend and this one is on the front door's import path, which
`tests/test_import_budget.py` keeps free of `deepagents`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.core.config import settings

if TYPE_CHECKING:
    from deepagents.backends import StoreBackend

#: What a mounted tier answers for a path outside what this turn may reach: the same words as
#: `agent/skill_backend.REFUSED`, which do not reveal whether the skill exists.
REFUSED = "This path is not part of the skills available to you."


#: How many rows one page of a namespace walk asks for. `BaseStore.asearch` defaults to `limit=10`,
#: which would silently truncate a listing.
LISTING_PAGE = 100


async def paged_items(store: Any, namespace: tuple[str, ...]) -> dict[str, Any]:
    """Every item in one namespace, keyed by store key — the whole tier rather than one page.

    One paging walk shared by each tier's listing, the organisation tier's version listing and cap,
    and `agent/stored_skill_tools.py`. (`agent/scratchpad.py`'s eviction walk stays separate because
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


#: The document that makes a directory a skill, mirroring the reviewed tree, so
#: `/org/<name>/SKILL.md` and `/mine/<name>/SKILL.md` follow the same convention as `skills/`.
SKILL_FILENAME = "SKILL.md"

#: The transaction-scoped advisory lock a stored tier's writes serialize on — see
#: `advisory_writer_lock`.
_WRITER_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"


def storable_name(name: str) -> bool:
    r"""Whether this name could ever have been written, which is what a read of it may assume.

    The writer's rule, asked by the readers: the `GET`/`DELETE /skills/mine/{name}` routes accept
    any URL bytes, and a name like `a\x00b` would make Postgres raise (a 500) where the honest
    answer is 404.
    """
    return all(not character.isspace() and character.isprintable() for character in name)


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


def store_writer(store: Any, namespace: tuple[str, ...]) -> "StoreBackend":
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
