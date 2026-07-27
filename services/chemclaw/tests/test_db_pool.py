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

from chemclaw import db
from chemclaw.config import settings
from tests.pg import migrated_db_or_skip


async def _scalar(sql: str) -> str:
    """Run a one-value query on a borrowed connection (every test here wants exactly this)."""
    async with db.connection(
        settings.postgres_dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
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
