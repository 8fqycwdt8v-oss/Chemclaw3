"""The per-process Postgres pool, against a real server (skips offline).

- **Reuse.** A request path must stop paying a handshake per call; `pg_backend_pid()` is the
  witness that calls shared a backend.
- **The DSN's own `options` survive.** Test-schema isolation rides on the DSN's
  `options=-c search_path=…`; losing it would point the suite at live data (D-107).
- **Exhaustion is a `ConnectionError`.** Waiting forever would hang a turn, and a bare
  `PoolTimeout` would not be retried as an infrastructure fault.
"""

import asyncio
import threading

import psycopg
import pytest
from psycopg import conninfo
from psycopg_pool import AsyncConnectionPool

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics import Metrics
from chemclaw.evals.retrieval import _run_sync
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


async def test_pooling_reuses_backends_across_sequential_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty sequential `connection()` calls use at most `pg_pool_max_size` backends.

    Sequential on purpose: reuse across time is what takes the handshake off the hot path. Bounded
    by `max_size` rather than one pid because the pool round-robins its connections.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 1)
    monkeypatch.setattr(settings, "pg_pool_max_size", 4)

    await migrated_db_or_skip()
    async with db.pooling():
        pids = [await _scalar("SELECT pg_backend_pid()") for _ in range(_CALLS)]
    assert len(set(pids)) <= settings.pg_pool_max_size, sorted(set(pids))


async def test_unpooled_connections_do_not_share_a_backend() -> None:
    """The same calls without a pool are one backend each — the behavior being replaced.

    Pins the contrast the test above depends on, so a bounded pid count cannot pass for the
    trivial reason that Postgres happened to reuse pids.
    """
    await migrated_db_or_skip()
    pids = [await _scalar("SELECT pg_backend_pid()") for _ in range(_CALLS)]
    assert len(set(pids)) == _CALLS


async def test_pooled_connections_keep_the_dsn_search_path_and_our_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pooled connection carries the DSN's own libpq `options` *and* our statement timeout.

    `tests/conftest.py` already set `search_path` in the DSN, so this asserts the live isolation
    setting: if pooling dropped it, every Postgres test would write to `public`.
    """
    # A fractional value so Postgres renders it in milliseconds — the units our merge computes in.
    monkeypatch.setattr(settings, "pg_statement_timeout_seconds", 1.5)

    await migrated_db_or_skip()
    async with db.pooling():
        search_path = await _scalar("SHOW search_path")
        statement_timeout = await _scalar("SHOW statement_timeout")
    assert "chemclaw_test_" in search_path
    assert statement_timeout == "1500ms"


async def test_a_caller_that_asks_for_no_timeout_still_gets_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`connection()` bounds the statement even when the call site says nothing about a timeout.

    The bound is a property of the helper, not a convention every caller must keep. Both paths are
    asserted because they are different code: unpooled falls through to `connect()`, pooled builds
    the `options` into the pool's connection kwargs.
    """
    monkeypatch.setattr(settings, "pg_statement_timeout_seconds", 7.5)

    await migrated_db_or_skip()
    assert await _scalar("SHOW statement_timeout") == "7500ms"  # unpooled
    async with db.pooling():
        assert await _scalar("SHOW statement_timeout") == "7500ms"  # pooled


async def test_an_explicit_timeout_still_overrides_the_default() -> None:
    """An explicit timeout still overrides the default.

    `/readyz` bounds its `SELECT 1` tighter than the stores; a default replacing it would turn the
    probe into a long hang.
    """
    await migrated_db_or_skip()
    async with db.connection(settings.postgres_dsn, statement_timeout_seconds=2.5) as conn:
        cursor = await conn.execute("SHOW statement_timeout")
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "2500ms"


