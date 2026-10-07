"""Shared Postgres connect helper and the per-process connection pool.

Every store connects through here, so an unreachable database is reported once, as "Postgres
unreachable at <host>: <cause>" with the password redacted. That failure is a `ConnectionError`
(transient, Temporal retries), never a `ChemclawError` (non-retryable bad data).

`connection()` is the call-site helper: it borrows from a per-process pool when the process entered
`pooling()` (front door lifespan, worker entrypoints) and otherwise opens a dedicated connection.
Pooling removes per-call handshakes that otherwise time out on a busy event loop. Pools are keyed by
`(event loop, dsn, libpq options, max_size)`. A borrowed connection carries
`pg_statement_timeout_seconds` by default; `connect()`, used by migrations, does not.

`existing_tables` and `apply_vector_recall_settings` take an open cursor because several subsystems
must ask the same question inside their own transaction.
"""

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

import psycopg
from psycopg import conninfo
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout
from pydantic import BeforeValidator

from chemclaw.core.config import pg_endpoint, settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import degraded, record_metric

logger = logging.getLogger(__name__)

# `operation` label for call sites that do not name themselves, so no borrow is unmeasured.
_UNNAMED_OPERATION = "unspecified"

# Pool key. Options carry the statement timeout, so differently bounded callers never share
# connections. The requested size is in the key so one caller's sizing never decides another's. The
# loop is in the key because `psycopg_pool` binds waiters to the loop that opened it, so a checkout
# from a second loop (e.g. `evals/retrieval._run_sync`) would wait out its timeout. The loop object,
# not `id(loop)`, since addresses are reused; `_forget_pools_of_ended_loops` drops pools of ended
# loops.
_PoolKey = tuple[asyncio.AbstractEventLoop, str, str | None, int | None]
_Pool = AsyncConnectionPool[psycopg.AsyncConnection[TupleRow]]
_POOLS: dict[_PoolKey, _Pool] = {}
# Guards every read and write of the two containers below. A threading lock, because pools of other
# loops are built from other threads while `/metrics` walks the dict. Never held across an `await`
# or the release of a pool's last reference: callers move pools to a local and drop them outside.
_POOL_REGISTRY_LOCK = threading.Lock()
# Pools this module did not build but this process holds (the LangGraph checkpointer's, in
# `agent/checkpointer.py`), counted in `chemclaw_pg_pool_max_size`. Their lifecycle stays with the
# owner, so they are separate from `_POOLS`.
_FOREIGN_POOLS: list[Any] = []
# Whether this process has entered `pooling()`. Off means `connection()` opens a dedicated
# connection per call — the pre-pool behavior, which is what a one-shot script or a test wants.
_POOLING = False


def _redact(dsn: str) -> str:
    """Return `dsn` with any password removed, so it is safe to echo in an error message.

    Round-trips through libpq's parser, covering URL userinfo, URL query and keyword forms. An
    unparseable DSN is replaced wholesale.
    """
    try:
        parts = conninfo.conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        # Counted, because the output `<postgres>` cannot otherwise distinguish a malformed DSN from
        # a redacted one. WARNING: the caller already reports the failure.
        degraded(
            logger,
            "db_dsn",
            "the configured DSN cannot be parsed by libpq; reporting it as <postgres>",
            level=logging.WARNING,
        )
        return "<postgres>"
    parts.pop("password", None)
    return conninfo.make_conninfo("", **parts)


# Never serve a pooled query from a generic plan. psycopg auto-prepares after five executions and
# Postgres may then switch to a generic plan; for the dense vector query
# (`retrieval/vector_index.py` at `vector(1536)`) and the `IS NULL OR` filters elsewhere, the
# generic plan is a sequential scan orders of magnitude slower. `auto` currently avoids it only by a
# cost estimate. `force_custom_plan` keeps the parse cached and re-plans with real parameters, for
# ~25 µs per query. The checkpointer pool is excluded: its OR sits behind
# `thread_id`/`checkpoint_ns` equalities, so its generic plan is an index scan.
_FORCE_CUSTOM_PLAN = "-c plan_cache_mode=force_custom_plan"


