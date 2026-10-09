"""The LangGraph turn-state checkpointer, on its own Postgres pool.

Temporal keeps every long or expensive job; this keeps turn state, rollback, resume and the
suspended turns `interrupt()` produces.

**Its own pool, opened with `autocommit=True`:** `setup()` runs `CREATE INDEX CONCURRENTLY`,
which Postgres refuses inside a transaction; the saver serializes every statement on one lock
and holds a connection across paginated reads; and it puts its connection in pipeline mode. A
pipeline block is still one transaction, so each `aput`, `aput_writes` and `adelete_thread` is
atomic (pinned in `tests/test_upstream_surface.py`), but each commits immediately on a
connection no sweep is inside.

**One saver per process, pinned to its loop**, hence the async factory.

**Schema stamp.** LangGraph restores a checkpoint into channels built from the current state
schema with no migration, so a channel added since the checkpoint was written is empty, and a
node indexing it raises a bare `KeyError` mid-turn. `SchemaStampedSaver` stamps each checkpoint
with the restorable first-party channels (`FIRST_PARTY_CHANNELS`, excluding langchain's base
channels and untracked ones) and, on resume, raises `CheckpointSchemaMismatch` only for missing
channels some module actually indexes (`channels_read_without_default`, which fails closed).
Removed channels are harmless; same-name type changes are not caught.

**Value stamp.** `CHECKPOINT_VALUES_KEY` records which channels held a value; a checkpoint whose
blobs are gone (a racing sweep, a partial restore) raises `CheckpointValuesMissing` instead of
resuming as an empty conversation. History reads (`alist`) are not guarded.

Refusing beats silently restarting: a confidently out-of-context answer is worse than none, and
nothing is destroyed. Unstamped checkpoints, or stamps this build cannot read, resume normally
so a deploy (rolling, in either direction) never bricks live sessions.
"""

import ast
import asyncio
import functools
import logging
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast, get_args, get_origin, get_type_hints

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import _ainternal
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row, tuple_row
from psycopg_pool import AsyncConnectionPool

from chemclaw.agent.session_store import _session_dsn
from chemclaw.agent.state import ChemclawState
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.db import register_pool, unregister_pool
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.turn_fence import TurnFenceLost, current_turn_fence

logger = logging.getLogger(__name__)

_saver: AsyncPostgresSaver | None = None
_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None

# Guards the lazy initializations below (and `scratchpad.memory_store()`, which shares the pool),
# each a check-then-await-then-act; globals are published only after the await completes, so a
# concurrent turn never sees an unmigrated saver or unopened pool. Created lazily because
# `asyncio.Lock` binds to the running loop; `close_checkpointer` drops it.
_init_lock: asyncio.Lock | None = None

# Checkpointer statements currently queued on a saver's lock, process-wide. A plain int is safe:
# all mutation is on one event loop with no `await` between read and write.
_statements_waiting = 0


def checkpointer_statements_waiting() -> float:
    """The gauge source for `chemclaw_checkpointer_statements_waiting`.

    See `SchemaStampedSaver._cursor`.
    """
    return float(_statements_waiting)


