"""Where a turn puts intermediate work: a scratchpad, and memories that outlive the session.

Without it every tool result lives only in the context window, where compaction reclaims it. Three
routes over one `CompositeBackend`:

- **`/scratch/…` → `StateBackend`.** Per-thread files in the graph's state; never on disk, gone with
  the checkpoint.
- **`/skills/…` → `NarrowedSkillsBackend`.** Read-only; writes are refused by the backend
  (`agent/skill_backend.py`).
- **`/memories/…` → `StoreBackend`** over `AsyncPostgresStore`, when the deployment enables it *and*
  the turn has an actor. The only route that outlives the session.

**The namespace is the erasure key**
(`D-2026-08-10-basestore-is-not-where-this-systems-memory-lives`): `store` has no actor column, so
the actor digest goes in the namespace and erasure is a prefix match on `store.prefix`, like every
other table in `agent/leaver.py`. It is computed when the backend is built (the graph is compiled
per turn), not from the runtime. With no actor there is no memories route at all, rather than a
shared namespace nobody could erase.

This is a working surface, not knowledge: a conclusion worth keeping goes through
`record_knowledge_note`, and nothing under `/memories/` is evidence a citation resolves to.

The only path to the store is the `write_file`/`edit_file` tools, so every write crosses the
`wrap_tool_call` chain (audit, authorization, repeat guard); `tests/test_scratchpad.py` asserts no
module calls `aput`/`adelete` directly. Whether a write is side-effecting depends on its path
(`/memories/` is durable, `/scratch/` is not), so `authz.side_effecting_call` reads the call's
`file_path` for the dry-run and plan gates.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any, cast

from deepagents import FsToolName
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import EditResult, WriteResult
from deepagents.backends.utils import file_data_to_string, perform_string_replacement
from deepagents.middleware.filesystem import FilesystemState
from langchain.agents.middleware import before_agent
from langgraph.runtime import Runtime
from langgraph.store.base import SearchItem
from langgraph.store.postgres.aio import AsyncPostgresStore

from chemclaw.agent.local_skills import (
    LOCAL_SKILLS_ROOT,
    local_skills_backend,
)
from chemclaw.agent.org_skills import ORG_SKILLS_ROOT, org_skills_backend
from chemclaw.agent.skill_access import SkillNarrowing
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

_store: AsyncPostgresStore | None = None

# The tables `AsyncPostgresStore.setup()` may create. They are upstream's and appear in no
# migration, so the erasure sweep (`agent/leaver.py`) spells them from here;
# `tests/test_scratchpad.py` checks them against upstream's migration constants.
#
# `store_vectors` exists only for a store built with an `index_config` (none is passed here); it
# stays listed so erasure reaches it wherever a site sets one.
#
# The retention sweep deliberately touches neither table: a memory is written to persist, so
# disposing of one is not an age cutoff's decision.
STORE_TABLES: tuple[str, ...] = ("store", "store_vectors")

# The memories route's root, shared by the route key, the write-permission rules and the erasure
# prefix so they cannot disagree.
MEMORY_ROOT = "/memories/"

# The scratchpad root. Unrouted, so it resolves to the composite's default `StateBackend`; named
# only so the permission rules and the system prompt can agree on where a turn may write.
SCRATCH_ROOT = "/scratch/"


def memory_namespace(actor: str) -> tuple[str, ...]:
    """The store namespace one person's memories live under.

    Digested because namespace components reject most punctuation and an actor may be spelled either
    as an Entra `oid` or `unverified:<id>`; a digest is always legal and keeps the spellings
    distinct, so erasure can hash and remove both.

    Args:
        actor: The turn's actor id, in whichever spelling the caller holds.

    Returns:
        The namespace tuple, stable for one actor across processes and restarts.
    """
    return ("memories", stable_hash(actor))


def memory_prefix(actor: str) -> str:
    """The `store.prefix` value that names one person's memories, for the erasure sweep.

    Exposed so `agent/leaver.py` uses the same dotted join this module writes under rather than
    re-deriving it.

    Args:
        actor: The departing person's id.

    Returns:
        The dotted prefix under which every memory of theirs is stored.
    """
    return ".".join(memory_namespace(actor))


async def memory_store() -> AsyncPostgresStore:
    """The process's memory store, created and migrated on first use.

    Shares the checkpointer's autocommit pool, whose reasons (see `agent/checkpointer.py`) apply
    equally to this store's `setup()`.

    The store is published only after `setup()` completes, under the checkpointer's `_init_lock`, so
    a concurrent turn never sees a store whose tables do not exist yet. The pool is awaited outside
    the lock because `_checkpoint_pool` takes the same non-reentrant lock; sharing one lock lets
    `close_checkpointer` drop both together.

    Returns:
        A ready store over this process's session pool.
    """
    global _store
    if _store is not None:
        return _store
    # Imported here rather than at module scope: `checkpointer` imports `state`, which imports
    # config, and a module-scope import would put this module in that cycle for one call.
    from chemclaw.agent.checkpointer import _checkpoint_pool, _initialization_lock, _setup_once
    from chemclaw.agent.session_store import _session_dsn

    # Awaited outside the lock, and it must stay outside: `_checkpoint_pool` takes the same lock,
    # and `asyncio.Lock` is not reentrant. Holding it across that call deadlocks every cold start.
    pool = await _checkpoint_pool()
    async with _initialization_lock():
        if _store is None:
            store = AsyncPostgresStore(pool)
            # Under the checkpointer's cross-pod lock: two replicas' first turns on a fresh
            # database collide on `store_migrations_pkey` otherwise.
            await _setup_once(store, _session_dsn())
            _store = store
            # No table count: `STORE_TABLES` names both tables the store may create, and this build
            # creates only `store`.
            logger.info("memory store ready")
    return _store


async def close_memory_store() -> None:
    """Drop the process's store — called by `close_checkpointer`, which owns the pool beneath it.

    The store is dropped before its pool is closed, so nothing is handed a store over closed
    connections. The pool itself is left to `close_checkpointer`.
    """
    global _store
    _store = None


class BoundedStoreBackend(StoreBackend):
    """`StoreBackend` with a row cap per namespace.

    `store` is agent-writable and no retention sweep touches it, so without a cap memories
    accumulate across turns for the life of the deployment; this is the per-actor twin of
    `ingest/rejections.py`'s `_MAX_ROWS_PER_SOURCE`.

    The cap lives in the backend, not in a `BaseStore` wrapper, so every write still arrives as a
    tool call through the `wrap_tool_call` chain. This class is the one module allowed to delete
    from the store directly, and only to evict in the same call.

    The bound is eventual, not atomic: the store shares an autocommit pool, so the write and the
    eviction are two statements, and concurrent writers may briefly overshoot. The next write
    converges.
    """

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Write, then evict whatever the cap no longer has room for.

        Evicting after rather than before, because overwriting an existing key adds no row and must
        not cost a memory.

        Args:
            file_path: The memory's path under `/memories/`.
            content: What to store.

        Returns:
            Upstream's result, unchanged, or a refusal when `content` is past
            `agent_scratch_file_max_chars` (checked first, so an oversized memory never lands).
        """
        refusal = oversized_file(file_path, content)
        if refusal is not None:
            return WriteResult(error=refusal)
        result = await super().awrite(file_path, content)
        await self._evict_past_the_cap()
        return result

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit, unless this edit has already been applied and applying it again would duplicate.

        A tool killed mid-call is re-run on resume with its original arguments. `/scratch/` is safe
        because its backend is the checkpoint; a `/memories/` edit is a read-modify-write against a
        store outside the checkpoint, so a replay would apply it twice. The breaking shape is an
        insert that keeps its anchor: when `new_string` contains `old_string` and is already
        present, the edit is refused. A plain substitution needs no guard, since its replay fails
        with upstream's "String not found".

        The cost is that deliberately inserting an identical block twice is refused, with a reason.

        Args:
            file_path: The memory's path under `/memories/`.
            old_string: The anchor to replace.
            new_string: What to put in its place.
            replace_all: Replace every occurrence rather than requiring exactly one.

        Returns:
            Upstream's result, a refusal naming the repeat, or a refusal when the edited memory
            would be past `agent_scratch_file_max_chars`.
        """
        edited = _edited_content(
            await self._current_content(file_path), old_string, new_string, replace_all
        )
        if edited is not None:
            refusal = oversized_file(file_path, edited)
            if refusal is not None:
                return EditResult(error=refusal)
        if old_string and old_string in new_string:
            # The raw store value, not `aread`, which paginates and would let the guard fail open on
            # long memories.
            content = await self._current_content(file_path)
            if content is not None and new_string in content:
                return EditResult(
                    error=(
                        f"Error: this edit is already applied to {file_path}. Its replacement "
                        "contains its own anchor, so applying it again would insert a second copy "
                        "rather than change anything — and a memory write is not replayable. If "
                        "you meant to add something further, edit with different text."
                    )
                )
        return await super().aedit(file_path, old_string, new_string, replace_all)

    async def _current_content(self, file_path: str) -> str | None:
        """This memory's whole text, or `None` when there is none — the way `aedit` itself reads it.

        Uses the same calls as `StoreBackend.aedit` so the guard sees exactly the content the
        replacement applies to. A malformed value answers `None`, leaving the error to upstream.
        """
        from deepagents.backends.utils import file_data_to_string

        item = await self._get_store().aget(self._get_namespace(), file_path)
        if item is None:
            return None
        try:
            return str(file_data_to_string(self._convert_store_item_to_file_data(item)))
        except ValueError:
            return None

    async def _evict_past_the_cap(self) -> None:
        """Drop the least recently updated memories until this namespace is inside the cap.

        The bound is a count; `updated_at` only decides which rows go. The whole namespace is paged
        and sorted here by `updated_at` rather than trusting the store's search order, which
        `BaseStore` does not promise (Postgres and the in-memory store differ), so the oldest
        surplus is removed in one write. `_EVICTION_PAGE` sizes each page of the walk, never the
        deletion.
        """
        cap = settings.agent_memory_max_files
        store = self._get_store()
        namespace = self._get_namespace()
        held: dict[str, SearchItem] = {}
        while True:
            page = await store.asearch(namespace, limit=_EVICTION_PAGE, offset=len(held))
            # A page that adds nothing is the end of the namespace — or a store that ignores
            # `offset`, which would otherwise walk the first page for ever.
            fresh = {item.key: item for item in page if item.key not in held}
            if not fresh:
                break
            held.update(fresh)
        if len(held) <= cap:
            return
        doomed = sorted(held.values(), key=lambda item: item.updated_at)[: len(held) - cap]
        for item in doomed:
            await store.adelete(namespace, item.key)
        METRICS.increment("chemclaw_memory_evictions_total", len(doomed))
        # Logged the way `ingest/rejections.py` logs its own eviction: an operator who set the cap
        # needs to know it is binding, and a chemist whose memory vanished has no other trace.
        logger.warning(
            "evicted %d memory file(s) past the %d-file cap: %s",
            len(doomed),
            cap,
            ", ".join(item.key for item in doomed),
        )


#: How many rows one page of the surplus walk reads. A page size, not a deployment posture, so not a
#: `Settings` field; it bounds one query, never the deletion.
_EVICTION_PAGE = 64


def oversized_file(file_path: str, content: str) -> str | None:
    """The refusal for a file a turn is about to store past `agent_scratch_file_max_chars`, if any.

    A refusal, never a cut: this is the caller's own document, and the model can act on the message
    by splitting the file or writing less. One function for both `/scratch/` and `/memories/` so the
    number and wording agree.

    Args:
        file_path: The path the turn named, for the message.
        content: The whole text the file would hold after this write or edit.

    Returns:
        The error text to return in place of the write, or `None` when it fits.
    """
    limit = settings.agent_scratch_file_max_chars
    if len(content) <= limit:
        return None
    return (
        f"Error: {file_path} was not written. It would hold {len(content):,} characters and one "
        f"file may hold at most {limit:,} (agent_scratch_file_max_chars). Nothing was truncated "
        "and nothing was stored: split it across several files, or write less."
    )


def _edited_content(
    current: str | None, old_string: str, new_string: str, replace_all: bool
) -> str | None:
    """The text an edit would leave, computed the way upstream's own `edit` computes it.

    `None` when there is no file or the replacement would fail, leaving upstream's error as the
    answer.
    """
    if current is None:
        return None
    result = perform_string_replacement(current, old_string, new_string, replace_all)
    return result[0] if isinstance(result, tuple) else None


class BoundedStateBackend(StateBackend):
    """`StateBackend` whose `write`/`edit` refuse a file past `agent_scratch_file_max_chars`.

    Bounded here rather than in a middleware: this backend writes the `files` channel directly, so
    no `wrap_tool_call` middleware sees the content. Only the sync verbs are overridden; upstream's
    `awrite`/`aedit` wrap them.
    """

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write, unless the file would be past the cap — then refuse and store nothing."""
        refusal = oversized_file(file_path, content)
        if refusal is not None:
            return WriteResult(error=refusal)
        return super().write(file_path, content)

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit, unless the edited file would be past the cap — then refuse and change nothing.

        The result's size is checked, so repeated appending edits cannot walk a file past the cap.
        """
        stored = self._read_files().get(file_path)
        current = file_data_to_string(stored) if stored is not None else None
        edited = _edited_content(current, old_string, new_string, replace_all)
        if edited is not None:
            refusal = oversized_file(file_path, edited)
            if refusal is not None:
                return EditResult(error=refusal)
        return super().edit(file_path, old_string, new_string, replace_all)


def _stale_files(files: Mapping[str, Any], cutoff: datetime) -> list[str]:
    """The paths in a `files` channel whose last write is older than `cutoff`.

    Dated by upstream's `modified_at`, stamped on every write and edit. A file with no parseable
    `modified_at` is kept rather than deleted at an unknown age.
    """
    stale = []
    for path, data in files.items():
        stamp = data.get("modified_at") if isinstance(data, Mapping) else None
        try:
            written = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
        except ValueError:
            written = None
        if written is None:
            continue
        if written.tzinfo is None:
            written = written.replace(tzinfo=UTC)
        if written < cutoff:
            stale.append(path)
    return stale


@before_agent(state_schema=FilesystemState)
def expire_stale_scratch(state: FilesystemState, runtime: Runtime[Any]) -> dict[str, Any] | None:
    """Drop every file this thread has not written for `agent_scratch_retention_days`.

    Runs at the start of a turn and deletes through the channel's own reducer (`{path: None}`),
    since `files` is a checkpointed channel with no row to delete; superseded checkpoints are pruned
    by `checkpoint_retain_per_thread`. A thread nobody returns to keeps its files until
    `retention_checkpoints_days` disposes of the thread. `0` keeps every file.
    """
    del runtime  # the hook's signature; nothing here depends on the run
    days = settings.agent_scratch_retention_days
    files = state.get("files") or {}
    if days <= 0 or not files:
        return None
    stale = _stale_files(files, datetime.now(UTC) - timedelta(days=days))
    if not stale:
        return None
    logger.info(
        "removed %d file(s) not written for %d day(s) (agent_scratch_retention_days): %s",
        len(stale),
        days,
        ", ".join(sorted(stale)),
    )
    return {"files": dict.fromkeys(stale)}


def scratchpad_backend(
    skills: CompositeBackend,
    store: Any | None = None,
    *,
    permits: SkillNarrowing,
) -> CompositeBackend:
    """Extend a turn's skills backend with a scratchpad and, when enabled, durable memories.

    Takes the skills backend rather than rebuilding it, so the skills middleware and this backend
    share one narrowing. The store is passed in (created by the async caller) so this function, and
    `build_langgraph_agent`, stay synchronous.

    Args:
        skills: The narrowed skills backend for this profile (`langgraph_agent.skills_backend`).
        store: This process's `AsyncPostgresStore` from `memory_store()`, or `None` for a turn with
        no durable memory.
        permits: The narrowing this turn computed (`langgraph_agent.skill_narrowing`); its `stored`
        half gates the two stored tiers. Required, so the personal tier can never be mounted
        unnarrowed by omission.

    Returns:
        A backend routing `/skills/…` as given, `/org/…` to the store whenever there is one,
        `/memories/…` and `/mine/…` to the store when there is also an actor, and everything else
        (`/scratch/…` included) to graph state through `BoundedStateBackend`.
    """
    routes = dict(skills.routes)
    actor = get_current_actor()
    if store is not None and actor:
        namespace = memory_namespace(actor)
        # A closure over the value, not a read through the runtime: see the module docstring. The
        # lambda takes the runtime upstream passes and ignores it, which is the whole point.
        routes[MEMORY_ROOT] = BoundedStoreBackend(namespace=lambda _runtime: namespace, store=store)
        # The chemist's own skills need a store and an actor too; a separate first namespace
        # component keeps the tiers separately erasable (see `agent/local_skills.py`).
        routes[LOCAL_SKILLS_ROOT] = local_skills_backend(store, actor, permits.stored)
    # The organisation's tier needs a store but no actor: it is one shared namespace every turn
    # resolves, with no per-actor prefix to erase (see `agent/org_skills.py`).
    if store is not None:
        routes[ORG_SKILLS_ROOT] = org_skills_backend(store, permits.stored)
    return CompositeBackend(default=BoundedStateBackend(), routes=routes)


@cache
def scratchpad_tools() -> tuple[FsToolName, ...]:
    """The filesystem verbs this deployment lets a turn reach, in one place.

    Read off the middleware so an upstream rename changes the value rather than staling an
    allow-list. Two are withheld:

    - **`execute`** would be a shell; no sandbox here is acceptable and a local shell is
      unrestricted.
    - **`delete`**, because a turn that can remove a `SKILL.md` decides what judgment the next turn
      can load.

    Cached: it builds a `FilesystemMiddleware` to answer a question about the installed package, and
    runs on a per-tool-call path.

    Returns:
        The tool names to hand `FilesystemMiddleware`, sorted so the prompt order is stable.
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware

    withheld = {"execute", "delete"}
    every = {tool.name for tool in FilesystemMiddleware(backend=StateBackend()).tools}
    # `cast` over upstream-derived names: `FsToolName` is upstream's alias for exactly this set.
    return cast("tuple[FsToolName, ...]", tuple(sorted(every - withheld)))


def filesystem_permissions() -> list[Any]:
    """Deny-rules bounding where a turn may write, evaluated before any filesystem operation.

    `FilesystemPermission` is first-match-wins, so the allows under the writable roots come first
    and a blanket write-deny closes the rest.

    The rules must also be handed to the `FilesystemMiddleware` instance
    `langgraph_agent._middleware` substitutes (as `_permissions=`);
    `create_deep_agent(permissions=…)` reaches only its own instance. Riding the middleware list
    also carries them into helpers.

    `/skills/` is refused here and again by `NarrowedSkillsBackend`, deliberately, so the property
    does not rest on upstream's defaults alone.

    Returns:
        The rules to pass `create_deep_agent(permissions=…)` **and** the `FilesystemMiddleware` that
        replaces upstream's.
    """
    from deepagents import FilesystemPermission

    return [
        FilesystemPermission(operations=["write"], paths=[f"{SCRATCH_ROOT}**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=[f"{MEMORY_ROOT}**"], mode="allow"),
        # `/**` rather than `**`: upstream validates that a rule's path is absolute and rejects a
        # bare glob outright, which is a better default than silently matching nothing.
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]
