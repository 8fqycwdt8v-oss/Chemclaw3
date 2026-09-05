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

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg import conninfo
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout

from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import degraded, record_metric

logger = logging.getLogger(__name__)

# What `operation` says when a call site does not name itself. Every borrowed connection is
# measured, so the alternative to a default is an unlabelled hole in the distribution exactly where
# an unaudited call site is — which is the one place a slow query is most likely to hide.
_UNNAMED_OPERATION = "unspecified"

# One pool per (event loop, dsn, merged libpq options). The options string carries the statement
# timeout, so keying on it keeps the `/readyz` probe's 2s-bounded connection out of the stores'
# 30s-bounded pool. (It once said "a migration's untimed connection", which was never a pooled
# connection at all: `migrate` uses `connect`, not `connection`, and never enters `pooling()`.)
#
# **The loop is part of the key, and that is not defensive.** `psycopg_pool` binds its waiter
# futures and its background workers to the loop the pool was opened in, so a checkout issued from
# a *second* loop in the same process is queued on a hand-off callback that will never run on it.
# Measured with `max_size=1` and an 8 s pool timeout, the holder releasing at t=1.00 s: the
# cross-loop waiter was served after **8.01 s** — its own timeout expiring — against 1.00 s for the
# identical hand-off on the pool's own loop. A second loop is not hypothetical here:
# `evals/retrieval._run_sync` starts one deliberately when a metric is invoked from a coroutine.
# `agent/checkpointer.close_checkpointer` identifies the same hazard for its own pool; this is the
# equivalent for these.
#
# **The loop object, never `id(loop)`.** CPython reuses an address as soon as the object at it is
# freed, and an ended loop is freed: six `new_event_loop()`/`close()` cycles produced four distinct
# ids. Keying on the address therefore reintroduces exactly the hand-off hazard the loop was added
# to the key to prevent — a live loop handed a pool built for a dead one — with no way to notice.
# The object is its own identity, and `_forget_pools_of_ended_loops` is what keeps holding a
# reference to it from being a leak of its own.
_PoolKey = tuple[asyncio.AbstractEventLoop, str, str | None]
_Pool = AsyncConnectionPool[psycopg.AsyncConnection[TupleRow]]
_POOLS: dict[_PoolKey, _Pool] = {}
# Pools this module did not build but this process holds: today the LangGraph checkpointer's
# autocommit pool (`agent/checkpointer.py`), which every turn's state write goes through. They are
# registered rather than taught to the metrics separately, because `chemclaw_pg_pool_max_size` is
# the per-process half of the fleet connection budget and a pool nobody counts is a pool the
# deployment opens and the dashboard does not show. Their *lifecycle* stays with their owner —
# `pooling()` closes only what it built — which is why this is a separate list and not `_POOLS`.
_FOREIGN_POOLS: list[Any] = []
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
        # Counted, because this is the one branch whose whole output is `<postgres>`: an operator
        # reading "Postgres unreachable at <postgres>" cannot tell a redacted DSN from an
        # unparseable one, and the second means the configured DSN is malformed rather than the
        # server being down. WARNING rather than ERROR — the caller is already reporting a failure
        # and this only says the address in that report is uninformative.
        degraded(
            logger,
            "db_dsn",
            "the configured DSN cannot be parsed by libpq; reporting it as <postgres>",
            level=logging.WARNING,
        )
        return "<postgres>"
    parts.pop("password", None)
    return conninfo.make_conninfo("", **parts)


