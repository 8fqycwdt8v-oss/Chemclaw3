"""The per-process Postgres pool, against a real server (skips offline).

Three properties matter, and none can be proven without a live backend:

- **Reuse.** The whole point is that a request path stops paying a TCP+auth handshake per call.
  A load test measured 401 connections opened for 150 chat turns; the connect that then failed to
  be scheduled inside its timeout is what silently disarmed the rollback watermark (D-107).
  `pg_backend_pid()` is the only honest witness that two calls shared one backend.
- **The DSN's own `options` survive.** The pool builds its connections from the DSN plus our
  `statement_timeout`, and the test-schema isolation in `tests/pg.py` rides entirely on the DSN's
  `options=-c search_path=…`. If pooling clobbered it the suite would silently operate on live
  data — the exact regression `db._merged_options` exists to prevent (D-107).
- **Exhaustion is a `ConnectionError`.** Waiting forever for a free connection would convert a
  transient shortage into a hung turn, and a bare `PoolTimeout` would not be retried by the
  callers that treat "database unreachable" as a retryable infrastructure fault.
"""

import asyncio

import pytest
from psycopg_pool import AsyncConnectionPool

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics import Metrics
from tests.pg import migrated_db_or_skip


async def _scalar(sql: str) -> str:
    """Run a one-value query on a borrowed connection (every test here wants exactly this).

    Passes no `statement_timeout_seconds`, so every test routed through it exercises the
    defaulted path that every store now takes.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(sql)
        row = await cursor.fetchone()
    assert row is not None
    return str(row[0])


_CALLS = 20


def test_pooling_reuses_backends_across_sequential_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty sequential `connection()` calls use at most `pg_pool_max_size` backends.

    Counterfactual: with connect-per-call each one is its own backend — the churn the load test
    measured at ~2.7 connects per turn. Sequential (not concurrent) on purpose: the pool hands out
    whichever connection is free, and reuse across *time* is what takes the handshake off the hot
    path. Bounded by `max_size` rather than pinned to one pid because the pool round-robins the
    connections it holds.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 1)
    monkeypatch.setattr(settings, "pg_pool_max_size", 4)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.pooling():
            pids = [await _scalar("SELECT pg_backend_pid()") for _ in range(_CALLS)]
        assert len(set(pids)) <= settings.pg_pool_max_size, sorted(set(pids))

    asyncio.run(_run())


def test_unpooled_connections_do_not_share_a_backend() -> None:
    """The same calls without a pool are one backend each — the behavior being replaced.

    Pins the contrast the test above depends on, so a bounded pid count cannot pass for the
    trivial reason that Postgres happened to reuse pids.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        pids = [await _scalar("SELECT pg_backend_pid()") for _ in range(_CALLS)]
        assert len(set(pids)) == _CALLS

    asyncio.run(_run())


def test_pooled_connections_keep_the_dsn_search_path_and_our_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pooled connection carries the DSN's own libpq `options` *and* our statement timeout.

    `tests/conftest.py` has already redirected `postgres_dsn` to `options=-c search_path=<schema>`,
    so this asserts on the live suite-isolation setting rather than a contrived one: if pooling
    dropped it, every Postgres test in this suite would start writing to `public`.
    """
    # A fractional value so Postgres renders it in milliseconds — the units our merge computes in.
    monkeypatch.setattr(settings, "pg_statement_timeout_seconds", 1.5)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.pooling():
            search_path = await _scalar("SHOW search_path")
            statement_timeout = await _scalar("SHOW statement_timeout")
        assert "chemclaw_test_" in search_path
        assert statement_timeout == "1500ms"

    asyncio.run(_run())


def test_a_caller_that_asks_for_no_timeout_still_gets_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`connection()` bounds the statement even when the call site says nothing about a timeout.

    Measured before the default existed: `SHOW statement_timeout` on a connection borrowed with no
    keyword returned `0` — no bound at all — both pooled and unpooled. Every store passed
    `pg_statement_timeout_seconds` by hand, so the bound was a convention twenty-two call sites
    happened to keep rather than a property of the helper, and a twenty-third that forgot would
    hold a pooled connection on one bad query for as long as the query ran.

    Both paths are asserted because they are different code: unpooled falls through to `connect()`,
    pooled builds the libpq `options` into the pool's connection kwargs.
    """
    monkeypatch.setattr(settings, "pg_statement_timeout_seconds", 7.5)

    async def _run() -> None:
        await migrated_db_or_skip()
        assert await _scalar("SHOW statement_timeout") == "7500ms"  # unpooled
        async with db.pooling():
            assert await _scalar("SHOW statement_timeout") == "7500ms"  # pooled

    asyncio.run(_run())


