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
"""

import logging
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from chemclaw.agent.session_store import _session_dsn
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

_saver: AsyncPostgresSaver | None = None
_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None

# The tables `AsyncPostgresSaver.setup()` creates. Named here because two other things need the
# list and neither can derive it: the erasure sweep (`agent/leaver.py`) has to delete a departing
# person's turn state, and its test has to prove the list is complete. `checkpoint_migrations` is
# deliberately absent from the *erasure* half — it holds schema versions, not anyone's conversation.
CHECKPOINT_TABLES: tuple[str, ...] = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def checkpointer() -> AsyncPostgresSaver:
    """The process's checkpointer, created and migrated on first use.

    Idempotent: `setup()` records applied versions in `checkpoint_migrations` and applies only what
    is missing, so calling this on every agent build costs one query after the first.

    Returns:
        A ready `AsyncPostgresSaver` over this process's checkpointer pool.
    """
    global _saver
    if _saver is None:
        _saver = AsyncPostgresSaver(await _checkpoint_pool())
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
