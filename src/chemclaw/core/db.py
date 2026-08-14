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

**A borrowed connection is bounded by default.** `connection()` applies
`pg_statement_timeout_seconds` when the caller says nothing, and `connect()` — the dedicated,
unpooled one the migration runner uses — does not. The asymmetry is the point: a connection you
own may run an index build for an hour, and one borrowed from a pool the request path shares may
not (D-2026-08-08-a-borrowed-connection-is-bounded-by-default).

Two helpers below take an open cursor rather than opening one, and both are here for the same
reason: two subsystems in two packages have to ask the identical question of the identical
transaction. `existing_tables` is one; `apply_vector_recall_settings` — the pgvector recall
parameters a dense search runs under — is the other.
"""

import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg import conninfo
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout

from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# One pool per (dsn, merged libpq options). The options string carries the statement timeout, so
# keying on it keeps the `/readyz` probe's 2s-bounded connection out of the stores' 30s-bounded
# pool. (It once said "a migration's untimed connection", which was never a pooled connection at
# all: `migrate` uses `connect`, not `connection`, and never enters `pooling()`.)
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
    migration runner and the grant applier do exactly that, since an index build may
    legitimately run long. **This is the one place where omitting the argument means
    "unbounded"**; `connection()` defaults it instead, because a connection borrowed from a
    shared pool must never be held open by a single runaway query
    (D-2026-08-08-a-borrowed-connection-is-bounded-by-default).

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

    **The statement timeout defaults.** Omitting `statement_timeout_seconds` (or passing `None`)
    applies `pg_statement_timeout_seconds`, resolved per call so a monkeypatched or reloaded
    setting is honoured. It used to mean *no bound*, which made the bound a convention thirty call
    sites in twenty-two modules happened to keep by writing it out; a thirty-first that forgot
    would hold a pooled connection for as long as one bad query ran, and nothing would say so.
    Pass a number to bound a call site differently (`/readyz` bounds its `SELECT 1` at two
    seconds), or `0` to opt out — but a connection borrowed from a shared pool wanting no bound at
    all is a dedicated connection, which is what `connect()` is for.
    """
    if statement_timeout_seconds is None:
        statement_timeout_seconds = settings.pg_statement_timeout_seconds
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


def bind_pool_metrics() -> None:
    """Expose this process's pool gauges, so pool saturation is visible wherever a pool exists.

    Called by `pooling()` rather than by any one process's startup code, which is the whole point:
    all three of these gauges used to be bound in the front door's `create_app`, so the eleven of
    the shipped chart's seventeen pooled processes that are *not* the front door — every Temporal
    worker, every connector server — served a `/metrics` surface with no pool reading on it at all.
    `requests_waiting` is the signal D-119 introduced to make "the pool is too small" legible, and
    it was absent from exactly the processes that do the long database work (the retention sweep,
    the reindex, the chain verification). Binding it where the pool is opened means a process
    cannot acquire a pool without also acquiring its witness — the same argument
    D-2026-08-01-every-process-carries-its-own-witness made for probes, which left this behind.

    `chemclaw_pg_pool_max_size` is the per-process half of the fleet connection budget: `sum()` of
    it across pods is what the deployment may open, and comparing that to
    `chemclaw_pg_fleet_max_connections` is the only way to see a fleet scaled past its ceiling by
    hand, since `Settings` validates the shape the chart rendered and never re-runs
    (D-2026-08-05-the-connection-budget-is-a-fleet-number).

    Imported inside the function: `core/metrics.py` is a sibling of this module and `core` keeps
    its no-module-scope-sibling-import rule (`tests/test_layering.py`), the same lazy exception
    `core/logging.py` declares.
    """
    from chemclaw.core.metrics import METRICS

    METRICS.bind_gauge("chemclaw_pg_pool_size", lambda: float(pool_stats()["pool_size"]))
    METRICS.bind_gauge("chemclaw_pg_pool_available", lambda: float(pool_stats()["pool_available"]))
    METRICS.bind_gauge(
        "chemclaw_pg_pool_requests_waiting", lambda: float(pool_stats()["requests_waiting"])
    )
    METRICS.bind_gauge("chemclaw_pg_pool_max_size", lambda: float(settings.pg_pool_max_size))
    METRICS.bind_gauge(
        "chemclaw_pg_fleet_max_connections", lambda: float(settings.pg_fleet_max_connections)
    )


