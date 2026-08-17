"""The LangGraph turn-state checkpointer, on its own Postgres pool (M6, D-2026-08-10).

Where MAF gave layer 1 no durability at all — so Chemclaw hand-built it in `agent/session_store.py`
— LangGraph ships a checkpointer, and D-2026-08-10 §3 draws the line: Temporal keeps every long or
expensive job, and this takes turn state, rollback and resume. `interrupt()` needs it too; without a
checkpointer there is nowhere for a suspended turn to live.

**Its own pool, deliberately, and for three measured reasons.** `core/db.py` owns the shared pool
that the calculation cache, the vector index and the session store borrow from. `AsyncPostgresSaver`
must not join it:

1. **`setup()` cannot run there.** Three of its ten migrations are `CREATE INDEX CONCURRENTLY`,
   which Postgres refuses inside a transaction block, and `db._pool_for` builds pools without
   `autocommit`, so psycopg opens an implicit transaction on the first execute.
2. **One `asyncio.Lock` per saver serializes every checkpointer statement**, and `alist` yields
   *inside* both that lock and the borrowed connection. A paginated history read would therefore
   hold a shared-pool connection for its entire iteration, starving call sites that have nothing to
   do with conversations.
3. The saver enters **pipeline mode** on the connection it borrows, which is not something to do to
   a connection another subsystem may have opinions about.

A separate pool is also what makes the first point cheap: this one is opened with
`autocommit=True`, so `setup()` just works and every checkpointer write is its own transaction,
which is what a checkpoint already is.

**One saver per process, pinned to its loop.** `AsyncPostgresSaver.__init__` calls
`asyncio.get_running_loop()` and keeps it, so the saver cannot outlive or precede the loop it was
built in — hence the async factory rather than a module-level instance.

**Every checkpoint records the state channels this repository declared when it was written, and a
thread that never held one this build declares is refused by name.** LangGraph restores a
checkpoint's `channel_values` into channels built from the *current* graph's state schema, and it
has no migration system: a channel the checkpoint never held simply stays empty, so a node that
indexes it raises a bare `KeyError` naming the field — from inside the node, with nothing in it
naming the thread, the schema change or a remedy.

**The direction that fails is not the intuitive one, so it was measured.** Each finding below names
the test in `tests/test_checkpointer_schema.py` that asserts it, because that test is the record
that cannot go stale — the prose restating its numbers here could, and this file is where a reader
decides whether the guard still means what it says:

- An **added** name is what raises — a channel this build declares that the checkpoint does not
  hold. A rename is an addition plus a removal, and it is the addition half that raises
  (`..._the_added_half_of_a_rename_that_raises_and_not_the_removed_half`).
- A **removed** channel is harmless: nothing declares it any more, so nothing indexes it
  (`..._a_channel_this_build_no_longer_declares_does_not_refuse_the_thread`).
- It only bites a turn resumed *inside* the graph, which is what `interrupt()` produces. At a turn
  boundary the run starts at `START` and a node indexing an unwritten channel fails identically on
  a brand-new thread, so the checkpoint contributed nothing to that one
  (`..._a_moved_channel_strands_a_turn_resumed_inside_the_graph`).
- `NotRequired` is not a filter this can use: it says how the *input* may be spelled, not how a
  node reads the channel (`..._notrequired_does_not_make_an_added_channel_safe`).

`SchemaStampedSaver` writes `FIRST_PARTY_CHANNELS` into every checkpoint's metadata, and on resume
refuses a checkpoint whose stamp is missing one of them, raising `CheckpointSchemaMismatch` naming
the thread, the missing channels and the remedy.

**Only the channels this repository declares, and that is the whole point of the exclusion.**
`ChemclawState` extends langchain's `PlanningState`, from which `messages`, `jump_to`,
`structured_response` and `todos` arrive. A stamp over all six would move on any langchain minor
bump that adds or renames one of *its* channels, refusing every in-flight thread in the fleet on a
dependency change nobody associated with turn state — the guard causing the exact harm it exists to
prevent. Middleware channels are outside it for a second reason: `create_agent` merges those in and
this module cannot see them without importing the agent builder that imports it.

**What is not caught, and where the refusal is deliberately wider than the failure.** Not caught: a
same-name *type* change (a type repr is not stable enough to hang a session's resumability on); an
upstream or middleware channel that moves; a first-party channel that is only *removed* (measured
harmless above). Wider than the failure: an added channel is refused even when every reader of it
uses `.get()` and the resume would have worked, because the stamp holds names and cannot see how a
node reads one. That over-refusal lands on a change this repository is itself deploying — which it
can drain sessions for, and which the paragraph below says it should — never on a dependency's.

**Refusing rather than silently starting the thread over**, which is the same call
`agent/plan_state.py` makes for an unreadable plan and for the same reason: the two are
indistinguishable to a chemist and not at all indistinguishable in what they authorize. A turn that
resumes with the conversation dropped answers *normally* — confidently, out of context, with no
sign anything is missing — and a confidently wrong answer about a process is worse here than no
answer. Nothing is destroyed by the refusal: the checkpoint rows stay until `durable/retention.py`
prunes them, and the transcript (`session_messages`) and the audit chain are separate stores that
the checkpointer never held (D-2026-08-10 §3). What the chemist gets is `api/runner.py`'s ordinary
turn-failure event — classified `internal` and non-retryable, which is exactly right, because
retrying cannot give a checkpoint a channel it never held — while the log carries this module's own
ERROR naming the session, the missing channels and the ones the thread does hold.

**An *unstamped* checkpoint is accepted, and so is a stamp this build cannot read.** Refusing those
would brick every live session at the deploy that introduces the guard — the exact outcome the
guard exists to prevent, caused by the guard. They resume as they always did, and the first write
of each thread stamps it from then on. The same rule covers a *rolling* deploy in both directions,
because the stamp lives under its own metadata key
(`test_a_checkpoint_from_before_the_guard_resumes_rather_than_being_refused`,
`test_a_stamp_this_build_cannot_read_is_treated_as_absent`).
"""

