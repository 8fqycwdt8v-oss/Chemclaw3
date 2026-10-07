"""The read half of a stored skills tier: narrowed on every reach path, refused on every write.

One backend behind both stored tiers — a chemist's own skills (`agent/local_skills.py`) and the
organisation's (`agent/org_skills.py`). It is the `StoreBackend` twin of
`agent/skill_backend.NarrowedSkillsBackend`, kept as a separate class because the two base classes
differ in which verbs are native.

The `permits` predicate applies to every reach path, not only the listing: a model told about one
path can ask for another, so a narrowing applied only in the prompt would still serve every body
(and would falsify the no-skills eval arm). Nothing reaches another person's tier regardless: the
namespace closes over the actor and mounts are per turn.

`ls`, `read`, `grep`, `glob` and `download_files` are gated here. The async
`als`/`aglob`/`agrep`/`adownload_files` are `to_thread` wrappers and dispatch through these, but
`aread` (and the async write verbs) are native on `StoreBackend` and are overridden explicitly; the
coverage test derives its method list from both the protocol and `StoreBackend`.
"""

import dataclasses
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar, cast

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import GlobResult, GrepResult, LsResult

from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.agent.skill_backend import (
    SkillsReadOnlyRefusal,
    is_a_skill_body,
    path_of,
    skill_of,
)
from chemclaw.core.config import settings

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


def store_writer(store: Any, namespace: tuple[str, ...]) -> StoreBackend:
    """A writable backend over one stored tier's namespace, for the routes that change it.

    Plain `StoreBackend`: this is reached from an HTTP route a person calls, not from anything a
    model holds. Using upstream's writer rather than `store.aput` keeps the stored shape singly
    defined, and `tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly`
    holds that no module bypasses a backend.
    """
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


class PermittedStoreBackend(StoreBackend):
    """A stored skills tier: every read bounded by `permits`, every write refused.

    Args:
        namespace: The store namespace factory this tier lives under, per upstream's signature.
        store: The store, passed explicitly because these backends are built outside a graph run.
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
        permits: Callable[[str], bool],
        refusal: str,
        on_load: Callable[[str], None],
    ) -> None:
        """Hold the namespace, the predicate, the refusal text and the counting hook."""
        super().__init__(namespace=namespace, store=store)
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
            return _refused_read()
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
            return _refused_read()
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
        raise SkillsReadOnlyRefusal(self._refusal)

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise SkillsReadOnlyRefusal(self._refusal)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives.

        A turn that could delete would be one bad inference away from removing judgment somebody
        relies on.
        """
        raise SkillsReadOnlyRefusal(self._refusal)

    def upload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise SkillsReadOnlyRefusal(self._refusal)

    async def awrite(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse.

        Overridden because `StoreBackend.awrite` is native rather than a thread wrapper, and it is
        the path an async agent takes.
        """
        raise SkillsReadOnlyRefusal(self._refusal)

    async def aedit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `awrite` gives."""
        raise SkillsReadOnlyRefusal(self._refusal)

    async def adelete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `awrite` gives."""
        raise SkillsReadOnlyRefusal(self._refusal)

    async def aupload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse.

        Stated explicitly rather than relying on the base class falling through to `upload_files`,
        since which async verbs are wrappers is not something to depend on.
        """
        raise SkillsReadOnlyRefusal(self._refusal)


def _refused_read() -> Any:
    """What a gated read answers — a result the model can read, never a raised error.

    Built through upstream's `ReadResult`, imported at call time so an upstream move is a red test
    rather than an import error at startup.
    """
    from deepagents.backends.protocol import ReadResult

    return ReadResult(error=REFUSED, file_data=None)


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
