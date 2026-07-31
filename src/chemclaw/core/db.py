"""Shared Postgres connect helper and the per-process connection pool.

Why this exists: both the calculation store (`chemclaw.science.calc.postgres_store`) and the
fingerprint
store (`chemclaw.science.fingerprints.store`) open short-lived psycopg connections, and a down or
misconfigured database otherwise surfaces as a raw `psycopg.OperationalError` traceback that
never says *which* database or *why*. This wraps the connect once (DRY) so every caller
reports "Postgres unreachable at <host>: <cause>" with the DSN password redacted.

The failure is raised as `ConnectionError`, deliberately **not** a `ChemclawError`: an
unreachable database is a transient infrastructure fault, so Temporal should retry the
activity, whereas `ChemclawError` (a `ValueError`) is marked non-retryable bad data.

**Pooling.** Connect-per-call was measured at 401 TCP+TLS+auth handshakes for 150 chat turns —
~2.7 per turn — and the cost is not the database (peak 28 of `max_connections=100`, mostly
idle) but the *event loop*: a connect that cannot be scheduled within
`pg_connect_timeout_seconds` fails, and one of the call sites that failed this way is the
rollback watermark (D-107), a correctness guard whose handler is deliberately non-fatal. So
the churn did not merely cost latency, it disarmed a guard.

`connection()` is the one call sites use. It borrows from a per-process pool when a process
has opened one (`pooling()`, entered by the front door's lifespan and each worker's
entrypoint) and otherwise falls back to a dedicated connect — so a script, a migration, or a
test keeps today's behavior with no setup. Pools are keyed by `(dsn, libpq options)` because
the statement timeout rides on the connection's `options`, so two call sites asking for
different timeouts must not share connections.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg import conninfo
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout

from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# One pool per (dsn, merged libpq options). The options string carries the statement timeout, so
# keying on it keeps a migration's untimed connection out of the stores' 30s-bounded pool.
_PoolKey = tuple[str, str | None]
_Pool = AsyncConnectionPool[psycopg.AsyncConnection[TupleRow]]
_POOLS: dict[_PoolKey, _Pool] = {}
# Whether this process has entered `pooling()`. Off means `connection()` opens a dedicated
# connection per call — the pre-pool behavior, which is what a one-shot script or a test wants.
_POOLING = False


def _redact(dsn: str) -> str:
    """Return `dsn` with any password removed, so it is safe to echo in an error message.

    Round-trips through libpq's own parser (`conninfo_to_dict`/`make_conninfo`) so every
    form psycopg accepts is covered — URL userinfo, URL query parameter, and the keyword
    `host=... password=...` form — not just the userinfo case a URL split can see. A DSN
    libpq cannot parse is replaced wholesale rather than echoed on a guess.
    """
    try:
        parts = conninfo.conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        return "<postgres>"
    parts.pop("password", None)
    return conninfo.make_conninfo("", **parts)


def _merged_options(dsn: str, statement_timeout_seconds: float | None) -> str | None:
    """Return the libpq `options` to connect with: the DSN's own, plus our statement timeout.

    psycopg merges a keyword argument *over* the connection string, so passing `options=` would
    silently discard any `options` the DSN already carries — and only on the connections that ask
    for a statement timeout, since `None` is dropped rather than merged. An operator who sets
    `options` in their DSN (a `search_path` for a shared database, `application_name`, `work_mem`)
    would lose it non-deterministically depending on the call site. Concatenating instead keeps
    both; libpq reads the last occurrence of a repeated `-c` setting, so our timeout still wins if
    the DSN happens to set one too.
    """
    if not statement_timeout_seconds:
        return None  # nothing of ours to add; the DSN's own `options` passes through untouched
    # libpq statement_timeout is in milliseconds; passed as a server option so it applies to
    # every statement on the connection without an extra round trip.
    ours = f"-c statement_timeout={int(statement_timeout_seconds * 1000)}"
    try:
        existing = conninfo.conninfo_to_dict(dsn).get("options")
    except psycopg.ProgrammingError:
        return ours  # unparseable DSN: let the connect itself report it, don't mask the error
    return f"{existing} {ours}" if isinstance(existing, str) and existing else ours


async def connect(
    dsn: str, *, statement_timeout_seconds: float | None = None
) -> psycopg.AsyncConnection[TupleRow]:
    """Open a *dedicated* Postgres connection, failing fast and clearly when unreachable.

    Uses the configured libpq `connect_timeout` so an unreachable host errors quickly instead
    of hanging the calling activity until its start-to-close timeout. A connection failure is
    re-raised as `ConnectionError` carrying the password-redacted DSN and the underlying
    cause, so an admin immediately sees which database failed and why.

    `statement_timeout_seconds` sets a per-statement wall-clock bound (libpq
    `statement_timeout`) so a hung query is cancelled rather than burning the enclosing
    activity's whole budget. Omit (or pass 0/None) for no per-statement bound — the
    migration runner does this, since an index build may legitimately run long.

    Prefer `connection()`: this opens a connection nobody pools, which is right for a migration
    or a one-shot script and wrong for anything on a request path.
    """
    options = _merged_options(dsn, statement_timeout_seconds)
    try:
        return await psycopg.AsyncConnection.connect(
            dsn, connect_timeout=settings.pg_connect_timeout_seconds, options=options
        )
    except psycopg.OperationalError as exc:
        raise ConnectionError(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc


def _pool_for(dsn: str, options: str | None) -> _Pool:
    """Return this process's pool for `(dsn, options)`, constructing it on first use.

    Constructed lazily rather than up front because a process does not know which DSNs it will
    touch until it touches them (the session store, the calculation store and the fingerprint
    store may be three different databases or one). Construction is synchronous and the
    dictionary insert happens before any `await`, so two coroutines racing on the first use of a
    DSN cannot end up with two pools for it.
    """
    key = (dsn, options)
    pool = _POOLS.get(key)
    if pool is None:
        pool = AsyncConnectionPool(
            conninfo=dsn,
            connection_class=psycopg.AsyncConnection[TupleRow],
            kwargs={"connect_timeout": settings.pg_connect_timeout_seconds, "options": options},
            min_size=settings.pg_pool_min_size,
            max_size=settings.pg_pool_max_size,
            max_idle=settings.pg_pool_max_idle_seconds,
            timeout=settings.pg_pool_timeout_seconds,
            # Opened by the caller below: constructing with `open=True` schedules the background
            # workers from `__init__`, which psycopg_pool warns about outside a running loop.
            open=False,
        )
        _POOLS[key] = pool
    return pool


@asynccontextmanager
async def connection(
    dsn: str, *, statement_timeout_seconds: float | None = None
) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
    """Borrow a connection for the duration of the block — pooled when this process pools.

    The single call site helper: on exit the transaction is committed (or rolled back if the
    block raised), exactly as `async with await connect(...)` did, and the connection goes back
    to the pool instead of being torn down. In a process that never entered `pooling()` this is
    a dedicated connect, so behavior is unchanged for scripts, migrations and tests.

    A pool that cannot hand out a connection in `pg_pool_timeout_seconds` raises the same
    `ConnectionError` an unreachable database raises, and for the same reason: from the caller's
    point of view "no connection available" and "no database" are one transient infrastructure
    fault, and Temporal must retry both.
    """
    options = _merged_options(dsn, statement_timeout_seconds)
    if not _POOLING:
        conn = await connect(dsn, statement_timeout_seconds=statement_timeout_seconds)
        async with conn:
            yield conn
        return
    pool = _pool_for(dsn, options)
    await pool.open()  # idempotent; the first caller starts the pool's background workers
    try:
        async with pool.connection() as conn:
            yield conn
    except (PoolTimeout, PoolClosed) as exc:
        # Both are `psycopg.OperationalError` subclasses raised only by the checkout itself, so
        # catching them here cannot swallow an error from the caller's block.
        raise ConnectionError(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc


@asynccontextmanager
async def pooling() -> AsyncIterator[None]:
    """Pool this process's Postgres connections for the duration of the block.

    Entered once per process — by the front door's lifespan and by each worker's entrypoint —
    because a pool belongs to one event loop and one process. Everything below `connection()`
    then borrows instead of connecting, which is what removes the per-call handshake that was
    timing out under load. On exit every pool is closed so a shutdown does not leave sockets
    behind for the database to reap.
    """
    global _POOLING
    _POOLING = True
    try:
        yield
    finally:
        _POOLING = False
        pools = list(_POOLS.values())
        _POOLS.clear()
        for pool in pools:
            await pool.close()


def pool_stats() -> dict[str, int]:
    """Aggregate pool counters across this process's pools, for the metrics surface.

    Aggregated rather than per-DSN because the thing an operator alerts on is "is the front door
    waiting for connections?", which is a process-level question; naming each DSN would leak a
    host into a metric label for no operational gain.
    """
    total: dict[str, int] = {"pool_size": 0, "pool_available": 0, "requests_waiting": 0}
    for pool in _POOLS.values():
        stats = pool.get_stats()
        for name in total:
            total[name] += int(stats.get(name, 0))
    return total