def test_an_explicit_timeout_still_overrides_the_default() -> None:
    """A call site that needs a different bound keeps it — the readiness probe is the live one.

    `/readyz` deliberately bounds its `SELECT 1` at `service_readiness_db_timeout_seconds` (2 s), a
    tighter budget than the stores'. A default that silently replaced an explicit argument would
    turn that probe into a 30-second hang, which is the failure it exists to avoid.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn, statement_timeout_seconds=2.5) as conn:
            cursor = await conn.execute("SHOW statement_timeout")
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "2500ms"

    asyncio.run(_run())


def test_pool_exhaustion_surfaces_as_a_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that cannot get a connection in time fails the same way an unreachable DB does.

    Same exception type on purpose: from the caller's side "no free connection" and "no database"
    are one transient infrastructure fault, and `ConnectionError` (deliberately not a
    `ChemclawError`) is what marks it retryable rather than bad data.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 1)
    monkeypatch.setattr(settings, "pg_pool_max_size", 1)
    monkeypatch.setattr(settings, "pg_pool_timeout_seconds", 0.2)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.pooling():
            async with db.connection(settings.postgres_dsn):
                with pytest.raises(ConnectionError) as exc_info:
                    async with db.connection(settings.postgres_dsn):
                        pass
        assert "Postgres unreachable" in str(exc_info.value)
        # The password must not leak through the new failure path either.
        assert "chemclaw:chemclaw" not in str(exc_info.value)

    asyncio.run(_run())


def test_pool_saturation_is_visible_as_a_gauge() -> None:
    """`requests_waiting` above zero is the only reading that says "the pool is too small".

    Without it, an undersized pool looks exactly like an unreachable database from the outside —
    which is the confusion the load test ran into, where connects timed out against an idle server.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        assert db.pool_stats() == {"pool_size": 0, "pool_available": 0, "requests_waiting": 0}
        async with db.pooling():
            async with db.connection(settings.postgres_dsn):
                stats = db.pool_stats()
        assert stats["pool_size"] >= 1
        # Borrowed for the duration of the block, so it is not among the available ones.
        assert stats["pool_available"] < stats["pool_size"]

    asyncio.run(_run())


_POOL_GAUGES = (
    "chemclaw_pg_pool_size",
    "chemclaw_pg_pool_available",
    "chemclaw_pg_pool_requests_waiting",
    "chemclaw_pg_pool_max_size",
    "chemclaw_pg_fleet_max_connections",
)


def test_pooling_binds_the_pool_gauges_so_every_pooled_process_reports_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that opens a pool cannot do so without also exposing the readings that describe it.

    This is the half the gauge above was missing. All three pool gauges were bound in the front
    door's `create_app`, so of the seventeen processes the shipped chart pools in, the eleven that
    are workers and connector servers served a `/metrics` surface with no pool reading at all —
    including the background worker, which runs the retention sweep, the reindex and the chain
    verification, the longest database work in the deployment. D-119 introduced `requests_waiting`
    as the signal that distinguishes "the pool is too small" from "the database is down"; it was
    absent exactly where that distinction is hardest to make from a log.

    Run against a *fresh* registry, because an unbound gauge is omitted from the exposition rather
    than rendered as 0 — on the shared singleton this would pass in any session where some earlier
    test happened to build the front door, which is precisely the accident it exists to rule out.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        registry = Metrics()
        # `bind_pool_metrics` resolves METRICS at call time (the declared lazy import that keeps
        # `core` free of module-scope sibling imports), so patching the module attribute reaches it.
        monkeypatch.setattr("chemclaw.core.metrics.METRICS", registry)
        assert not any(name in registry.render() for name in _POOL_GAUGES), (
            "a fresh registry must expose no pool gauge, or this test proves nothing"
        )
        # No front door, no worker, no connector server — just the pool itself.
        async with db.pooling():
            rendered = registry.render()
        for name in _POOL_GAUGES:
            assert name in rendered, f"{name} is not exposed by a process that opened a pool"

    asyncio.run(_run())