async def test_pool_exhaustion_surfaces_as_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that cannot get a connection in time fails the same way an unreachable DB does.

    Both are one transient infrastructure fault to the caller, and `ConnectionError` (not a
    `ChemclawError`) marks it retryable.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 1)
    monkeypatch.setattr(settings, "pg_pool_max_size", 1)
    monkeypatch.setattr(settings, "pg_pool_timeout_seconds", 0.2)

    await migrated_db_or_skip()
    async with db.pooling():
        async with db.connection(settings.postgres_dsn):
            with pytest.raises(ConnectionError) as exc_info:
                async with db.connection(settings.postgres_dsn):
                    pass
    assert "Postgres unreachable" in str(exc_info.value)
    # The password must not leak through the new failure path either.
    assert "chemclaw:chemclaw" not in str(exc_info.value)


async def test_pool_saturation_is_visible_as_a_gauge() -> None:
    """`requests_waiting` above zero is the only reading that says "the pool is too small".

    Without it, an undersized pool looks exactly like an unreachable database from the outside —
    which is the confusion the load test ran into, where connects timed out against an idle server.
    """
    await migrated_db_or_skip()
    assert db.pool_stats() == {"pool_size": 0, "pool_available": 0, "requests_waiting": 0}
    async with db.pooling():
        async with db.connection(settings.postgres_dsn):
            stats = db.pool_stats()
    assert stats["pool_size"] >= 1
    # Borrowed for the duration of the block, so it is not among the available ones.
    assert stats["pool_available"] < stats["pool_size"]


_POOL_GAUGES = (
    "chemclaw_pg_pool_size",
    "chemclaw_pg_pool_available",
    "chemclaw_pg_pool_requests_waiting",
    "chemclaw_pg_pool_max_size",
    "chemclaw_pg_session_pool_max_size",
    "chemclaw_pg_fleet_max_connections",
)