# **Never let this pool serve a query from a generic plan.** psycopg auto-prepares a statement on
# its fifth execution, and a pooled connection lives long enough to reach that on the first
# minute of traffic. Postgres may then switch a prepared statement to a *generic* plan — one
# planned once with the parameters unknown — and for the shape this system's retrieval is built
# on that plan is a sequential scan, because an `ORDER BY embedding <=> $1` cannot use an HNSW
# index when `$1` is not yet a value. Measured on 100k chunks, the dense note query went
# **9 ms → 1,280 ms on execution 11 and stayed there for the life of that connection**, and
# `EXPLAIN (GENERIC_PLAN)` names the reason: `Seq Scan on note_index` under a `Sort`. Two other
# statements have the same shape today (the scoped lexical `= ANY($1)` and fingerprint
# similarity), and any future `ORDER BY <parameterised distance>` would join them silently.
#
# The remedy is the server's own, and it is set here rather than per statement so that a query
# added next year inherits it: `force_custom_plan` keeps the prepared statement — the parse is
# still cached — and re-plans each execution with the parameters in hand. **Measured cost of that
# on the queries which do not need it**: a point lookup goes 135.7 µs → 140.5 µs, about 10 µs,
# against removing a 142x cliff. The checkpointer pool (`agent/checkpointer.py`) deliberately does
# not get this: its statements are primary-key lookups where a generic plan is both correct and
# the cheaper one.
_FORCE_CUSTOM_PLAN = "-c plan_cache_mode=force_custom_plan"