def _strict_serde() -> JsonPlusSerializer:
    """The checkpoint serializer, pinned to reject import-by-name deserialization.

    The default `JsonPlusSerializer` lets a stored blob name any importable callable, so a poisoned
    `checkpoint_blobs` row (writable by the app role) would execute code on resume.
    `allowed_msgpack_modules=None` restricts it to `SAFE_MSGPACK_TYPES`; legitimate channels use the
    typed/JSON path and still round-trip. `tests/test_upstream_surface.py` pins upstream's
    permissive default.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=None)


def _initialization_lock() -> asyncio.Lock:
    """The current loop's initialization lock, created on first use.

    No `await` sits between the check and the assignment, so this cannot race.
    """
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


# The tables `AsyncPostgresSaver.setup()` creates, for the erasure sweep and its completeness test.
# `checkpoint_migrations` holds schema versions, not conversations, so it is excluded.
CHECKPOINT_TABLES: tuple[str, ...] = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


# One turn's prune of the copies the newest checkpoint has superseded.
#
# Every superstep rewrites the whole `messages` channel, so stored blobs grow with the square of a
# thread's turns. This keeps the newest checkpoints per `checkpoint_ns` and deletes older rows
# below a per-namespace version floor. Channel versions are monotone, so rows written after this
# statement's snapshot sort above every floor and cannot be deleted; one statement on an
# autocommit pool is one transaction. Partitioning by namespace means a subgraph namespace never
# loses rows to the root's floor. A blob whose channel appears in no kept checkpoint has no floor
# and is kept: an unreferenced row costs bytes, a wrongly deleted one costs the conversation.
_PRUNE_SUPERSEDED = """
WITH ranked AS (
    SELECT checkpoint_ns, checkpoint_id, checkpoint,
           row_number() OVER (PARTITION BY checkpoint_ns ORDER BY checkpoint_id DESC) AS rn
      FROM checkpoints WHERE thread_id = %(thread)s
),
kept AS (SELECT checkpoint_ns, checkpoint_id, checkpoint FROM ranked WHERE rn <= %(keep)s),
floors AS (
    SELECT kept.checkpoint_ns AS ns, versions.key AS channel, min(versions.value) AS floor_version
      FROM kept, jsonb_each_text(kept.checkpoint -> 'channel_versions') AS versions
     GROUP BY 1, 2
),
oldest_kept AS (
    SELECT checkpoint_ns AS ns, min(checkpoint_id) AS floor_id FROM kept GROUP BY 1
),
pruned_checkpoints AS (
    DELETE FROM checkpoints c USING oldest_kept
     WHERE c.thread_id = %(thread)s AND c.checkpoint_ns = oldest_kept.ns
       AND c.checkpoint_id < oldest_kept.floor_id
    RETURNING 1
),
pruned_writes AS (
    DELETE FROM checkpoint_writes w USING oldest_kept
     WHERE w.thread_id = %(thread)s AND w.checkpoint_ns = oldest_kept.ns
       AND w.checkpoint_id < oldest_kept.floor_id
    RETURNING 1
),
pruned_blobs AS (
    DELETE FROM checkpoint_blobs b
     WHERE b.thread_id = %(thread)s
       AND EXISTS (SELECT 1 FROM floors f
                    WHERE f.ns = b.checkpoint_ns AND f.channel = b.channel
                      AND b.version < f.floor_version)
    RETURNING 1
)
SELECT (SELECT count(*) FROM pruned_checkpoints),
       (SELECT count(*) FROM pruned_writes),
       (SELECT count(*) FROM pruned_blobs)
"""


#: Raw size of the `messages` blob of the thread's newest root checkpoint. `octet_length` on a
#: `bytea` reads the TOAST header, so this is an index probe. The newest checkpoint is chosen first,
#: so a missing blob reads 0 rather than an older copy's size. Held by `tests/test_thread_size.py`.
_THREAD_BYTES = """
SELECT octet_length(b.blob)
  FROM (SELECT checkpoint FROM checkpoints
         WHERE thread_id = %(thread)s AND checkpoint_ns = ''
         ORDER BY checkpoint_id DESC
         LIMIT 1) AS newest
  LEFT JOIN checkpoint_blobs b
    ON b.thread_id = %(thread)s AND b.checkpoint_ns = ''
   AND b.channel = 'messages' AND b.version = newest.checkpoint -> 'channel_versions' ->> 'messages'