async def test_pooling_binds_the_pool_gauges_so_every_pooled_process_reports_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that opens a pool also exposes the gauges that describe it.

    Binding them only in the front door would leave workers and connector servers (including the
    background worker's long database work) without `requests_waiting`, the signal separating "pool
    too small" from "database down". Run against a fresh registry, because an unbound gauge is
    omitted, and the shared singleton may have been bound by an earlier test.
    """
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


def test_a_second_event_loop_gets_its_own_pool_rather_than_borrowing_a_broken_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second event loop gets its own pool rather than borrowing a broken one.

    `psycopg_pool` binds its waiters and workers to the loop that opened it, so a checkout queued
    from another loop is never woken by the hand-off and waits out its whole timeout. The key names
    the loop. `evals/retrieval._run_sync` creates exactly such a second loop.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 0)
    monkeypatch.setattr(settings, "pg_pool_max_size", 1)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.pooling():
            async with db.connection(settings.postgres_dsn):
                pass
            here = asyncio.get_running_loop()
            mine = {pool for key, pool in db._POOLS.items() if key[0] is here}
            assert mine, "the first loop opened no pool, so this test proves nothing"

            def _second_loop() -> set[object]:
                """A second loop in the same process — what `evals/retrieval._run_sync` starts."""

                async def _touch() -> set[object]:
                    async with db.connection(settings.postgres_dsn):
                        pass
                    there = asyncio.get_running_loop()
                    return {pool for key, pool in db._POOLS.items() if key[0] is there}

                return asyncio.run(_touch())

            theirs = await asyncio.to_thread(_second_loop)

        assert theirs, "the second loop's checkout was served by no pool of its own"
        assert not (mine & theirs), (
            "the second loop was handed a pool bound to the first loop's futures; its checkouts "
            "are woken by nothing and are served only when the pool timeout expires"
        )

    asyncio.run(_run())


async def test_a_nested_loop_that_opened_a_pool_still_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nested loop that opened a pool still ends.

    `asyncio.run` cancels and **awaits** every remaining task, and a `psycopg_pool` worker
    mid-reconnect does not come back, so a loop that abandons its pool can wedge its thread
    (`D-2026-09-13-a-loop-that-abandons-its-pool-can-fail-to-end`). `min_size` is 8 because the hang
    rate rises with it and is deterministic there (other tests here use 0 or 1, which cannot
    reproduce it). Asserted as the thread joining within a wall clock; an empty `_POOLS` would not
    distinguish a closed pool from a reclaimed one.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 8)
    monkeypatch.setattr(settings, "pg_pool_max_size", 16)

    await migrated_db_or_skip()
    async with db.pooling():
        # The parent loop has to hold a pool too, or the process is not the pooled one the
        # defect needs: `connection()` outside `pooling()` opens a dedicated connection and no
        # pool exists to be abandoned anywhere.
        async with db.connection(settings.postgres_dsn):
            pass
        returned = threading.Event()

        async def _touch() -> None:
            async with db.connection(settings.postgres_dsn):
                pass

        def _nested_loop() -> None:
            """`evals/retrieval._run_sync` itself, on a thread with no loop of its own.

            The real function, so the test fails if `_run_sync` stops calling
            `_closing_this_loops_pools`. This is production's arm too: `durable/eval_drift` reaches
            it via `asyncio.to_thread` inside `pooling()`.
            """
            try:
                _run_sync(_touch())
            finally:
                returned.set()

        thread = threading.Thread(target=_nested_loop, daemon=True)
        thread.start()
        # Daemon and generously bounded: a hang here is the defect, and a test that hung with it
        # would report as a timeout in whatever ran next rather than as this assertion.
        joined = returned.wait(timeout=30.0)

    assert joined, (
        "the nested asyncio.run never returned: its loop is in _cancel_all_tasks awaiting "
        "psycopg_pool's background workers, which a cancelled mid-reconnect worker does not "
        "leave — so the thread that called a live metric is wedged for the life of the process"
    )


def test_a_pool_whose_loop_has_ended_is_neither_counted_nor_left_holding_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pool whose loop has ended is neither counted nor left holding backends.

    Otherwise every `asyncio.run` inside a pooled process abandons an open pool: its connections
    inflate `chemclaw_pg_pool_max_size` and `pool_available`, and its backends stay live. Asserted
    through `pg_stat_activity` as well as the gauges, because "forgotten" and "closed" are different
    claims. Dropping the last reference is what closes it, since `close()` cannot run on an ended
    loop.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 1)
    monkeypatch.setattr(settings, "pg_pool_max_size", 1)
    # A distinct `application_name` so the count below sees this test's backends and no other
    # test's or dev shell's. It also changes nothing about the pooling: `application_name` rides in
    # the DSN, which is already part of the pool key.
    marker = "chemclaw-dead-loop-test"
    separator = "&" if "?" in settings.postgres_dsn else "?"
    dsn = f"{settings.postgres_dsn}{separator}application_name={marker}"

    def _marked_backends() -> int:
        """How many server connections carry the marker, counted from outside the pool."""
        with (
            psycopg.connect(settings.postgres_dsn, connect_timeout=5) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (marker,)
            )
            row = cur.fetchone()
        assert row is not None
        return int(row[0])

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.pooling():
            async with db.connection(dsn):
                pass

            def _short_lived_loop() -> None:
                """One `asyncio.run` on its own loop — `evals/retrieval._run_sync`'s shape."""

                async def _touch() -> None:
                    async with db.connection(dsn):
                        pass

                asyncio.run(_touch())

            await asyncio.to_thread(_short_lived_loop)
            # Reading the pool surface is what notices the ended loop; nothing else has to.
            assert db.pool_stats()["pool_size"] == 1, db.pool_stats()
            assert db._process_max_connections() == 1
            assert await asyncio.to_thread(_marked_backends) == 1

    asyncio.run(_run())


async def test_the_reported_per_process_ceiling_counts_pools_and_not_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported per-process ceiling counts pools, not processes.

    A process holds several pools (stores, `/readyz`'s narrow one, the checkpointer's), so the gauge
    sums the `max_size` of every pool it holds, foreign ones included; the checkpointer registers
    its pool. `tests/test_deploy_chart.py` derives the fleet figures from the rendered chart.
    """
    monkeypatch.setattr(settings, "pg_pool_max_size", 4)

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


def _gauge(rendered: str, name: str) -> float:
    """The value of one gauge in a rendered exposition."""
    for line in rendered.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split(" ", 1)[1])
    raise AssertionError(f"{name} is not in the exposition")


async def test_the_two_pool_ceiling_gauges_partition_this_process_by_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two pool-ceiling gauges partition this process by server.

    The alert checks each server against its own ceiling (a sum against a sum can miss one being
    over), so `chemclaw_pg_session_pool_max_size` is the part landing on a split session store and
    the alert subtracts it for the primary. This drives the front door's split shape and asserts
    both halves and their sum, so a pool landing in neither fails. Zero without a split.
    """
    monkeypatch.setattr(settings, "pg_pool_max_size", 8)

    await migrated_db_or_skip()
    registry = Metrics()
    monkeypatch.setattr("chemclaw.core.metrics.METRICS", registry)
    # A second *endpoint* for the same database: these gauges partition by the server `pg_endpoint`
    # says a DSN dials, and the loopback aliases are two spellings connectable from any runner.
    host = str(conninfo.conninfo_to_dict(settings.postgres_dsn).get("host") or "").lower()
    if host not in {"localhost", "127.0.0.1"}:
        pytest.skip(f"needs a loopback postgres_dsn to spell twice; this one dials {host!r}")
    split = conninfo.make_conninfo(
        settings.postgres_dsn, host="127.0.0.1" if host == "localhost" else "localhost"
    )
    monkeypatch.setattr(settings, "session_store_dsn", split)
    # `db.same_server` asks the server its `system_identifier`, which would collapse the loopback
    # pair to one (`D-2026-09-23-the-server-says-which-server-it-is`); seeding two identities makes
    # the pair stand in for two servers.
    from chemclaw.core.config import pg_endpoint

    for dsn, identity in ((settings.postgres_dsn, 1), (split, 2)):
        endpoint = pg_endpoint(dsn)
        assert endpoint is not None
        monkeypatch.setitem(db._SERVER_IDENTITY, endpoint, identity)
    async with db.pooling():
        async with db.connection(settings.postgres_dsn):
            pass
        async with db.connection(split, statement_timeout_seconds=2.0, pool_max_size=1):
            pass
        async with db.connection(split):
            pass
        rendered = registry.render()
        total = _gauge(rendered, "chemclaw_pg_pool_max_size")
        elsewhere = _gauge(rendered, "chemclaw_pg_session_pool_max_size")

    assert (total, elsewhere) == (17.0, 9.0), (
        f"the front door's split shape holds 8 + 1 + 8 = 17 connections, 9 of them on the "
        f"session store's server; the gauges report {total:.0f} and {elsewhere:.0f}, and the "
        "alert reads the primary's side as their difference"
    )

    # And nothing lands on a second server when there is not one.
    monkeypatch.setattr(settings, "session_store_dsn", "")
    async with db.pooling():
        async with db.connection(settings.postgres_dsn):
            pass
        assert _gauge(registry.render(), "chemclaw_pg_session_pool_max_size") == 0.0


def test_a_sized_pool_is_not_the_pool_the_next_caller_borrows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requested size is in the pool key, so one call site's ceiling is never another's.

    Otherwise the first caller to reach a `(dsn, options)` key would fix the pool's width for
    everyone, and a later caller with the same timeout could inherit `/readyz`'s single connection
    and answer 503 against an idle database. One more pool in the worst case is the safe direction.
    """
    monkeypatch.setattr(settings, "pg_pool_max_size", 4)
    monkeypatch.setattr(settings, "pg_pool_min_size", 2)

    async def _run() -> list[tuple[int, int]]:
        await migrated_db_or_skip()
        async with db.pooling():
            async with db.connection(settings.postgres_dsn, statement_timeout_seconds=2.0):
                pass
            async with db.connection(
                settings.postgres_dsn, statement_timeout_seconds=2.0, pool_max_size=1
            ):
                pass
            return sorted((int(pool.min_size), int(pool.max_size)) for pool in db._all_pools())

    assert asyncio.run(_run()) == [(1, 1), (2, 4)], (
        "asking for a narrow pool resized the pool an unsized caller borrows from, or shared one "
        "with it. The size belongs in the pool key: db._POOLS"
    )


def test_a_narrow_pool_does_not_raise_on_the_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`min_size` is clamped under the requested size, or `/readyz` answers 500.

    psycopg raises `ValueError` for `min_size > max_size`, which on the request path is not a
    `psycopg.Error`, so nothing counts or names it and the probe's own `except` misses it.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 8)
    monkeypatch.setattr(settings, "pg_pool_max_size", 16)

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        async with db.pooling():
            async with db.connection(settings.postgres_dsn, pool_max_size=1) as conn:
                await conn.execute("SELECT 1")
            pool = next(iter(db._all_pools()))
            return int(pool.min_size), int(pool.max_size)

    assert asyncio.run(_run()) == (1, 1)


def test_a_falsy_pool_size_does_not_mint_a_second_pool_of_the_default_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool key carries the *effective* width, not the width that was asked for.

    A falsy request resolves to the default, so keying on the raw request would hold two identical
    pools on one DSN. Unreachable today; pinned for future settings-driven callers that pass 0.
    """
    monkeypatch.setattr(settings, "pg_pool_max_size", 4)

    async def _run() -> list[int]:
        await migrated_db_or_skip()
        async with db.pooling():
            for requested in (None, 0, settings.pg_pool_max_size):
                async with db.connection(settings.postgres_dsn, pool_max_size=requested):
                    pass
            return sorted(int(pool.max_size) for pool in db._all_pools())

    assert asyncio.run(_run()) == [4], (
        "an omitted size, a falsy one and the default spelled out are one pool of one width; "
        "keying on the request rather than the resolution made them two"
    )


def test_the_three_pool_gauges_read_one_instant_rather_than_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three pool gauges read one instant rather than three.

    "Is the pool full **and** are callers waiting" needs one snapshot. Driven with a walk that
    changes on every call, so separate walks per gauge would visibly disagree.
    """
    walks = iter(
        [
            {"pool_size": 10, "pool_available": 10, "requests_waiting": 0},
            {"pool_size": 10, "pool_available": 0, "requests_waiting": 7},
            {"pool_size": 3, "pool_available": 1, "requests_waiting": 99},
        ]
    )
    monkeypatch.setattr(db, "pool_stats", lambda: next(walks))
    db.reset_pool_snapshot()

    first = db.coherent_pool_stats()
    assert [db.coherent_pool_stats() for _ in range(2)] == [first, first], (
        "three reads inside one coherence window came from different walks, so a scrape can "
        "publish a pool_size, a pool_available and a requests_waiting that never held together"
    )
    # And the walk really would have moved: this is what the old binding published.
    assert next(walks) != first


def test_a_caller_cannot_mutate_the_snapshot_the_next_gauge_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cannot mutate the snapshot the next gauge reads.

    A shared dict handed out by reference would let one gauge corrupt the next one's reading,
    invisibly to every single-gauge assertion.
    """
    monkeypatch.setattr(
        db, "pool_stats", lambda: {"pool_size": 5, "pool_available": 2, "requests_waiting": 1}
    )
    db.reset_pool_snapshot()

    borrowed = db.coherent_pool_stats()
    borrowed["pool_size"] = 999
    assert db.coherent_pool_stats()["pool_size"] == 5


def test_closing_the_pools_drops_the_window_they_were_measured_in() -> None:
    """A process that has closed its pools must not answer a later scrape from the live window.

    `pooling()`'s exit resets it, so a shutdown does not publish its last busy reading.
    """
    db.reset_pool_snapshot()
    assert db._POOL_SNAPSHOT is None
    db.coherent_pool_stats()
    assert db._POOL_SNAPSHOT is not None
    db.reset_pool_snapshot()
    assert db._POOL_SNAPSHOT is None