@asynccontextmanager
async def pooling() -> AsyncIterator[None]:
    """Pool this process's Postgres connections for the duration of the block.

    Entered once per process — by the front door's lifespan and by each worker's entrypoint —
    because a pool belongs to one event loop and one process. Everything below `connection()`
    then borrows instead of connecting, which is what removes the per-call handshake that was
    timing out under load. On exit every pool is closed so a shutdown does not leave sockets
    behind for the database to reap.

    Binds the pool gauges on the way in, so every process that pools also reports on its pool.
    """
    global _POOLING
    _POOLING = True
    bind_pool_metrics()
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


def vector_recall_settings() -> dict[str, str]:
    """The pgvector recall parameters the configuration asks a dense query to run under.

    Empty is the default and means "issue no statement": pgvector's own `ef_search` (40) and
    `iterative_scan` (`off`) stand, the dense path costs exactly the round trips it did before this
    existed, and a server without `hnsw.iterative_scan` (pgvector < 0.8, where the reserved `hnsw.`
    prefix makes an unknown parameter an error rather than an ignored placeholder) is never handed
    one. See `core/config/retrieval.py` for why neither knob is the first thing to reach for — the
    measured cause of the large `within=` shortfalls was stale planner statistics, and these address
    only the residual.
    """
    wanted: dict[str, str] = {}
    if settings.hnsw_ef_search:
        wanted["hnsw.ef_search"] = str(settings.hnsw_ef_search)
    if settings.hnsw_iterative_scan != "off":
        wanted["hnsw.iterative_scan"] = settings.hnsw_iterative_scan
    return wanted


async def apply_vector_recall_settings(cur: Any) -> None:
    """Put the configured pgvector recall parameters on this cursor's transaction, if any are set.

    `set_config(name, value, is_local => true)` rather than `SET LOCAL` because the values come from
    configuration: `SET` accepts no placeholders, so the alternative is interpolating an
    operator-supplied value into statement text. One `unnest` over two arrays applies however many
    are set in a single round trip, and nothing is sent at all when none are — which is the default,
    so a dense path costs exactly what it did before this existed.

    **Transaction-local is the load-bearing half, not an implementation detail.** `connection()`
    commits on exit and pooled connections are reused, so a session-level `SET` here would leak one
    query's widened candidate list onto every later borrower of that connection — including the
    unscoped searches that never wanted it. `is_local => true` makes the setting die with the
    transaction that asked for it.

    **Here rather than on one index, because the shape these knobs govern is on both.** They were
    introduced for the note index (`chemclaw.retrieval.vector_index`) and cited a residual on the
    *document* one (`chemclaw.ingest.documents.index`), which read them nowhere — so the knob did
    nothing for the case named as its reason. Measured on the document index, live PostgreSQL 16 /
    pgvector 0.8.0, 20,000 chunks with one file row each, `ANALYZE`d: the plan really is a
    `Nested Loop Semi Join` over an `Index Scan using document_chunks_embedding_idx`, i.e. the
    eligibility `EXISTS` sits *above* the HNSW scan — the shape in which `ef_search` decides how
    many candidates survive the filter — for an unfiltered query and for tags matching 100%, 50%,
    20% and 10% of the corpus; at 5% and below the planner abandons the vector index for an exact
    plan.

    **The shortfall itself did not reproduce, and that is worth saying plainly.** 20 queries × 6
    selectivities × 4 settings of the two knobs: **0 of 480 searches returned fewer than `top_k`**,
    and the HNSW scan handed the semi join 62 rows where `ef_search=40` would suggest 40. So this is
    applied because the plan permits the shortfall and the knob must be able to reach the plan, not
    because this corpus exhibits one — the same conclusion `PostgresNoteIndex.__init__` reached when
    it re-measured its own.

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

    One query rather than a guard inside each statement, because a guard inside the statement
    cannot work: `DELETE FROM t` resolves `t` when the statement is *parsed*, long before any
    `WHERE` runs. Measured against a schema with no checkpointer — a `WHERE to_regclass(...) IS NOT
    NULL` guard never got evaluated and the whole erasure failed with `relation "checkpoints" does
    not exist`.

    Here rather than private to one caller because two subsystems ask the same question about the
    same tables, and for the same reason: the LangGraph checkpoint tables are created by
    `AsyncPostgresSaver.setup()` rather than by a migration in `infra/sql`, so a deployment that has
    never run the graph engine does not have them. Erasure must not become the one operation such a
    deployment cannot perform (`agent/leaver.py`), and neither must the nightly retention sweep
    (`durable/retention.py`) — a sweep that fails outright on a missing table stops pruning every
    other table too.

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