import asyncio
import logging
from typing import Any, cast, get_origin, get_type_hints

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from chemclaw.agent.session_store import _session_dsn
from chemclaw.agent.state import ChemclawState
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

_saver: AsyncPostgresSaver | None = None
_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None

# Guards the two lazy initializations below, which are each a check-then-*await*-then-act.
#
# **Publishing before the await is what made this a race rather than a style question.** Both
# `checkpointer()` and `_checkpoint_pool()` assigned their global *before* awaiting the work that
# makes the object usable — `setup()`'s ten migrations, and `pool.open()`. A second turn arriving
# inside either await saw a non-`None` global and got a saver whose tables do not exist yet, or a
# pool that is not open: `relation "checkpoints" does not exist` on a cold start with traffic,
# which is every deploy of a two-replica chart, since `api/runner._turn_checkpointer()` is awaited
# once per turn.
#
# Created lazily rather than at import, for the reason the saver itself is: `asyncio.Lock` binds to
# the running loop, and this module is imported by processes (Temporal workers, the CLI) that build
# their loop later or never. `close_checkpointer` drops it with the pool so the next loop gets its
# own.
_init_lock: asyncio.Lock | None = None


def _initialization_lock() -> asyncio.Lock:
    """The current loop's initialization lock, created on first use.

    Not a race itself: this is called from coroutines, so it runs on the single thread of one event
    loop and cannot be interleaved before the assignment — there is no `await` between the check
    and the store.
    """
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


# The tables `AsyncPostgresSaver.setup()` creates. Named here because two other things need the
# list and neither can derive it: the erasure sweep (`agent/leaver.py`) has to delete a departing
# person's turn state, and its test has to prove the list is complete. `checkpoint_migrations` is
# deliberately absent from the *erasure* half — it holds schema versions, not anyone's conversation.
CHECKPOINT_TABLES: tuple[str, ...] = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

# The metadata key each checkpoint's channel stamp is written under. Metadata is a plain jsonb
# column the saver round-trips untouched, so this needs no migration and no table of its own — and
# it travels *with* the checkpoint, which is the only thing that makes the check possible on a
# thread whose writer was a different build.
#
# Its own key rather than the `chemclaw_state_schema` one the first version of this guard used,
# because the *value* changed shape (a schema hash then, a channel list now) and a rolling deploy
# runs both builds at once. Under one key each build would read the other's value as a mismatch and
# refuse the thread; under two, each reads the other's checkpoints as unstamped and resumes them.
STATE_CHANNELS_KEY = "chemclaw_state_channels"