def _merged_options(dsn: str, statement_timeout_seconds: float | None) -> str:
    """Return the libpq `options` to connect with: the DSN's own, our plan mode, our timeout.

    psycopg merges a keyword argument *over* the connection string, so passing `options=` would
    silently discard any `options` the DSN already carries — and only on the connections that ask
    for a statement timeout, since `None` is dropped rather than merged. An operator who sets
    `options` in their DSN (a `search_path` for a shared database, `application_name`, `work_mem`)
    would lose it non-deterministically depending on the call site. Concatenating instead keeps
    both; libpq reads the last occurrence of a repeated `-c` setting, so our timeout still wins if
    the DSN happens to set one too.

    This never returns `None` any more, because `_FORCE_CUSTOM_PLAN` above is ours to add on every
    connection whether or not a statement timeout was asked for. That widens the pool key
    (`_pool_for` keys on the merged options) by a constant, which merges and splits nothing: every
    pool gains the same suffix.
    """
    # libpq statement_timeout is in milliseconds; passed as a server option so it applies to
    # every statement on the connection without an extra round trip.
    ours = (
        f"{_FORCE_CUSTOM_PLAN} -c statement_timeout={int(statement_timeout_seconds * 1000)}"
        if statement_timeout_seconds
        else _FORCE_CUSTOM_PLAN
    )
    try:
        existing = conninfo.conninfo_to_dict(dsn).get("options")
    except psycopg.ProgrammingError:
        # The connect below will fail and say so, but *this* branch silently drops whatever
        # `options` the DSN carried — a `search_path`, an `application_name`, a `work_mem` — and
        # that loss is invisible in the connect's own error. Said once, here, where it happens.
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

    A `ConnectionError` subclass, so every caller that already tests `ConnectionError` (Temporal's
    retry classification, `api/runner.py`, `science/bo/campaign_record.py`) is unaffected: the
    published contract is still "an unreachable or saturated database raises `ConnectionError`".

    **Private because it exists to be narrower than the builtin, not wider.** `_failure_kind` used
    to classify `isinstance(exc, ConnectionError)` as `kind="unavailable"`, and `ConnectionError`
    is the *builtin* base of `ConnectionResetError`, `BrokenPipeError`, `ConnectionAbortedError`
    and `ConnectionRefusedError` — every one of which a caller's own HTTP client, MCP session or
    socket can raise from inside the `connection()` block. Measured: raising
    `ConnectionResetError("my HTTP client died")` inside the block booked
    `chemclaw_db_query_failures_total{kind="unavailable"}` and logged a `db.failed` WARNING naming
    the database, for a fault that had nothing to do with Postgres — the exact thing
    `_failure_kind`'s own docstring says it does not do ("a `ValueError` from it is not a fact
    about Postgres"). Raised by both sites in this module that mean it, so nothing that *is* a
    real outage stopped being counted.
    """


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
        raise _DatabaseUnavailable(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc


def _forget_pools_of_ended_loops() -> None:
    """Drop every pool whose event loop has ended, releasing the backends it was still holding.

    Nothing used to do this, and the omission was not a slow leak but a permanent one: only
    `pooling()`'s `finally` removed a `_POOLS` entry, and it runs once, at process shutdown. So
    inside a pooled process every `asyncio.run` on a fresh loop built a pool, opened it to
    `pg_pool_min_size` connections, and abandoned it for the life of the process — measured against
    a live server as one extra `pg_stat_activity` row per short-lived loop, forever. That path is
    ordinary rather than exotic: `evals/retrieval._run_sync` starts a loop per live metric call, and
    `durable/eval_drift` runs `run_eval` in a thread *inside* the background worker's `pooling()`.

    **Dropped, not closed, and that is the only thing available.** `psycopg_pool` schedules a
    pool's shutdown on the loop it was opened in, so `close()` on an ended loop raises
    `RuntimeError: Event loop is closed` — the reason `pooling()`'s `finally` skips them too.
    Releasing the last reference is what shuts the connections: measured, the marked backend
    disappeared from `pg_stat_activity` on the `pop` below, with no `gc.collect()` needed.

    Called from the two places that ask this module what pools exist — the lookup and the readings
    — so whichever runs first releases, and neither `chemclaw_pg_pool_max_size` (the per-process
    half of the `pg_fleet_max_connections` budget) nor `pool_stats`' `pool_available` counts a pool
    nothing can borrow from. A read that evicts is deliberate: the resource is tied to an owner
    that no longer exists, and there is no other moment at which anyone learns it has gone.
    """
    for key in [key for key in _POOLS if key[0].is_closed()]:
        _POOLS.pop(key, None)


def _pool_for(dsn: str, options: str | None) -> _Pool:
    """Return this process's pool for `(dsn, options)`, constructing it on first use.

    Constructed lazily rather than up front because a process does not know which DSNs it will
    touch until it touches them (the session store, the calculation store and the fingerprint
    store may be three different databases or one). Construction is synchronous and the
    dictionary insert happens before any `await`, so two coroutines racing on the first use of a
    DSN cannot end up with two pools for it.

    Keyed on the running loop as well, so a second loop builds and owns its own pool instead of
    borrowing one whose waiters it cannot be woken by — see `_POOLS`.
    """
    _forget_pools_of_ended_loops()
    key = (asyncio.get_running_loop(), dsn, options)
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
            # `max_idle` only governs when the pool itself decides a connection has sat unused long
            # enough to close — it does nothing about one that was already killed out from under the
            # pool by something the pool cannot see: a managed-Postgres vendor's idle limit, a
            # stateful load balancer's NAT timeout, `idle_in_transaction_session_timeout`. Without
            # this, the first query on such a connection is handed straight to a caller and fails
            # with a raw connection-reset error instead of the pool quietly replacing it before
            # anyone borrows it. `check_connection` runs `SELECT 1` on a connection the background
            # health-check loop is about to keep, so a dead one is caught and swapped there instead.
            check=AsyncConnectionPool.check_connection,
            # Opened by the caller below: constructing with `open=True` schedules the background
            # workers from `__init__`, which psycopg_pool warns about outside a running loop.
            open=False,
        )
        _POOLS[key] = pool
    return pool


def _failure_kind(exc: BaseException) -> str | None:
    """Which of the four database failure classes `exc` is, or `None` if it is not one.

    The four are separated because the operator response differs and nothing could tell them
    apart: before this, a `statement_timeout` firing raised `QueryCanceled` that no handler in this
    repository caught, counted or named — `grep deadlock|40001|40P01` over `src/` returned prose
    only — so a database cancelling a runaway query looked, from every dashboard, exactly like a
    database that was down.

    Order is load-bearing, because these are not siblings: `QueryCanceled`, `DeadlockDetected` and
    `SerializationFailure` are all `OperationalError` subclasses in psycopg 3, so a broad
    `OperationalError` test first would collapse all of them into "unavailable".

    `deadlock` covers a serialization failure too. The label names the *class* — a transaction the
    server aborted because of a concurrent one — rather than the SQLSTATE, which the log line
    carries; two label values for one operator response (retry the unit of work) would split the
    series without splitting the decision.

    Anything that is not a database error at all returns `None` and is not counted: the block a
    caller runs inside `connection()` is its own code, and a `ValueError` from it is not a fact
    about Postgres.

    **That last rule is why the first test names `_DatabaseUnavailable` and not `ConnectionError`.**
    The builtin is the base of `ConnectionResetError`, `BrokenPipeError`, `ConnectionAbortedError`
    and `ConnectionRefusedError`, so testing it counted a caller's *own* dead socket — an HTTP
    client, an MCP session, a sink driver — as a Postgres outage, complete with a WARNING naming
    this deployment's database. `_DatabaseUnavailable` is raised only by the two places in this
    module that mean it (`connect`'s wrap, and the pool-checkout handler), so the class of the
    exception is now the same statement as the label.
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

    Named as well as counted because the counter says a kind and the line says which `operation`,
    which database, and what the server called it — and a `sqlstate` is the difference between
    "the statement timeout fired" and "somebody cancelled it".
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

    **The unit is the block, not the statement**, and that is what `connection()` can honestly
    measure: it hands out a connection and the caller runs one statement or twenty on it. That is
    also the quantity a pool cares about — `chemclaw_pg_pool_requests_waiting` rises because
    somebody *held* a connection, not because one statement was slow — so this is the number that
    joins the pool gauges to a call site.

    **It is therefore not database latency, and reading it as such is a mistake this measurement
    invited.** The span runs from before the checkout to after the caller's `with` body, so
    whatever else the caller does while holding the connection is inside it: measured, a block that
    ran one `SELECT 1` and then slept three seconds booked 3.029 s and emitted `db.slow` at the
    2 s threshold. That is
    the honest reading of *hold* time and a false one of *query* time, and one call site made the
    difference material — `kg/git_submitter._cluster_lock` takes a Postgres advisory lock and
    `yield`s across an entire note submission, fetch and push included, so every submission booked
    a git-push-length sample. It is not wrong that the connection was held that long; what was
    wrong is that it was booked unlabelled, so a dashboard rendered a remote git push as
    `{operation="unspecified"}` database latency. The fix on that side is a name
    (`kg_cluster_submit_lock`), which is what makes such a sample readable instead of alarming.

    Both branches of `connection()` are timed, pooled and dedicated alike: a process that never
    entered `pooling()` still holds a connection for its block, and dropping half the call sites
    out of the distribution because of how the connection was obtained would make the metric
    depend on which process is asking.
    """
    record_metric(
        lambda m: m.observe("chemclaw_db_query_duration_seconds", seconds, {"operation": operation})
    )
    threshold = settings.pg_slow_query_seconds
    if threshold and seconds >= threshold:
        log_event(
            logger,
            "db.slow",
            "database operation %r held a connection for %.3fs (threshold %.3fs)",
            operation,
            seconds,
            threshold,
            level=logging.WARNING,
            operation=operation,
            duration_s=round(seconds, 3),
        )


@asynccontextmanager
async def connection(
    dsn: str, *, statement_timeout_seconds: float | None = None, operation: str = _UNNAMED_OPERATION
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

    **`operation` is what the measurement is *about*.** It labels
    `chemclaw_db_query_duration_seconds` and names the call site in the slow-query and failure
    lines, so it must be a literal at the call site and low-cardinality — a table, a job, a store
    method — never a value derived from a request. It defaults rather than being required because
    thirty call sites in twenty-two modules predate it and an unlabelled hole in the distribution
    is worse than a coarse label: `unspecified` is a true statement about a call site nobody has
    named yet, and it is greppable.

    **What is timed is the whole block, not a statement** — see `_record_duration`. A call site
    that holds a connection across work of its own is measured doing exactly that, which is why a
    long-holding one owes the metric a name more than a short one does.

    **Every borrowed connection is timed and every database failure it raises is classified here**,
    which is the only place that can see both. `chemclaw_db_unavailable_total` is incremented at
    two front-door sites, so a `ConnectionError` inside an ingest activity, the outbox drain or the
    retention sweep incremented nothing at all; `statement_timeout` — 30 s by default — raised a
    `QueryCanceled` that nothing in this repository caught, counted or named. Both are counted now
    on `chemclaw_db_query_failures_total{kind}`, from the seam every store already goes through, so
    a new call site cannot forget to.
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
        pool = _pool_for(dsn, options)
        await pool.open()  # idempotent; the first caller starts the pool's background workers
        try:
            async with pool.connection() as conn:
                yield conn
        except (PoolTimeout, PoolClosed) as exc:
            # Both are `psycopg.OperationalError` subclasses raised only by the checkout itself, so
            # catching them here cannot swallow an error from the caller's block.
            raise _DatabaseUnavailable(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc
    except Exception as exc:
        # `Exception`, not `BaseException`: a cancelled task (Temporal cancelling an activity, a
        # dropped SSE connection) is not a database failure, and counting it as one would put the
        # ordinary shutdown path into the metric an operator pages on. Re-raised untouched — this
        # observes, it never decides.
        _record_failure(operation, dsn, exc)
        raise
    finally:
        _record_duration(operation, time.perf_counter() - started)


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
    (D-2026-08-05-the-connection-budget-is-a-fleet-number). It therefore reads the **sum over every
    pool this process holds**, not `settings.pg_pool_max_size`: a process routinely holds more than
    one — this module keys on `(dsn, options)` precisely so `/readyz` and the stores do not share
    connections, and the checkpointer registers a third — and measured against a live server that
    was three pools and 48 connections reported as 16, which puts the shipped chart at ~184 real
    connections against the 136 its values file provisions.

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
    METRICS.bind_gauge("chemclaw_pg_pool_max_size", lambda: float(_process_max_connections()))
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
        # **Only the pools this loop opened.** `psycopg_pool` schedules a pool's shutdown on the
        # loop it was opened in, so closing one built on a *different* loop raises
        # `RuntimeError: Event loop is closed` from inside the close — after the reference would
        # otherwise have been cleared, leaving the process holding a pool nobody can close. The
        # same hazard, and the same treatment, as `agent/checkpointer.close_checkpointer`.
        #
        # Dropping such a pool is what releases it, and it is worth being exact about which act
        # does that: an ended loop does *not* take its pool's connections with it — measured, an
        # abandoned pool held a live `pg_stat_activity` row until the reference went — so the
        # `clear()` below is the release rather than a tidy-up after one.
        # `_forget_pools_of_ended_loops` is the same act, performed as soon as the loop ends
        # instead of at shutdown. Production has one loop per process and closes what it opened.
        here = asyncio.get_running_loop()
        mine = [key for key in _POOLS if key[0] is here]
        pools = [_POOLS.pop(key) for key in mine]
        _POOLS.clear()
        for pool in pools:
            await pool.close()


def register_pool(pool: Any) -> None:
    """Count a pool this module did not build in this process's readings.

    For the one pool that is genuinely somebody else's: the LangGraph checkpointer's autocommit
    pool (`agent/checkpointer.py`), which every turn's state write goes through. It was invisible
    to `pool_stats` and to `chemclaw_pg_pool_max_size`, so a turn-serving process could open twice
    what it reported, and a saturated checkpointer stalled turns inside `AsyncPostgresSaver` while
    `chemclaw_pg_pool_requests_waiting` read 0.

    Registration only — the caller keeps the lifecycle, because `close_checkpointer` owns when that
    pool opens and closes and a second closer is how a live pool gets shut under a running turn.
    """
    if pool not in _FOREIGN_POOLS:
        _FOREIGN_POOLS.append(pool)


def unregister_pool(pool: Any) -> None:
    """Stop counting a foreign pool — called by its owner as it closes it."""
    if pool in _FOREIGN_POOLS:
        _FOREIGN_POOLS.remove(pool)


def _all_pools() -> list[Any]:
    """Every pool this process holds: the ones built here, plus the registered foreign ones."""
    _forget_pools_of_ended_loops()
    return [*_POOLS.values(), *_FOREIGN_POOLS]


def _process_max_connections() -> int:
    """How many Postgres connections this process may open — the sum over every pool it holds.

    Not `settings.pg_pool_max_size`, which is one pool's ceiling: see `bind_pool_metrics` for the
    measurement that separates the two.
    """
    return sum(int(pool.max_size) for pool in _all_pools())


def pool_stats() -> dict[str, int]:
    """Aggregate pool counters across this process's pools, for the metrics surface.

    Aggregated rather than per-DSN because the thing an operator alerts on is "is the front door
    waiting for connections?", which is a process-level question; naming each DSN would leak a
    host into a metric label for no operational gain. Foreign pools are included for the same
    reason they are registered at all — see `register_pool`.
    """
    total: dict[str, int] = {"pool_size": 0, "pool_available": 0, "requests_waiting": 0}
    for pool in _all_pools():
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
