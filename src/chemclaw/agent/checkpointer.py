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

**Every checkpoint carries the state schema it was written under, and a foreign one is refused by
name.** LangGraph restores a checkpoint's `channel_values` into channels built from the *current*
graph's state schema, and it has no migration system: a channel the checkpoint never held simply
stays empty, so the first node that reads it raises a bare `KeyError` naming a field. Measured on
this tree — a thread checkpointed under a state declaring `plan`, resumed under one declaring
`todos`, fails with exactly `KeyError: 'todos'`, from inside the node, with nothing in it naming the
thread, the schema change or a remedy. That is the whole failure mode: a redeploy that moves a state
field strands every session that already has turn state, and the only evidence is a key name.

`SchemaStampedSaver` closes it in the two places the shape is available at all: it writes
`STATE_SCHEMA_VERSION` into every checkpoint's metadata, and on resume it refuses a checkpoint
stamped with a *different* one, raising `CheckpointSchemaMismatch` with the thread, both versions
and the remedy in the message.

**Refusing rather than silently starting the thread over**, which is the same call
`agent/plan_state.py` makes for an unreadable plan and for the same reason: the two are
indistinguishable to a chemist and not at all indistinguishable in what they authorize. A turn that
resumes with the conversation dropped answers *normally* — confidently, out of context, with no
sign anything is missing — and a confidently wrong answer about a process is worse here than no
answer. Nothing is destroyed by the refusal: the checkpoint rows stay until `durable/retention.py`
prunes them, and the transcript (`session_messages`) and the audit chain are separate stores that
the checkpointer never held (D-2026-08-10 §3). What the chemist gets is `api/runner.py`'s ordinary
turn-failure event — classified `internal` and non-retryable, which is exactly right, because
retrying cannot move a checkpoint to a different schema — while the log carries this module's own
ERROR naming the session and both versions.