def test_a_second_event_loop_gets_its_own_pool_rather_than_borrowing_a_broken_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pool belongs to the loop that opened it, so the key has to name the loop.

    `psycopg_pool` binds its waiter futures and background workers to the loop the pool was opened
    in. `_pool_for` keyed only on `(dsn, options)`, so any coroutine on any loop in the process got
    the *same* object — and a checkout queued on loop-2 was never woken by the hand-off callback
    running on loop-1. Measured with `max_size=1` and a `pool_timeout` of 8 s, the holder releasing
    at t=1.00 s: the cross-loop waiter was served after **8.01 s** (its own timeout expiring),
    against **1.00 s** for the identical hand-off on the pool's own loop.

    `evals/retrieval._run_sync` creates exactly that second loop, on purpose, when a metric is
    invoked from a coroutine — and then blocks the first on `thread.join()` for the whole wait.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 0)
    monkeypatch.setattr(settings, "pg_pool_max_size", 1)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.pooling():
            async with db.connection(settings.postgres_dsn):
                pass
            here = id(asyncio.get_running_loop())
            mine = {pool for key, pool in db._POOLS.items() if key[0] == here}
            assert mine, "the first loop opened no pool, so this test proves nothing"

            def _second_loop() -> set[object]:
                """A second loop in the same process — what `evals/retrieval._run_sync` starts."""

                async def _touch() -> set[object]:
                    async with db.connection(settings.postgres_dsn):
                        pass
                    there = id(asyncio.get_running_loop())
                    return {pool for key, pool in db._POOLS.items() if key[0] == there}

                return asyncio.run(_touch())

            theirs = await asyncio.to_thread(_second_loop)

        assert theirs, "the second loop's checkout was served by no pool of its own"
        assert not (mine & theirs), (
            "the second loop was handed a pool bound to the first loop's futures; its checkouts "
            "are woken by nothing and are served only when the pool timeout expires"
        )

    asyncio.run(_run())


def test_the_reported_per_process_ceiling_counts_pools_and_not_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`chemclaw_pg_pool_max_size` is the per-process half of the fleet budget, so it must be true.

    Two defects, one number. `bind_pool_metrics` reported `settings.pg_pool_max_size` — *one*
    pool's ceiling — while a process routinely holds more than one: `db.py` deliberately keys pools
    on `(dsn, options)` so `/readyz`'s 2 s-bounded connection stays out of the stores' 30 s pool,
    and the LangGraph checkpointer opens a third, autocommit pool that `pool_stats` could not see
    at all. Measured in one process against a live server: three pools, 48 connections, reported as
    16 — which puts the shipped chart at ~184 real connections against the
    `postgres.maxConnections: 136` its values file says is exactly what it provisions.

    So the gauge sums the `max_size` of every pool this process actually holds, foreign ones
    included, and the checkpointer registers its pool instead of the metrics learning about it
    twice.
    """
    monkeypatch.setattr(settings, "pg_pool_max_size", 4)

    async def _run() -> None:
        await migrated_db_or_skip()
        registry = Metrics()
        monkeypatch.setattr("chemclaw.core.metrics.METRICS", registry)
        async with db.pooling():
            # The stores' pool and `/readyz`'s differently-bounded one: two keys, by design.
            async with db.connection(settings.postgres_dsn):
                pass
            async with db.connection(settings.postgres_dsn, statement_timeout_seconds=2.0):
                pass
            # And the pool `core.db` does not build — the checkpointer's shape.
            foreign = AsyncConnectionPool(
                conninfo=settings.postgres_dsn,
                kwargs={"autocommit": True},
                min_size=0,
                max_size=settings.pg_pool_max_size,
                open=False,
            )
            await foreign.open()
            db.register_pool(foreign)
            try:
                reported = _gauge(registry.render(), "chemclaw_pg_pool_max_size")
                stats = db.pool_stats()
            finally:
                db.unregister_pool(foreign)
                await foreign.close()

        assert reported == 3 * settings.pg_pool_max_size, (
            f"this process may open {3 * settings.pg_pool_max_size} connections and reports "
            f"{reported:.0f}; the fleet budget check is made against this number"
        )
        # And the foreign pool's saturation is legible at all, which it was not before.
        assert stats["pool_size"] >= 1

    asyncio.run(_run())


def _gauge(rendered: str, name: str) -> float:
    """The value of one gauge in a rendered exposition."""
    for line in rendered.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split(" ", 1)[1])
    raise AssertionError(f"{name} is not in the exposition")