def _merged_options(dsn: str, statement_timeout_seconds: float | None) -> str:
    """Return the libpq `options` to connect with: the DSN's own, our plan mode, our timeout.

    Concatenated rather than passed as `options=`, which would override any `options` in the DSN
    (`search_path`, `application_name`, ...). libpq takes the last repeated `-c`, so ours win.
    """
    # libpq statement_timeout is in milliseconds; passed as a server option so it applies to every
    # statement on the connection without an extra round trip.
    ours = (
        f"{_FORCE_CUSTOM_PLAN} -c statement_timeout={int(statement_timeout_seconds * 1000)}"
        if statement_timeout_seconds
        else _FORCE_CUSTOM_PLAN
    )
    try:
        existing = conninfo.conninfo_to_dict(dsn).get("options")
    except psycopg.ProgrammingError:
        # This branch drops the DSN's own `options`, which the connect error would not show.
        degraded(
            logger,
            "db_dsn",
            "the configured DSN cannot be parsed by libpq; any `options` it carries are dropped "
            "and only our own plan mode and statement timeout are applied",
            level=logging.WARNING,
        )
        return ours
    return f"{existing} {ours}" if isinstance(existing, str) and existing else ours


class _DatabaseUnavailable(ConnectionError):
    """This module's own "there is no connection to hand you" — never a caller's socket error.

    A `ConnectionError` subclass, so existing retry classification is unaffected. Private and
    narrower than the builtin: `_failure_kind` keys on it, so a caller's own `ConnectionResetError`
    inside a `connection()` block is not booked as a Postgres outage.
    """


