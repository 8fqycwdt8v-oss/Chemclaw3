"""A cluster-wide, non-blocking lock for work that must run in one pod at a time.

`exclusive_job(name)` takes a Postgres session-level advisory lock and reports whether this caller
holds it. It never waits: a caller that does not hold the lock skips its work, so N replicas
polling one queue do no more than one replica would. The lock belongs to the backend connection,
so a pod that is killed, partitioned until its TCP session dies, or evicted frees it without
anyone running a cleanup, and the next caller takes over.

Invariants:
- The key folds in `current_schema()`, so deployments and tests sharing one database do not
  contend, while every pod of one deployment (one `search_path`) agrees on it.
- The connection is dedicated, not a pool slot, and comes from the session layer's DSN
  (`session_store_dsn`, else `postgres_dsn`). It runs in autocommit, so the lock is never held by
  an idle transaction a server timeout can end. Behind a transaction pooler that DSN must be a
  session-mode endpoint. It is one connection per held lock outside the pools, which
  `postgres.maxConnections` leaves room for (`tests/test_deploy_chart.py`).
- A memory session store means one process; the block runs unlocked unless the caller says the
  work itself lives in a shared database (`shared_database=True`).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import psycopg

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# One expression for the key, so the try-lock and the unlock cannot disagree about it.
_KEY = "hashtextextended(current_schema() || ':job:' || %s, 0)"
_TRY = f"SELECT pg_try_advisory_lock({_KEY})"
_UNLOCK = f"SELECT pg_advisory_unlock({_KEY})"


@asynccontextmanager
async def exclusive_job(name: str, *, shared_database: bool = False) -> AsyncIterator[bool]:
    """Hold the cluster-wide lock `name` for the block, or yield `False` without waiting.

    Args:
        name: A stable literal naming the job (`"note-reindex"`), never request-derived; it is
            the metric label and part of the lock key.
        shared_database: The job writes to Postgres that other processes also write, so it is
            locked even when the session store is `memory` (a hand-run beside the pods).

    Yields:
        `True` when this caller holds the lock for the whole block; `False` when another holder
        has it, in which case the caller must do nothing.

    Raises:
        ConnectionError: The lock connection could not be had. Not swallowed into `False`: a job
            that cannot reach Postgres must fail and be retried, not report itself skipped.
    """
    if settings.session_store != "postgres" and not shared_database:
        yield True
        return
    dsn = settings.session_store_dsn or settings.postgres_dsn
    async with await db.connect(dsn) as conn:
        await conn.set_autocommit(True)
        cursor = await conn.execute(_TRY, (name,))
        row = await cursor.fetchone()
        held = bool(row and row[0])
        if not held:
            record_metric(
                lambda m: m.increment("chemclaw_job_lock_skipped_total", labels={"job": name})
            )
            log_event(
                logger,
                "job_lock.skipped",
                "%s is already running on another worker; this one does nothing",
                name,
                job=name,
            )
        try:
            yield held
        finally:
            if held:
                # Closing the connection releases the lock anyway; this just frees it sooner.
                with suppress(psycopg.Error):
                    await conn.execute(_UNLOCK, (name,))