"""


async def stored_thread_bytes(thread_id: str) -> int:
    """How many bytes of conversation a turn on `thread_id` would load, or 0 if it loads none.

    Every turn loads the whole thread (compaction trims only what is sent), so
    `api/budget.check_thread_size` holds this against `session_max_thread_bytes`. Read on the pool
    directly so the admission check does not queue behind the saver's lock. 0 without durable turn
    state or before the first checkpoint.
    """
    if settings.session_store != "postgres":
        return 0
    pool = await _checkpoint_pool()
    async with pool.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        await cur.execute(_THREAD_BYTES, {"thread": thread_id})
        row = await cur.fetchone()
    return int(row[0] or 0) if row else 0


def checkpoint_thread_delete_statements(match: str) -> tuple[tuple[str, str], ...]:
    """The three per-thread DELETEs, in an order a concurrent turn cannot tear.

    The pool is autocommit, so a live turn can commit between a deleter's statements. The dependent
    blob and write deletes therefore re-check, inside the deleter's transaction, that the thread has
    no `checkpoints` row left. Shared by the erasure sweep and the single-session delete;
    `tests/test_checkpoint_delete_order.py` interleaves a real commit to prove the order.

    Args:
        match: The caller's own SQL predicate on `thread_id` (e.g. `"thread_id = %(session_id)s"`).
            It is interpolated, so it must never come from a request; ids go in bound parameters.

    Returns:
        `(table, statement)` pairs in delete order, one per `CHECKPOINT_TABLES` entry.
    """
    statements: list[tuple[str, str]] = []
    for table in CHECKPOINT_TABLES:
        if table == "checkpoints":
            statements.append((table, f"DELETE FROM {table} WHERE {match}"))
            continue
        statements.append(
            (
                table,
                f"DELETE FROM {table} WHERE {match} AND NOT EXISTS ("
                f"SELECT 1 FROM checkpoints c WHERE c.thread_id = {table}.thread_id)",
            )
        )
    return tuple(statements)


# Metadata key for the channel stamp. Metadata is jsonb the saver round-trips, so no migration is
# needed and the stamp travels with the checkpoint. Distinct from the older schema-hash key so a
# rolling deploy reads the other build's stamps as absent rather than mismatched.
STATE_CHANNELS_KEY = "chemclaw_state_channels"

# Metadata key for the value stamp: channels that held a value when this checkpoint was written.
#
# Taken before `aput` splits values between the row and `checkpoint_blobs`, since afterwards
# nothing can say which blobs should exist; `channel_versions` cannot answer it (consumed channels
# like `__start__` have versions but no blob). Absent stamps are treated as unstamped.
CHECKPOINT_VALUES_KEY = "chemclaw_checkpoint_values"


def _first_party_channels(state: Any) -> tuple[str, ...]:
    """The channel names `state` declares itself, with those of the base it extends left out.

    Derived rather than declared so it cannot go stale. Names only. A `TypedDict` merges its bases'
    annotations, so the base's channels are subtracted via `__orig_bases__` (populated because
    `PlanningState` is generic; `tests/test_checkpointer_schema.py` would catch a no-op). Untracked
    channels are excluded: they are never written to a checkpoint, so their absence on restore means
    nothing, and stamping them would refuse every live session whenever a per-turn counter is added.

    Args:
        state: The graph state class to read — `ChemclawState` in this process, and stand-in
            classes in tests.

    Returns:
        The restorable names this class adds to its base, sorted.
    """
    own = _own_channels(state)
    return tuple(sorted(name for name, ann in own.items() if not _is_untracked(ann)))


def _untracked_channels(state: Any) -> tuple[str, ...]:
    """The first-party channels the stamp deliberately leaves out, derived the same way.

    The complement of `_first_party_channels`, named so the partition test can tell an argued
    exclusion from an accidental drop.

    Args:
        state: The graph state class to read.

    Returns:
        The names this class adds to its base that no checkpoint can hold, sorted.
    """
    own = _own_channels(state)
    return tuple(sorted(name for name, ann in own.items() if _is_untracked(ann)))


#: Where `channels_read_without_default` looks: this package, the only code that can name a
#: first-party channel. Module-level so tests can point it at a fixture tree.
SOURCE_ROOT = Path(__file__).resolve().parents[1]

# The method calls on a state mapping that cannot raise `KeyError` for an absent key, whatever
# arguments follow the name. `pop` is not here: with one argument it raises as indexing does.
_DEFAULTED_READS = frozenset({"get", "setdefault"})


def channels_read_without_default(names: Iterable[str], root: Path | None = None) -> frozenset[str]:
    """Which of `names` some module under `root` reads in a way that raises when it is absent.

    Every string constant equal to one of `names` is classified by its context (`_is_a_safe_use`).
    Fails closed: an unrecognised use counts as an index, and an unreadable module or a tree with no
    sources makes every name count, because over-refusing is a named refusal while under-refusing
    is a mid-turn `KeyError`. Names spelled at run time are not seen.

    Args:
        names: The channel names to classify — in practice those a stored stamp is missing, so this
            runs only on a deploy transition.
        root: The source tree to read; `SOURCE_ROOT` (this package) when omitted.

    Returns:
        The subset of `names` some reader indexes, or could, as far as this can tell.
    """
    return _indexed(frozenset(names), root if root is not None else SOURCE_ROOT)


@functools.cache
def _indexed(wanted: frozenset[str], root: Path) -> frozenset[str]:
    """`channels_read_without_default`'s body, cached per (names, tree).

    Source does not change at run time.
    """
    if not wanted:
        return frozenset()
    modules = sorted(root.rglob("*.py"))
    if not modules:
        logger.warning(
            "no Python source under %s to derive how state channels are read; treating %s as "
            "indexed, so an older session missing one is refused rather than resumed",
            root,
            ", ".join(sorted(wanted)),
        )
        return wanted
    indexed: set[str] = set()
    for module in modules:
        try:
            text = module.read_text(encoding="utf-8")
            # A substring pre-filter: parsing only the modules that spell a name at all is what
            # keeps this a few files rather than the whole package.
            if not any(name in text for name in wanted):
                continue
            tree = ast.parse(text, filename=str(module))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            logger.warning(
                "could not read %s to derive how state channels are read; treating %s as indexed",
                module,
                ", ".join(sorted(wanted)),
            )
            return wanted
        # `state["x"] += 1` has a Store-context subscript but reads first; only the parent
        # `AugAssign` says so, so these targets are collected to never count as writes.
        augmented = {id(node.target) for node in ast.walk(tree) if isinstance(node, ast.AugAssign)}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                if (
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and child.value in wanted
                    and not _is_a_safe_use(parent, child, augmented)
                ):
                    indexed.add(child.value)
    return frozenset(indexed)


def _is_a_safe_use(parent: ast.AST, name: ast.Constant, augmented: set[int]) -> bool:
    """Whether this occurrence of a channel name provably cannot raise for an absent channel.

    A closed list; anything else counts as an index:

    - `state.get("x", …)` / `state.setdefault("x", …)`: a defaulted read.
    - `state["x"] = …` / `del state["x"]`: a write — unless it is an augmented assignment target
      (in `augmented`), which reads first.
    - `{"x": …}`: a key in a literal, i.e. an update a node returns.
    - `"x" in state` / `"x" not in state`: a membership test.
    - a bare expression statement: a docstring or no-op.
    """
    if isinstance(parent, ast.Subscript):
        return (
            parent.slice is name
            and isinstance(parent.ctx, (ast.Store, ast.Del))
            and id(parent) not in augmented
        )
    if isinstance(parent, ast.Call):
        return (
            isinstance(parent.func, ast.Attribute)
            and parent.func.attr in _DEFAULTED_READS
            and bool(parent.args)
            and parent.args[0] is name
        )
    if isinstance(parent, ast.Dict):
        return any(key is name for key in parent.keys)
    if isinstance(parent, ast.Compare):
        return parent.left is name and all(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops)
    return isinstance(parent, ast.Expr)


def _own_channels(state: Any) -> dict[str, Any]:
    """The channels `state` adds to its base, name onto annotation.

    One walk shared by `_first_party_channels` and `_untracked_channels`.

    Args:
        state: The graph state class to read.

    Returns:
        Each name this class declares beyond its base, mapped to its type hint with extras kept.
    """
    inherited: set[str] = set()
    for base in getattr(state, "__orig_bases__", ()):
        # `__orig_bases__` holds the written base, so a generic one arrives subscripted
        # (`AgentState[ResponseT]`); `get_type_hints` needs the class under it.
        origin = get_origin(base) or base
        # `__required_keys__` is what makes a class a `TypedDict` rather than `Generic` or `dict`,
        # both of which also appear in these lists and neither of which declares channels.
        if isinstance(origin, type) and hasattr(origin, "__required_keys__"):
            inherited |= set(get_type_hints(origin, include_extras=True))
    declared = get_type_hints(state, include_extras=True)
    return {name: ann for name, ann in declared.items() if name not in inherited}


def _is_untracked(annotation: Any) -> bool:
    """Whether this channel's annotation binds an `UntrackedValue`, so no checkpoint holds it.

    Read off the annotation, unwrapping `NotRequired[Annotated[...]]`, so a new untracked shape is
    covered automatically. Both an instance (`TurnTotal(int)`) and a bare class
    (`Annotated[int, UntrackedValue]`, which LangGraph instantiates) count. Uses a `type` check
    before `issubclass`, which would raise on an instance.

    Args:
        annotation: The channel's type hint, as `get_type_hints(..., include_extras=True)` gives it.

    Returns:
        `True` when the channel cannot appear in a checkpoint's `channel_values`.
    """
    return any(
        isinstance(bound, UntrackedValue)
        or (isinstance(bound, type) and issubclass(bound, UntrackedValue))
        for bound in _channel_bindings(annotation)
    )


def _channel_bindings(annotation: Any) -> tuple[Any, ...]:
    """The `Annotated` metadata of a channel annotation, unwrapped from `NotRequired` and kin.

    Args:
        annotation: The channel's type hint, as `get_type_hints(..., include_extras=True)` gives it.

    Returns:
        What the annotation binds — a channel instance or class, typically — or `()` for none.
    """
    inner = annotation
    while (origin := get_origin(inner)) is not None and origin is not Annotated:
        args = get_args(inner)
        if not args:
            break
        inner = args[0]
    return tuple(getattr(inner, "__metadata__", ()))


FIRST_PARTY_CHANNELS = _first_party_channels(ChemclawState)
UNTRACKED_CHANNELS = _untracked_channels(ChemclawState)


class CheckpointValuesMissing(RuntimeError):
    """A thread's newest checkpoint has lost channel values it was written holding.

    Its own type so callers can tell a half-deleted thread from a schema change or an outage.
    """


class CheckpointSchemaMismatch(RuntimeError):
    """A thread's turn state never held a state channel this build declares.

    Raised instead of the `KeyError` a node would produce, so callers can tell "session predates a
    state change" from an outage.
    """


@asynccontextmanager
async def _translating(operation: str, config: RunnableConfig | None) -> AsyncIterator[None]:
    """Run one checkpointer statement, turning a pool or connection outage into `ConnectionError`.

    The same translation `core/db.connection()` makes, needed because this pool bypasses `core/db`:
    the front door classifies by type, and `PoolTimeout`/`PoolClosed` are neither `ConnectionError`
    nor `TimeoutError`. Only `psycopg.OperationalError` and pool errors are translated; a schema
    fault such as `UndefinedTable` must not be reported as a retryable outage. Applied to all four
    statements so the answer never depends on which one met the outage.

    Args:
        operation: What was being done, for the message the front door logs.
        config: The `configurable` naming the thread, for the same message; may be absent on some
            history reads.

    Raises:
        ConnectionError: The statement could not run — the same type `core/db.py` raises.
    """
    try:
        yield
    except psycopg.OperationalError as exc:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id", "")
        # Counted before re-raising so `chemclaw_degraded_total{subsystem="checkpointer"}` moves;
        # the caller must still fail.
        degraded(
            logger,
            "checkpointer",
            "could not %s turn state for session %s: %s",
            operation,
            thread_id,
            type(exc).__name__,
        )
        raise ConnectionError(
            f"checkpointer {operation} failed for session {thread_id!r}: {exc}"
        ) from exc


def _refuse_if_values_are_missing(stored: CheckpointTuple) -> None:
    """Refuse a checkpoint that no longer holds channel values it was written with.

    Without this, a thread whose blobs are gone but whose `checkpoints` row survives (a racing
    sweep, a restore, a partial `session_fork` copy, hand surgery) resumes silently as an empty
    conversation. Compares the writer's value stamp, not `channel_versions`, which legitimately
    names value-less channels. Unstamped checkpoints pass.

    Args:
        stored: The checkpoint tuple as loaded, values already merged from both stores.

    Raises:
        CheckpointValuesMissing: A stamped channel did not load back.
    """
    stamp = (stored.metadata or {}).get(CHECKPOINT_VALUES_KEY)
    if not isinstance(stamp, list):
        return
    loaded = stored.checkpoint.get("channel_values") or {}
    missing = [str(name) for name in stamp if name not in loaded]
    if not missing:
        return
    thread_id = stored.config.get("configurable", {}).get("thread_id", "")
    checkpoint_id = stored.config.get("configurable", {}).get("checkpoint_id", "")
    # `logger.error`, not `degraded`: nothing continues; the turn fails with its own type.
    logger.error(
        "refusing turn state for session %s: checkpoint %s has lost channel value(s) %s",
        thread_id,
        checkpoint_id,
        ", ".join(missing),
    )
    raise CheckpointValuesMissing(
        f"session {thread_id!r} has turn state that is half gone: checkpoint {checkpoint_id!r} was "
        f"written holding channel(s) {', '.join(missing)} and no longer has them. Rows this "
        "checkpoint needs have been deleted from checkpoint_blobs or checkpoint_writes without its "
        "own row going with them. Resuming would answer out of an empty conversation as if it were "
        "the whole one, so it is refused. Start a new session: this one's transcript and audit "
        "trail are separate stores and are unaffected."
    )


# A fenced turn's write takes a share lock on the turn's claim row before it writes, in the
# transaction that writes. A takeover's claim (`INSERT … ON CONFLICT DO UPDATE` on that row) waits
# for the lock, so no other replica can have taken the thread between the check and the commit; and
# a claim that is gone or lapsed finds no row, so a process that woke after its turn was resumed
# writes nothing.
_HOLD_CLAIM = (
    "SELECT 1 FROM session_turns WHERE session_id = %s AND holder = %s AND expires_at > now() "
    "FOR SHARE"
)


class SchemaStampedSaver(AsyncPostgresSaver):
    """`AsyncPostgresSaver` that records the channels it writes and refuses a thread missing one.

    Schema and value guards are on `aput` and `aget_tuple`, the only points where they matter;
    `alist` renders history and is unguarded. Outage translation covers all four statements, and
    `_cursor` counts the wait on the saver's lock.
    """

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncIterator[Any]:
        """Upstream's cursor, with the wait to get into it counted.

        Upstream takes one `asyncio.Lock` per saver before touching the pool, so every checkpointer
        statement in the process runs one at a time and the queue is invisible to pool metrics. The
        lock has no timeout; only the turn timeout bounds the wait. Counted on entry and exit
        because `asyncio.Lock` exposes no public waiter count.

        Args:
            pipeline: Passed straight through to upstream; see `AsyncPostgresSaver._cursor`.
        """
        global _statements_waiting
        started = time.perf_counter()
        _statements_waiting += 1
        fence = current_turn_fence() if pipeline else None
        try:
            if fence is None:
                async with super()._cursor(pipeline=pipeline) as cur:
                    # Sampled here, not in a `finally`, so it measures the wait alone, not the
                    # statement.
                    METRICS.observe(
                        "chemclaw_checkpointer_lock_wait_seconds", time.perf_counter() - started
                    )
                    yield cur
                return
            # A write by a turn that holds a claim: upstream's own pipeline, but inside a
            # transaction that first holds the claim row (`_HOLD_CLAIM`). `pipeline=True` is how
            # upstream marks a write; reads never reach here.
            async with self.lock, _ainternal.get_connection(self.conn) as conn:
                async with conn.transaction():
                    held = await conn.execute(
                        _HOLD_CLAIM, (fence.claim.session_id, fence.claim.holder)
                    )
                    if await held.fetchone() is None:
                        fence.lose()
                        raise TurnFenceLost
                    METRICS.observe(
                        "chemclaw_checkpointer_lock_wait_seconds", time.perf_counter() - started
                    )
                    async with conn.cursor(binary=True, row_factory=dict_row) as cur:
                        yield cur
        finally:
            _statements_waiting -= 1

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Write the checkpoint with this build's channel names, and the values it holds, stamped.

        `STATE_CHANNELS_KEY` is what this build declared; `CHECKPOINT_VALUES_KEY` is what this
        checkpoint held, read before `super().aput` splits values across stores. Once per turn the
        write is followed by `_prune_superseded`, after the write so a thread is never smaller than
        the checkpoint replacing it.

        Raises:
            ConnectionError: The checkpoint could not be written.
        """
        stamped = cast(
            CheckpointMetadata,
            {
                **metadata,
                STATE_CHANNELS_KEY: list(FIRST_PARTY_CHANNELS),
                # `.get`, so the stamp is never what fails a write on a hand-built checkpoint.
                CHECKPOINT_VALUES_KEY: sorted(checkpoint.get("channel_values") or {}),
            },
        )
        async with _translating("write", config):
            written = await super().aput(config, checkpoint, stamped, new_versions)
        await self._prune_superseded(config, metadata)
        return written

    async def _prune_superseded(self, config: RunnableConfig, metadata: CheckpointMetadata) -> None:
        """Delete the copies this thread's newest checkpoints have superseded.

        Once per turn: on the root namespace's `source == "input"` checkpoint, which LangGraph
        writes once per `ainvoke`; the statement covers every namespace. This bounds a thread at the
        retained checkpoints plus one turn's writes; write volume stays quadratic (upstream rewrites
        `messages` each superstep). A failure is logged at WARNING and never fails the turn: the
        checkpoint is already committed.

        Args:
            config: The `configurable` of the write, naming the thread and the namespace.
            metadata: The checkpoint's metadata, read for upstream's `source`.
        """
        keep = settings.checkpoint_retain_per_thread
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")
        if not keep or not thread_id:
            return
        if configurable.get("checkpoint_ns") or metadata.get("source") != "input":
            return
        try:
            async with self._cursor() as cur:
                await cur.execute(_PRUNE_SUPERSEDED, {"thread": thread_id, "keep": keep})
                await cur.fetchone()
        except psycopg.Error as exc:
            logger.warning(
                "could not prune superseded checkpoints for session %s: %s; the thread is intact "
                "and keeps every superseded copy until this succeeds",
                thread_id,
                exc,
            )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Write one task's pending channel updates, translating an outage like every other write.

        No stamp: `checkpoint_writes` holds task values, not checkpoint metadata.
        """
        async with _translating("write", config):
            await super().aput_writes(config, writes, task_id, task_path)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Load the checkpoint, refusing one that predates a channel this build declares.

        Also refuses one whose stored values no longer cover what it was written holding. An absent
        or unreadable stamp is treated as unstamped and resumed.

        Args:
            config: The `configurable` naming the thread (and optionally the checkpoint) to load.

        Returns:
            The stored checkpoint, or `None` when the thread has none.

        Raises:
            CheckpointValuesMissing: The stored checkpoint has lost channel values it was written
                holding.
            CheckpointSchemaMismatch: The stored checkpoint lacks a channel this build indexes.
            ConnectionError: The checkpoint could not be read — see `_translating`.
        """
        async with _translating("read", config):
            stored = await super().aget_tuple(config)
        if stored is None:
            return None
        _refuse_if_values_are_missing(stored)
        stamp = (stored.metadata or {}).get(STATE_CHANNELS_KEY)
        if not isinstance(stamp, list):
            return stored
        absent = [name for name in FIRST_PARTY_CHANNELS if name not in stamp]
        if not absent:
            return stored
        # Only now, on a thread an older build wrote, is it worth asking how the absent channels are
        # read — the answer is cached, and an ordinary turn never reaches this line.
        indexed = channels_read_without_default(absent)
        missing = [name for name in absent if name in indexed]
        if not missing:
            return stored
        held = ", ".join(str(name) for name in stamp) or "none"
        thread_id = stored.config.get("configurable", {}).get("thread_id", "")
        logger.error(
            "refusing turn state for session %s: it never held state channel(s) %s; it holds %s",
            thread_id,
            ", ".join(missing),
            held,
        )
        raise CheckpointSchemaMismatch(
            f"session {thread_id!r} has turn state from before this build declared the state "
            f"channel(s) {', '.join(missing)} (it holds {held}). LangGraph has no migration for "
            "that, and a turn resuming mid-graph raises a bare KeyError from whichever node "
            "indexes one of them. Start a new session: this one's transcript and audit trail are "
            "unaffected and its checkpoints stay until retention prunes them."
        )

    def alist(
        self,
        config: RunnableConfig | None,
        *,
        # `filter` shadows the builtin; it is upstream's parameter name and a caller
        # passes it by keyword, so renaming it here would break the override.
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Page a thread's history, translating an outage the way every other statement here does.

        A plain method returning the guarded generator, so the translation wraps the iteration
        itself rather than starting only at the first `__anext__`.
        """

        async def _guarded() -> AsyncIterator[CheckpointTuple]:
            async with _translating("read", config):
                async for stored in super(SchemaStampedSaver, self).alist(
                    config, filter=filter, before=before, limit=limit
                ):
                    yield stored

        return _guarded()


async def process_checkpointer() -> Any:
    """Turn state for a process that keeps **one** graph alive for its whole run.

    Durable where a database exists, `InMemorySaver` otherwise. Differs from
    `api/runner._turn_checkpointer`, which returns `None` without a database because the front door
    compiles a graph per turn. Lives here so the CLI need not import `langgraph` (layering).

    Returns:
        A checkpointer to build a long-lived graph on and to read that session's plan from.
    """
    if settings.session_store == "postgres":
        return await checkpointer()
    return InMemorySaver()


async def checkpointer() -> AsyncPostgresSaver:
    """The process's checkpointer, created and migrated on first use.

    Idempotent: `setup()` applies only missing migrations. A `SchemaStampedSaver` because durable
    checkpoints outlive the build that wrote them. Published only once usable, under `_init_lock`;
    a ready saver is returned without taking the lock.

    Returns:
        A ready saver over this process's checkpointer pool.
    """
    global _saver
    if _saver is not None:
        return _saver
    # Awaited outside the lock, because `_checkpoint_pool` takes that same lock itself and
    # `asyncio.Lock` is not reentrant.
    pool = await _checkpoint_pool()
    async with _initialization_lock():
        if _saver is None:
            saver = SchemaStampedSaver(pool, serde=_strict_serde())
            await _setup_once(saver, _session_dsn())
            _saver = saver
            logger.info("checkpointer ready")
    return _saver


# Advisory-lock key serializing checkpointer migrators; distinct from
# `core/migrate._MIGRATION_LOCK_KEY`, since advisory locks share one namespace per database.
_SETUP_LOCK_KEY = 0x43484D4157_00_03  # "CHMAW" + a discriminator for the checkpointer's setup

# Poll interval for a peer's `setup()`; ten seconds of polls covers this small migration. Not a
# config knob.
_SETUP_LOCK_POLL_SECONDS = 0.1
_SETUP_LOCK_POLLS = 100


async def _setup_once(saver: AsyncPostgresSaver, dsn: str) -> None:
    """Migrate the checkpoint tables under an advisory lock, so two pods cannot race each other.

    `setup()` is not race-safe across processes (concurrent runs collide on
    `checkpoint_migrations_pkey`), and a retry alone collides again while the winner is mid-run. The
    lock is polled with `pg_try_advisory_lock` rather than awaited, because a waiting lock holds a
    snapshot that `CREATE INDEX CONCURRENTLY` would wait on — a deadlock. It runs on a dedicated
    autocommit connection, since `setup()` uses the saver's pool. A pod that never gets the lock
    runs `setup()` anyway after the timeout rather than run without a checkpointer.

    Args:
        saver: The saver whose `setup()` to run.
        dsn: The database to take the advisory lock on — the saver's own.

    Raises:
        psycopg.Error: `setup()` itself failed for a reason that is not a concurrent migrator.
        ConnectionError: The lock connection could not be opened.
    """
    async with await db.connect(dsn) as guard:
        await guard.set_autocommit(True)
        held = False
        for attempt in range(_SETUP_LOCK_POLLS):
            cursor = await guard.execute("SELECT pg_try_advisory_lock(%s)", (_SETUP_LOCK_KEY,))
            row = await cursor.fetchone()
            if row is not None and row[0]:
                held = True
                break
            if attempt == 0:
                logger.info("another pod is migrating the checkpoint tables; waiting for it")
            await asyncio.sleep(_SETUP_LOCK_POLL_SECONDS)
        if not held:
            logger.warning(
                "waited %.0fs for another pod's checkpointer migration and never got the lock; "
                "running setup() anyway rather than leaving this process without a checkpointer",
                _SETUP_LOCK_POLLS * _SETUP_LOCK_POLL_SECONDS,
            )
        try:
            await saver.setup()
        finally:
            if held:
                # The lock also dies with the session (covering a killed pod); releasing it lets the
                # next caller in this process skip polling.
                await guard.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK_KEY,))


async def _checkpoint_pool() -> Any:
    """This process's checkpointer pool — autocommit, opened once.

    Takes `_init_lock` itself, so no caller may hold it (`asyncio.Lock` is not reentrant); both
    `checkpointer()` and `scratchpad.memory_store()` await this before taking the lock for their own
    object, so two cold callers never open two pools.
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _initialization_lock():
        if _pool is None:
            pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
                conninfo=_session_dsn(),
                kwargs={"autocommit": True, "connect_timeout": settings.pg_connect_timeout_seconds},
                # Unlike `core/db`'s pool: a process that never takes a turn (a Temporal worker)
                # holds no idle connections; the pool fills on demand.
                min_size=0,
                # Sized for the memory store sharing this pool, not the saver: the saver uses one
                # connection at a time, but `AsyncPostgresStore` is genuinely concurrent.
                max_size=settings.pg_pool_max_size,
                # Timeout, idle limit and connection check match `core/db._pool_for`, so a saturated
                # waiter is refused on the configured timeout and a backend killed from outside is
                # swapped rather than handed to a turn. `tests/test_checkpointer_concurrency.py`
                # asserts the two pools agree.
                timeout=settings.pg_pool_timeout_seconds,
                max_idle=settings.pg_pool_max_idle_seconds,
                check=AsyncConnectionPool.check_connection,
                open=False,
            )
            await pool.open()
            # Registered so this process's pool readings (and the fleet connection budget) include
            # it, though this module owns its lifecycle.
            register_pool(pool)
            # Bound here: `core/db.py` may not import `agent`, and every process with a checkpointer
            # has this queue.
            METRICS.bind_gauge(
                "chemclaw_checkpointer_statements_waiting", checkpointer_statements_waiting
            )
            _pool = pool
    return _pool


async def close_checkpointer() -> None:
    """Drop the process's checkpointer and close its pool — for tests and orderly shutdown.

    The saver goes with the pool because it is pinned to the loop it was built in. The memory store,
    which sits on this pool, is dropped first; this is `close_memory_store`'s only caller, so the
    ordering lives in one place. A pool whose loop has already closed is dropped rather than
    awaited, since closing it from another loop raises; its connections died with that loop.
    """
    global _saver, _pool, _init_lock
    # Imported here rather than at module scope: `scratchpad` pulls the deepagents backends in, and
    # a worker that only needs `CHECKPOINT_TABLES` should not import the agent's filesystem stack.
    from chemclaw.agent.scratchpad import close_memory_store

    await close_memory_store()
    _saver = None
    # Dropped too: an `asyncio.Lock` belongs to its loop, and the next loop's first caller would
    # wait on it forever.
    _init_lock = None
    pool, _pool = _pool, None
    if pool is None:
        return
    # Off the process's readings before it is closed, so a gauge scraped mid-shutdown does not
    # count connections that are going away.
    unregister_pool(pool)
    try:
        await pool.close()
    except RuntimeError:
        logger.debug("the checkpointer pool outlived its event loop; dropped without closing")