async def connect(
    dsn: str, *, statement_timeout_seconds: float | None = None
) -> psycopg.AsyncConnection[TupleRow]:
    """Open a *dedicated* Postgres connection, failing fast and clearly when unreachable.

    Uses libpq `connect_timeout`; failures become `ConnectionError` with the redacted DSN and cause.
    `statement_timeout_seconds` omitted (or 0/None) means unbounded, unlike `connection()`; the
    migration runner and grant applier rely on that. Prefer `connection()` on any request path.
    """
    options = _merged_options(dsn, statement_timeout_seconds)
    try:
        return await psycopg.AsyncConnection.connect(
            dsn, connect_timeout=settings.pg_connect_timeout_seconds, options=options
        )
    except psycopg.OperationalError as exc:
        raise _DatabaseUnavailable(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc


def _forget_pools_of_ended_loops() -> None:
    """Drop every pool whose event loop has ended, releasing the backends it was still holding.

    A pool opened on a short-lived loop (`evals/retrieval._run_sync`, `durable/eval_drift`) would
    otherwise hold its connections for the life of the process. It cannot be closed (`close()` on an
    ended loop raises), so it is dropped; releasing the last reference closes the connections.
    Called from the pool lookup and the readings, so neither counts a dead pool.
    """
    with _POOL_REGISTRY_LOCK:
        dead = [key for key in _POOLS if key[0].is_closed()]
        evicted = [_POOLS.pop(key) for key in dead]
    # The release happens here, outside the registry lock.
    evicted.clear()


def _pool_for(dsn: str, options: str | None, max_size: int | None) -> _Pool:
    """Return this process's pool for `(dsn, options, max_size)`, constructing it on first use.

    Lazy, since a process learns its DSNs by using them. Lookup and insert are one critical section
    under `_POOL_REGISTRY_LOCK` and precede any `await`, so racing first uses build one pool.
    `max_size` is this caller's ceiling (`None` = `pg_pool_max_size`). `min_size` is clamped under
    it, because psycopg's `ValueError` here would surface as an unclassified 500 on the request
    path.
    """
    _forget_pools_of_ended_loops()
    # Key on the effective width so `0`, `None` and an explicit default are one pool.
    size = max_size or settings.pg_pool_max_size
    key = (asyncio.get_running_loop(), dsn, options, size)
    with _POOL_REGISTRY_LOCK:
        pool = _POOLS.get(key)
        if pool is not None:
            return pool
        pool = AsyncConnectionPool(
            conninfo=dsn,
            connection_class=psycopg.AsyncConnection[TupleRow],
            kwargs={"connect_timeout": settings.pg_connect_timeout_seconds, "options": options},
            min_size=min(settings.pg_pool_min_size, size),
            max_size=size,
            max_idle=settings.pg_pool_max_idle_seconds,
            timeout=settings.pg_pool_timeout_seconds,
            # Check idle connections before keeping them, so one killed from outside (vendor idle
            # limit, NAT timeout, `idle_in_transaction_session_timeout`) is replaced instead of
            # handed to a caller.
            check=AsyncConnectionPool.check_connection,
            # Opened by the caller below: constructing with `open=True` schedules the background
            # workers from `__init__`, which psycopg_pool warns about outside a running loop.
            open=False,
        )
        _POOLS[key] = pool
        return pool


def _failure_kind(exc: BaseException) -> str | None:
    """Which of the four database failure classes `exc` is, or `None` if it is not one.

    Separated because the operator response differs (a cancelled statement is not an outage). Order
    matters: `QueryCanceled`, `DeadlockDetected` and `SerializationFailure` subclass
    `OperationalError`. `deadlock` includes serialization failures (same response: retry); the log
    carries the SQLSTATE. Non-database errors return `None`, and only `_DatabaseUnavailable` (not
    the builtin `ConnectionError`) counts as unavailable.
    """
    if isinstance(exc, _DatabaseUnavailable):
        return "unavailable"
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return "cancelled"
    if isinstance(exc, psycopg.errors.DeadlockDetected | psycopg.errors.SerializationFailure):
        return "deadlock"
    if isinstance(exc, psycopg.OperationalError):
        return "unavailable"
    if isinstance(exc, psycopg.Error):
        return "error"
    return None


def _record_failure(operation: str, dsn: str, exc: BaseException) -> None:
    """Count and name one database failure, once, at the seam every call site already goes through.

    The log line adds the `operation`, database and `sqlstate` the counter's kind cannot carry.
    """
    kind = _failure_kind(exc)
    if kind is None:
        return
    record_metric(lambda m: m.increment("chemclaw_db_query_failures_total", 1, {"kind": kind}))
    log_event(
        logger,
        "db.failed",
        "database operation %r failed (%s) at %s: %s",
        operation,
        kind,
        _redact(dsn),
        exc,
        level=logging.WARNING,
        operation=operation,
        kind=kind,
        sqlstate=getattr(exc, "sqlstate", "") or "",
    )


def _record_duration(operation: str, seconds: float) -> None:
    """Record how long one unit of work held a connection, and say so when it was slow.

    The unit is the whole `connection()` block, from before checkout to after the body: hold time,
    which is what pool pressure depends on, not query latency. A call site doing other work while
    holding a connection should name its `operation`. Pooled and dedicated branches are both timed.
    """
    record_metric(
        lambda m: m.observe("chemclaw_db_query_duration_seconds", seconds, {"operation": operation})
    )
    threshold = settings.pg_slow_query_seconds
    if threshold and seconds >= threshold:
        log_event(
            logger,
            "db.slow",
            # "spent", not "held": the span includes waiting for a connection that may never have
            # arrived.
            "database operation %r spent %.3fs waiting for and using a connection "
            "(threshold %.3fs)",
            operation,
            seconds,
            threshold,
            level=logging.WARNING,
            operation=operation,
            duration_s=round(seconds, 3),
        )


@asynccontextmanager
async def connection(
    dsn: str,
    *,
    statement_timeout_seconds: float | None = None,
    operation: str = _UNNAMED_OPERATION,
    pool_max_size: int | None = None,
) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
    """Borrow a connection for the duration of the block — pooled when this process pools.

    Commits on exit (rolls back on error) and returns the connection to the pool; without
    `pooling()` it is a dedicated connect. Pool exhaustion within `pg_pool_timeout_seconds` raises
    the same `ConnectionError` as an unreachable database, so Temporal retries both.

    `statement_timeout_seconds` defaults to `pg_statement_timeout_seconds` (read per call); pass a
    number to bound differently or `0` to opt out, though an unbounded connection belongs in
    `connect()`. `operation` labels `chemclaw_db_query_duration_seconds` and the slow/failure logs:
    a low-cardinality literal, never request-derived. `pool_max_size` sizes this caller's own pool
    when its concurrency is known and smaller (e.g. the single-flighted `/readyz`); it does nothing
    without `pooling()`. Every borrow is timed and every database failure classified on
    `chemclaw_db_query_failures_total{kind}`.
    """
    if statement_timeout_seconds is None:
        statement_timeout_seconds = settings.pg_statement_timeout_seconds
    options = _merged_options(dsn, statement_timeout_seconds)
    started = time.perf_counter()
    try:
        if not _POOLING:
            conn = await connect(dsn, statement_timeout_seconds=statement_timeout_seconds)
            async with conn:
                yield conn
            return
        pool = _pool_for(dsn, options, pool_max_size)
        await pool.open()  # idempotent; the first caller starts the pool's background workers
        try:
            async with pool.connection() as conn:
                # One round trip on the first borrow against an endpoint, so the fleet gauge can
                # tell one server spelled two ways from two servers; never during a scrape.
                await _learn_server_identity(conn, dsn)
                yield conn
        except (PoolTimeout, PoolClosed) as exc:
            # Both are `psycopg.OperationalError` subclasses raised only by the checkout itself, so
            # catching them here cannot swallow an error from the caller's block.
            raise _DatabaseUnavailable(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc
    except Exception as exc:
        # `Exception`, not `BaseException`: cancellation is not a database failure. Re-raised
        # untouched.
        _record_failure(operation, dsn, exc)
        raise
    finally:
        _record_duration(operation, time.perf_counter() - started)


def bind_pool_metrics() -> None:
    """Expose this process's pool gauges, so pool saturation is visible wherever a pool exists.

    Bound by `pooling()` so every pooled process (workers and connector servers, not just the front
    door) reports `requests_waiting`. `chemclaw_pg_pool_max_size` sums every pool the process holds,
    not `settings.pg_pool_max_size`: a process routinely holds several, and summed across pods it is
    compared with `chemclaw_pg_fleet_max_connections` to catch a fleet scaled past its ceiling.

    Imports `core/metrics.py` lazily, per the `core` sibling-import rule (`tests/test_layering.py`).
    """
    from chemclaw.core.metrics import METRICS

    # `coherent_pool_stats`, so the three gauges of one scrape describe the same instant.
    METRICS.bind_gauge("chemclaw_pg_pool_size", lambda: float(coherent_pool_stats()["pool_size"]))
    METRICS.bind_gauge(
        "chemclaw_pg_pool_available", lambda: float(coherent_pool_stats()["pool_available"])
    )
    METRICS.bind_gauge(
        "chemclaw_pg_pool_requests_waiting",
        lambda: float(coherent_pool_stats()["requests_waiting"]),
    )
    METRICS.bind_gauge("chemclaw_pg_pool_max_size", lambda: float(_process_max_connections()))
    METRICS.bind_gauge(
        "chemclaw_pg_fleet_max_connections", lambda: float(settings.pg_fleet_max_connections)
    )
    METRICS.bind_gauge(
        "chemclaw_pg_session_fleet_max_connections",
        lambda: float(settings.pg_session_fleet_max_connections),
    )
    METRICS.bind_gauge(
        "chemclaw_pg_session_pool_max_size", lambda: float(_session_store_max_connections())
    )


@asynccontextmanager
async def pooling() -> AsyncIterator[None]:
    """Pool this process's Postgres connections for the duration of the block.

    Entered once per process (front door lifespan, worker entrypoints), since a pool belongs to one
    loop. Binds the pool gauges on entry and closes this loop's pools on exit.
    """
    global _POOLING
    _POOLING = True
    bind_pool_metrics()
    try:
        yield
    finally:
        _POOLING = False
        reset_pool_snapshot()
        # Only this loop's pools: closing one opened on another loop raises inside the close. Pools
        # of other loops are released by dropping the reference (`_forget_pools_of_ended_loops`).
        await close_pools_of_this_loop()
        with _POOL_REGISTRY_LOCK:
            abandoned = list(_POOLS.values())
            _POOLS.clear()
        # The drop happens outside the lock, for the reason `_forget_pools_of_ended_loops` gives: a
        # registry the request path reads should not be held across a refcount drop.
        abandoned.clear()


async def close_pools_of_this_loop() -> None:
    """Close and forget every pool the *running* loop opened — before that loop ends.

    An abandoned pool can hang `asyncio.run`'s shutdown: `_cancel_all_tasks` awaits psycopg's
    background workers, and one mid-reconnect may never return. This is the live-loop counterpart of
    `_forget_pools_of_ended_loops`, which can only drop pools afterwards. Safe when the loop opened
    nothing. Called by `pooling()` and by anyone running its own loop inside a pooled process.
    """
    here = asyncio.get_running_loop()
    with _POOL_REGISTRY_LOCK:
        mine = [key for key in _POOLS if key[0] is here]
        pools = [_POOLS.pop(key) for key in mine]
    # Outside the lock, because `close()` awaits and the registry is read from the request path.
    for pool in pools:
        await pool.close()


def register_pool(pool: Any) -> None:
    """Count a pool this module did not build in this process's readings.

    For the LangGraph checkpointer's autocommit pool (`agent/checkpointer.py`). Registration does
    not surface checkpointer contention, which queues on the saver's own lock before the pool:
    `chemclaw_checkpointer_statements_waiting` does. The caller keeps the lifecycle.
    """
    with _POOL_REGISTRY_LOCK:
        if pool not in _FOREIGN_POOLS:
            _FOREIGN_POOLS.append(pool)


def unregister_pool(pool: Any) -> None:
    """Stop counting a foreign pool — called by its owner as it closes it."""
    with _POOL_REGISTRY_LOCK:
        if pool in _FOREIGN_POOLS:
            _FOREIGN_POOLS.remove(pool)


# Dedicated connections registered by `register_connection`, as `(connection, conninfo)`:
# `AsyncConnection` is unhashable and does not keep a readable conninfo.
_HELD_CONNECTIONS: list[tuple[Any, str]] = []


def register_connection(conn: Any, conninfo: str) -> None:
    """Count one *dedicated* connection a caller holds open, for as long as it holds it.

    For connections that occupy a backend outside any pool (e.g. `publish/drivers/postgres.py`).
    Counted only on `postgres_dsn`'s server, since that is the budget `pg_fleet_max_connections`
    bounds; a sink on another server is the operator's to size. The caller keeps the lifecycle; a
    closed connection stops counting, but call `unregister_connection` on a deliberate close.

    Args:
        conn: The open connection. Counted while `conn.closed` is false.
        conninfo: The connection string it was dialled with, so the endpoint can be compared.
            Not read off the connection: psycopg keeps no such attribute.
    """
    with _POOL_REGISTRY_LOCK:
        if all(held is not conn for held, _ in _HELD_CONNECTIONS):
            _HELD_CONNECTIONS.append((conn, conninfo))


def unregister_connection(conn: Any) -> None:
    """Stop counting a dedicated connection — called when its holder closes it."""
    with _POOL_REGISTRY_LOCK:
        _HELD_CONNECTIONS[:] = [entry for entry in _HELD_CONNECTIONS if entry[0] is not conn]


#: `pg_endpoint(dsn) -> system_identifier` for every endpoint a borrow has reached. String
#: comparison reads one server spelled two ways (`localhost`/`127.0.0.1`) as two; the identifier is
#: fixed at `initdb` and readable by an unprivileged role. Learned once and cached, so it survives
#: an outage; before the first borrow the string comparison is used.
_SERVER_IDENTITY: dict[tuple[str, str], int] = {}

#: Endpoints whose identity could not be read; tried once, warned once, then string comparison.
_IDENTITY_UNREADABLE: set[tuple[str, str]] = set()


async def _learn_server_identity(conn: Any, dsn: str) -> None:
    """Read one endpoint's `system_identifier`, at most once per process, never during a scrape.

    Called after a successful borrow in `connection()`. Never raises: an unreadable endpoint is
    recorded and the string comparison stays.
    """
    endpoint = pg_endpoint(dsn)
    if endpoint is None or endpoint in _SERVER_IDENTITY or endpoint in _IDENTITY_UNREADABLE:
        return
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT system_identifier FROM pg_control_system()")
            row = await cur.fetchone()
        if row is None:
            raise ValueError("pg_control_system() returned no row")
        _SERVER_IDENTITY[endpoint] = int(row[0])
    except Exception as exc:
        _IDENTITY_UNREADABLE.add(endpoint)
        logger.warning(
            "postgres.identity_unreadable",
            extra={
                "endpoint": f"{endpoint[0]}:{endpoint[1]}",
                "error": str(exc),
                "consequence": (
                    "two DSNs naming this server can no longer be recognised as one, so a split "
                    "deployment is charged to two connection ceilings"
                ),
            },
        )


def same_server(one: str, other: str) -> bool:
    """Whether two DSNs name one Postgres server, measured where a borrow has already answered.

    Falls back to `pg_endpoint`'s string comparison when either side is unmeasured; `None` endpoints
    are treated as one server, so pools are summed against one ceiling.
    """
    here, there = pg_endpoint(one), pg_endpoint(other)
    if here is not None and there is not None:
        measured_here = _SERVER_IDENTITY.get(here)
        measured_there = _SERVER_IDENTITY.get(there)
        if measured_here is not None and measured_there is not None:
            return measured_here == measured_there
    return here == there


def _live_held_connections() -> list[tuple[Any, str]]:
    """Every registered connection this process still holds, dropping the closed ones as it goes."""
    with _POOL_REGISTRY_LOCK:
        live = [(conn, info) for conn, info in _HELD_CONNECTIONS if not conn.closed]
        _HELD_CONNECTIONS[:] = live
    return live


def _held_connections_on(endpoint: tuple[str, str] | None) -> int:
    """How many live registered connections this process holds on one *endpoint*."""
    return sum(1 for _, info in _live_held_connections() if pg_endpoint(info) == endpoint)


def _held_connections_on_server(dsn: str) -> int:
    """How many live registered connections this process holds on the *server* `dsn` names."""
    return sum(1 for _, info in _live_held_connections() if same_server(info, dsn))


def _all_pools() -> list[Any]:
    """Every pool this process holds: the ones built here, plus the registered foreign ones."""
    _forget_pools_of_ended_loops()
    with _POOL_REGISTRY_LOCK:
        return [*_POOLS.values(), *_FOREIGN_POOLS]


def _process_max_connections() -> int:
    """How many Postgres connections this process may open — the sum over every pool it holds.

    Plus registered dedicated connections on `postgres_dsn`'s server (see `register_connection`).
    """
    return sum(int(pool.max_size) for pool in _all_pools()) + _held_connections_on(
        pg_endpoint(settings.postgres_dsn)
    )


def _session_store_max_connections() -> int:
    """The part of `_process_max_connections()` that lands on a split session store's own server.

    Zero unless `session_store_dsn` names a different server, as decided by
    `Settings.fleet_connections_per_server` and refined by `same_server`. Subtracted from the
    process total so each server can be checked against its own ceiling; configuration-derived, so
    the gauge keeps its series through a database outage.
    """
    if not settings.fleet_connections_per_server()[1]:
        return 0
    there = settings.session_store_dsn
    # A split the measurement disproves is not a split: one server, one sum, one ceiling.
    if same_server(settings.postgres_dsn, there):
        return 0
    return sum(
        int(pool.max_size) for pool in _all_pools() if same_server(str(pool.conninfo), there)
    ) + _held_connections_on_server(there)


def pool_stats() -> dict[str, int]:
    """Aggregate pool counters across this process's pools, for the metrics surface.

    Process-level, because the alert question is whether this process waits for connections; per-DSN
    labels would leak hosts. Includes registered foreign pools.
    """
    total: dict[str, int] = {"pool_size": 0, "pool_available": 0, "requests_waiting": 0}
    for pool in _all_pools():
        stats = pool.get_stats()
        for name in total:
            total[name] += int(stats.get(name, 0))
    return total


#: How long one walk of the pools serves later gauge reads, in seconds: a coherence window so the
#: three pool gauges of one render describe one instant, far shorter than a scrape interval.
_POOL_SNAPSHOT_WINDOW_SECONDS = 1.0

#: `(taken_at, stats)` of the latest walk, or `None`; guarded by `_POOL_SNAPSHOT_LOCK` so concurrent
#: renders never see a half-built snapshot.
_POOL_SNAPSHOT: tuple[float, dict[str, int]] | None = None
_POOL_SNAPSHOT_LOCK = threading.Lock()


def coherent_pool_stats() -> dict[str, int]:
    """`pool_stats()`, but one walk per scrape rather than one per gauge.

    Returns:
        A copy, so a caller cannot mutate the shared snapshot for the gauges that follow it.
    """
    global _POOL_SNAPSHOT
    now = time.monotonic()
    with _POOL_SNAPSHOT_LOCK:
        cached = _POOL_SNAPSHOT
        if cached is not None and now - cached[0] < _POOL_SNAPSHOT_WINDOW_SECONDS:
            return dict(cached[1])
    # Walked outside the lock so concurrent scrapes do not serialise on it; a race costs one extra
    # walk.
    fresh = pool_stats()
    with _POOL_SNAPSHOT_LOCK:
        _POOL_SNAPSHOT = (now, fresh)
    return dict(fresh)


def reset_pool_snapshot() -> None:
    """Drop the cached walk, so the next read takes a fresh one.

    For tests, and for `pooling()`'s exit so closed pools are not reported.
    """
    global _POOL_SNAPSHOT
    with _POOL_SNAPSHOT_LOCK:
        _POOL_SNAPSHOT = None


def vector_recall_settings() -> dict[str, str]:
    """The pgvector recall parameters the configuration asks a dense query to run under.

    Empty by default, meaning no statement is issued, so pgvector's defaults stand and older servers
    without `hnsw.iterative_scan` are never sent it. See `core/config/retrieval.py`.
    """
    wanted: dict[str, str] = {}
    if settings.hnsw_ef_search:
        wanted["hnsw.ef_search"] = str(settings.hnsw_ef_search)
    if settings.hnsw_iterative_scan != "off":
        wanted["hnsw.iterative_scan"] = settings.hnsw_iterative_scan
    return wanted


async def apply_vector_recall_settings(cur: Any) -> None:
    """Put the configured pgvector recall parameters on this cursor's transaction, if any are set.

    `set_config(name, value, is_local => true)` because `SET` takes no placeholders; one `unnest`
    applies all of them in one round trip, and nothing is sent when none are set. Transaction-local
    is required: pooled connections are reused, so a session setting would leak onto later
    borrowers. Both dense searches (note index and document index) call this.

    Args:
        cur: An open async cursor. Taken rather than opened here so the settings join the
            transaction the search itself runs in — applying them on another connection would
            parametrize a transaction nobody is searching in.
    """
    wanted = vector_recall_settings()
    if not wanted:
        return
    await cur.execute(
        "SELECT set_config(name, value, true) "
        "FROM unnest(%(names)s::text[], %(values)s::text[]) AS parameter(name, value)",
        {"names": list(wanted), "values": list(wanted.values())},
    )


async def existing_tables(cur: Any, tables: Iterable[str]) -> set[str]:
    """Which of `tables` exist on this connection's `search_path`.

    A guard inside the statement cannot work: `DELETE FROM t` resolves `t` at parse time. Shared
    because the LangGraph checkpoint tables exist only after `AsyncPostgresSaver.setup()`, and both
    erasure (`agent/leaver.py`) and retention (`durable/retention.py`) must work without them.

    Args:
        cur: An open async cursor. Taken rather than opened here so the check joins whatever
            transaction the caller is already in — asking on a separate connection would answer
            about a different snapshot.
        tables: The table names to ask about.

    Returns:
        The subset that exists.
    """
    names = sorted(set(tables))
    await cur.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND c.relname = ANY(%s) "
        "AND n.nspname = ANY(current_schemas(true))",
        (names,),
    )
    return {str(row[0]) for row in await cur.fetchall()}


def _iso_stamp(value: Any) -> Any:
    """A `TIMESTAMPTZ` column as `datetime.isoformat()`'s string, NULL as `""`, else untouched.

    A validator rather than a SQL `::text` cast, which would spell the instant differently from what
    every reader has been given. Other values are left for pydantic.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else value


#: A `TIMESTAMPTZ` column as the ISO string its readers expect; NULL reads as "" (still waiting,
#: never settled), not `None`.
IsoStamp = Annotated[str, BeforeValidator(_iso_stamp)]