**An *unstamped* checkpoint is accepted.** Every checkpoint written before this guard existed has no
stamp, and refusing those would brick every live session at the deploy that introduces the guard —
the exact outcome the guard exists to prevent, caused by the guard. They resume as they always did,
and the first write of each thread stamps it from then on.
"""

import hashlib
import logging
from typing import Any, cast, get_type_hints

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

# The tables `AsyncPostgresSaver.setup()` creates. Named here because two other things need the
# list and neither can derive it: the erasure sweep (`agent/leaver.py`) has to delete a departing
# person's turn state, and its test has to prove the list is complete. `checkpoint_migrations` is
# deliberately absent from the *erasure* half — it holds schema versions, not anyone's conversation.
CHECKPOINT_TABLES: tuple[str, ...] = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

# The metadata key each checkpoint's state-schema stamp is written under. Metadata is a plain jsonb
# column the saver round-trips untouched, so this needs no migration and no table of its own — and
# it travels *with* the checkpoint, which is the only thing that makes the check possible on a
# thread whose writer was a different build.
STATE_SCHEMA_KEY = "chemclaw_state_schema"


def _state_schema_version(state: Any) -> str:
    """Fingerprint a graph state's declared channels — the thing an old checkpoint is read into.

    **Derived, not declared, because a version somebody has to remember to bump is a version that
    silently stops being one.** The whole failure this guards is invisible at the moment it is
    introduced: the change looks like an ordinary field rename and the tests pass, because nothing
    in a unit test has a checkpoint from the previous build. A constant in this file would be
    correct exactly as often as it was remembered, and the deploy where it was forgotten is the
    deploy that needs it.

    **The channel *names*, sorted, and nothing else.** A name is what a node indexes state by, so a
    name that appears, disappears or moves is precisely what turns into a `KeyError` on resume; a
    same-name type change is not caught, and that is stated rather than fixed because a type repr
    is not stable enough to hang a session's resumability on. Inherited fields count — this reads
    `ChemclawState` *and* the `PlanningState`/`AgentState` it extends — so a dependency bump that
    reshapes the upstream agent state moves the fingerprint too, which is the case no hand-written
    constant would ever have covered.

    Middleware that contributes its own state channels is outside this: `create_agent` merges those
    in and this module cannot see them without importing the agent builder that imports it. So the
    fingerprint is a strong signal, not a proof of compatibility — it catches the schema this
    repository declares, and it never fires falsely on one it does not.

    Args:
        state: The graph state class to fingerprint — `ChemclawState` in this process, and a
            stand-in in the test that proves the fingerprint tracks a schema in both directions.

    Returns:
        Twelve hex characters identifying that state schema. Short because it is written on every
        checkpoint row and it identifies a schema rather than authenticating one.
    """
    channels = ",".join(sorted(get_type_hints(state, include_extras=True)))
    return hashlib.sha256(channels.encode()).hexdigest()[:12]


STATE_SCHEMA_VERSION = _state_schema_version(ChemclawState)


class CheckpointSchemaMismatch(RuntimeError):
    """A thread's turn state was written under a different graph state schema than this build's.

    Raised instead of letting the restore proceed to the `KeyError` it would otherwise produce
    somewhere inside a node. Its own type is the point: a caller can tell "this session predates a
    schema change" from "the database is down", which is not something a `KeyError` on a field name
    supports.
    """


class SchemaStampedSaver(AsyncPostgresSaver):
    """`AsyncPostgresSaver` that records the state schema it writes and refuses a foreign one.

    Two overrides, on the write and the resume, because those are the only two points where the
    schema is knowable and where it matters. `alist` is deliberately not guarded: history reads
    render checkpoints, they do not restore them into a running graph, and a schema change is not a
    reason to stop showing what a session did.

    The module docstring holds the argument for refusing rather than resuming empty.
    """

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Write the checkpoint with this build's state-schema stamp in its metadata."""
        stamped = cast(CheckpointMetadata, {**metadata, STATE_SCHEMA_KEY: STATE_SCHEMA_VERSION})
        return await super().aput(config, checkpoint, stamped, new_versions)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Load the checkpoint, refusing one stamped with a different state schema.

        Args:
            config: The `configurable` naming the thread (and optionally the checkpoint) to load.

        Returns:
            The stored checkpoint, or `None` when the thread has none.

        Raises:
            CheckpointSchemaMismatch: The stored checkpoint was written under a different state
                schema, so restoring it would fail inside a node instead of here.
        """
        stored = await super().aget_tuple(config)
        if stored is None:
            return None
        stamp = (stored.metadata or {}).get(STATE_SCHEMA_KEY)
        if stamp is None or stamp == STATE_SCHEMA_VERSION:
            return stored
        thread_id = stored.config.get("configurable", {}).get("thread_id", "")
        logger.error(
            "refusing turn state for session %s: written under state schema %s, this build "
            "declares %s",
            thread_id,
            stamp,
            STATE_SCHEMA_VERSION,
        )
        raise CheckpointSchemaMismatch(
            f"session {thread_id!r} has turn state written under a different graph state schema "
            f"(checkpoint {stamp}, this build {STATE_SCHEMA_VERSION}). LangGraph has no migration "
            "for that, and restoring it raises a bare KeyError from whichever node reads the field "
            "that moved. Start a new session: this one's transcript and audit trail are unaffected "
            "and its checkpoints stay until retention prunes them."
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

    Returns:
        A ready saver over this process's checkpointer pool.
    """
    global _saver
    if _saver is None:
        _saver = SchemaStampedSaver(await _checkpoint_pool())
        await _saver.setup()
        logger.info("checkpointer ready (%d tables)", len(CHECKPOINT_TABLES))
    return _saver


async def _checkpoint_pool() -> Any:
    """This process's checkpointer pool — autocommit, opened once.

    `min_size=0` because a process that never takes a turn (a Temporal worker running calculations)
    should not hold connections open for a checkpointer it will not use, and the pool fills on
    demand.
    """
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=_session_dsn(),
            kwargs={"autocommit": True, "connect_timeout": settings.pg_connect_timeout_seconds},
            min_size=0,
            max_size=settings.pg_pool_max_size,
            open=False,
        )
        await _pool.open()
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
    global _saver, _pool
    _saver = None
    pool, _pool = _pool, None
    if pool is None:
        return
    try:
        await pool.close()
    except RuntimeError:
        logger.debug("the checkpointer pool outlived its event loop; dropped without closing")