def _first_party_channels(state: Any) -> tuple[str, ...]:
    """The channel names `state` declares itself, with those of the base it extends left out.

    **Derived, not declared, because a version somebody has to remember to bump is a version that
    silently stops being one.** The failure this guards is invisible at the moment it is
    introduced: the change looks like an ordinary field rename and every test passes, because
    nothing in a unit test has a checkpoint from the previous build.

    **Names only.** A name is what a node indexes state by, so a name that appears is precisely what
    becomes a `KeyError` on a mid-turn resume. A same-name type change is not covered, and that is
    stated rather than fixed because a type repr is not stable enough to hang a session's
    resumability on.

    **The base's channels are subtracted, and that is the reason this function exists rather than a
    one-line `get_type_hints`.** A `TypedDict` merges its bases' annotations into its own
    `__annotations__` (measured on 3.11: `ChemclawState.__annotations__` reports all six channels,
    four of them langchain's), so "what this repository declares" is not directly readable and has
    to be computed by difference. `__orig_bases__` is where the pre-merge base list survives. It is
    only populated when a base is generic — true of `PlanningState`, which extends
    `AgentState[ResponseT]` — so the subtraction can silently become a no-op if that ever changes;
    `tests/test_checkpointer_schema.py` asserts the result stays disjoint from the upstream base's
    channels, which turns that into a red build rather than a fleet-wide refusal.

    Args:
        state: The graph state class to read — `ChemclawState` in this process, and stand-in
            classes in the tests that prove what the derivation includes and excludes.

    Returns:
        The names this class adds to its base, sorted, so declaration order cannot move the stamp.
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
    return tuple(sorted(set(get_type_hints(state, include_extras=True)) - inherited))


FIRST_PARTY_CHANNELS = _first_party_channels(ChemclawState)


class CheckpointSchemaMismatch(RuntimeError):
    """A thread's turn state never held a state channel this build declares.

    Raised instead of letting the restore proceed to the `KeyError` a node indexing that channel
    would otherwise produce. Its own type is the point: a caller can tell "this session predates a
    state change" from "the database is down", which is not something a `KeyError` on a field name
    supports.
    """


class SchemaStampedSaver(AsyncPostgresSaver):
    """`AsyncPostgresSaver` that records the channels it writes and refuses a thread missing one.

    Two overrides, on the write and the resume, because those are the only two points where the
    state schema is knowable and where it matters. `alist` is deliberately not guarded: history
    reads render checkpoints, they do not restore them into a running graph, and a state change is
    not a reason to stop showing what a session did.

    The module docstring holds what is and is not caught, and the argument for refusing rather than
    resuming empty.
    """

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Write the checkpoint with this build's first-party channel names in its metadata."""
        stamped = cast(
            CheckpointMetadata, {**metadata, STATE_CHANNELS_KEY: list(FIRST_PARTY_CHANNELS)}
        )
        return await super().aput(config, checkpoint, stamped, new_versions)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Load the checkpoint, refusing one that predates a channel this build declares.

        A stamp that is absent, or that this build cannot read — the schema-hash string the first
        version of this guard wrote, or anything else that is not a list of names — is treated the
        same as an unstamped checkpoint and resumed, for the module docstring's reason: refusing it
        would brick live sessions on the deploy that changed the stamp.

        Args:
            config: The `configurable` naming the thread (and optionally the checkpoint) to load.

        Returns:
            The stored checkpoint, or `None` when the thread has none.

        Raises:
            CheckpointSchemaMismatch: The stored checkpoint never held a channel this build
                declares, so restoring it can fail inside a node instead of here.
        """
        stored = await super().aget_tuple(config)
        if stored is None:
            return None
        stamp = (stored.metadata or {}).get(STATE_CHANNELS_KEY)
        if not isinstance(stamp, list):
            return stored
        missing = [name for name in FIRST_PARTY_CHANNELS if name not in stamp]
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


async def process_checkpointer() -> Any:
    """Turn state for a process that keeps **one** graph alive for its whole run.

    Durable where the deployment has a database, `InMemorySaver` otherwise — which is a real
    conversation for as long as the process lives, and the honest lifetime for a run whose
    transcript is not stored anywhere either.

    **Not the same question `api/runner._turn_checkpointer` answers, and the difference is the
    graph's lifetime.** The front door compiles a graph *per turn* (it binds that turn's connector
    tools at construction), so an in-memory saver there would be created and discarded inside one
    turn and hold nothing — `None` is the truthful answer for a deployment with no database. A
    terminal session builds its graph once, so the same saver spans every turn of the run and the
    in-memory branch is worth taking.

    Here rather than in `chemclaw.cli` because the caller must not name `langgraph` at module scope:
    `tests/test_third_party_layering.py` polices which package may depend on which third-party
    stack, and the CLI is not one that owns this one. That is not a formality worked around — this
    module is what "where turn state lives" means, so the decision belongs beside the durable
    saver it chooses between.

    Returns:
        A checkpointer to build a long-lived graph on and to read that session's plan from.
    """
    if settings.session_store == "postgres":
        return await checkpointer()
    return InMemorySaver()


async def checkpointer() -> AsyncPostgresSaver:
    """The process's checkpointer, created and migrated on first use.

    Idempotent: `setup()` records applied versions in `checkpoint_migrations` and applies only what
    is missing, so calling this on every agent build costs one query after the first.

    A `SchemaStampedSaver` rather than a bare `AsyncPostgresSaver`, because the durable saver is the
    one whose checkpoints outlive the build that wrote them — the in-memory saver
    `process_checkpointer` falls back to cannot be resumed by a different schema at all, since it
    dies with the process that declared one.

    **Published only once it is usable, under `_init_lock`.** The assignment used to happen before
    `setup()` was awaited, so a concurrent second turn saw a non-`None` global and got a saver whose
    migrations had not run — see the lock's own comment. A ready saver is returned without taking
    the lock at all, so the steady-state cost is one `is None` check.

    Returns:
        A ready saver over this process's checkpointer pool.
    """
    global _saver
    if _saver is not None:
        return _saver
    async with _initialization_lock():
        if _saver is None:
            saver = SchemaStampedSaver(await _checkpoint_pool())
            await saver.setup()
            _saver = saver
            logger.info("checkpointer ready")
    return _saver


async def _checkpoint_pool() -> Any:
    """This process's checkpointer pool — autocommit, opened once.

    `min_size=0` because a process that never takes a turn (a Temporal worker running calculations)
    should not hold connections open for a checkpointer it will not use, and the pool fills on
    demand.

    **Called only from `checkpointer()`, which already holds `_init_lock` — so this takes no lock
    of its own**, and must not: `asyncio.Lock` is not reentrant and a second acquire here would
    deadlock the first turn of every process. The global is nevertheless published only after
    `open()` returns, so the ordering is right on its own terms rather than by inheritance from
    its one caller.
    """
    global _pool
    if _pool is None:
        pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            conninfo=_session_dsn(),
            kwargs={"autocommit": True, "connect_timeout": settings.pg_connect_timeout_seconds},
            min_size=0,
            max_size=settings.pg_pool_max_size,
            open=False,
        )
        await pool.open()
        _pool = pool
    return _pool


async def close_checkpointer() -> None:
    """Drop the process's checkpointer and close its pool — for tests and orderly shutdown.

    The saver is dropped with the pool because it holds both the pool *and* the loop it was built
    in; keeping one without the other is how a second caller in a second event loop gets a saver
    pinned to a loop that has closed.

    **A pool whose loop has already closed is dropped, not awaited.** `psycopg_pool` schedules its
    workers' shutdown on the loop it was opened in, so closing it from a *different* live loop
    raises `RuntimeError: Event loop is closed` — from inside the close, after the reference would
    otherwise have been cleared, leaving the process holding a pool nobody can close. Production has
    one loop, so this is a test-shaped hazard; it is handled here rather than in the tests because
    the alternative is every caller remembering which loop opened the pool. The connections are
    released with their dead loop either way, so there is nothing left to leak.
    """
    global _saver, _pool, _init_lock
    _saver = None
    # Dropped with the pool for the same reason the saver is: an `asyncio.Lock` belongs to the loop
    # it was created in, so a lock kept across `close_checkpointer` would be one the next loop's
    # first caller waits on forever.
    _init_lock = None
    pool, _pool = _pool, None
    if pool is None:
        return
    try:
        await pool.close()
    except RuntimeError:
        logger.debug("the checkpointer pool outlived its event loop; dropped without closing")
