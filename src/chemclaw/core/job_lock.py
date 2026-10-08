"""A cluster-wide, non-blocking lock for work that must run in one pod at a time.

`exclusive_job(name)` takes a Postgres session-level advisory lock and reports whether this caller
holds it. It never waits: a caller that does not hold the lock skips its work, so N replicas
polling one queue do no more than one replica would. The lock belongs to the backend connection,
so a pod that is killed, partitioned until its TCP session dies, or evicted frees it without
anyone running a cleanup, and the next caller takes over.

Invariants:
- The key folds in `current_schema()`, so deployments and tests sharing one database do not
  contend, while every pod of one deployment (one `search_path`) agrees on it.
- The connection comes from the session layer's DSN (`session_store_dsn`, else `postgres_dsn`)
  and runs in autocommit, so the lock is never held by an idle transaction a server timeout can
  end. Behind a transaction pooler that DSN must be a session-mode endpoint (the deployment guide
  lists which connections need it).
- A memory session store means one process; the block runs unlocked.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
async def exclusive_job(name: str) -> AsyncIterator[bool]:
    """Hold the cluster-wide lock `name` for the block, or yield `False` without waiting.

    Args:
        name: A stable literal naming the job (`"note-reindex"`), never request-derived; it is
            the metric label and part of the lock key.

    Yields:
        `True` when this caller holds the lock for the whole block; `False` when another holder
        has it, in which case the caller must do nothing.

    Raises:
        ConnectionError: The lock connection could not be had. Not swallowed into `False`: a job
            that cannot reach Postgres must fail and be retried, not report itself skipped.
    """
    if settings.session_store != "postgres":
        yield True
        return
    dsn = settings.session_store_dsn or settings.postgres_dsn
    async with db.connection(dsn, operation=f"job_lock:{name}") as conn:
        await conn.set_autocommit(True)
        try:
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
                    try:
                        await conn.execute(_UNLOCK, (name,))
                    except Exception:
                        # A lock that cannot be released explicitly dies with its backend, so the
                        # connection is closed rather than returned to the pool still holding it.
                        await conn.close()
        finally:
            if not conn.closed:
                await conn.set_autocommit(False)
