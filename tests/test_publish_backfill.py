"""The corpus-wide backfill walk: every row seen exactly once, and skip vs. queue counted right.

`backfill_cached`/`backfill_jobs` page through `calculation_results`/`job_records` with
`LIMIT`/`OFFSET`, which Postgres only guarantees to partition a table's rows correctly when the
`ORDER BY` is a total order. `created_at`/`completed_at` are not unique on their own — concurrent
calculator workers, or a bulk import in one transaction, can give several rows the identical
instant — so what is actually under test here is the tiebreaker (`key`/`job_id`, each table's own
primary key): every row inserted must be `seen` exactly once by a walk whose batch size is smaller
than a run of tied rows, however the ties land.

This file was an empty stand-in (`git show`: added as the empty blob by a large dead-code sweep) —
the pagination/skip logic this module owns had no coverage at all, only a monkeypatched stand-in
used elsewhere for heartbeat timing.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.publish import backfill, outbox
from tests.pg import migrated_db_or_skip


async def _reset(conn: Any) -> None:
    """Empty both tables, so each test starts from a known corpus."""
    await conn.execute("DELETE FROM calculation_results")
    await conn.execute("DELETE FROM job_records")
    await conn.execute("DELETE FROM result_publications")
    await conn.commit()


async def _insert_cached(conn: Any, key: str, created_at: datetime, calc_type: str = "pka") -> None:
    await conn.execute(
        "INSERT INTO calculation_results "
        "(key, calc_type, calc_version, input_hash, params_hash, result, created_at) "
        "VALUES (%s, %s, 'v1', 'h', 'p', %s, %s)",
        (key, calc_type, Jsonb({"pka": 4.2}), created_at),
    )


async def _insert_job(conn: Any, job_id: str, completed_at: datetime, connector: str = "x") -> None:
    await conn.execute(
        "INSERT INTO job_records "
        "(job_id, connector, job, rationale, requested_by, summary, result, completed_at) "
        "VALUES (%s, %s, 'unregistered-job', 'r', 'tester', 's', %s, %s)",
        (job_id, connector, Jsonb({"ok": True}), completed_at),
    )


def test_the_queries_break_ties_on_a_unique_column() -> None:
    """LIMIT/OFFSET over an ORDER BY with no unique column can silently skip a tied row.

    Offline and exact, so a future edit that drops the tiebreaker fails here immediately rather
    than waiting on the non-deterministic Postgres behaviour it would take to reproduce the skip.
    """
    assert "ORDER BY created_at, key" in backfill._CACHED
    assert "ORDER BY completed_at, job_id" in backfill._JOBS


def test_every_row_is_seen_exactly_once_even_when_many_share_a_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: a batch smaller than a run of tied `created_at` values must still see all.

    Every row here carries an unregistered `calc_type`, so the count under test is `seen` — the
    walk's own accounting of how many rows it visited — not anything projection-dependent.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            # Two ties of four and three, both wider than `batch=2`, so at least one page boundary
            # must fall strictly inside a run of identical timestamps.
            tie_a = datetime(2026, 1, 1, tzinfo=UTC)
            tie_b = datetime(2026, 1, 2, tzinfo=UTC)
            for i in range(4):
                await _insert_cached(conn, f"a-{i}", tie_a, calc_type="no-such-calculator")
            for i in range(3):
                await _insert_cached(conn, f"b-{i}", tie_b, calc_type="no-such-calculator")
            await conn.commit()

        seen, queued, skipped = await backfill.backfill_cached(dry_run=True, batch=2)

        assert seen == 7
        assert skipped == 7, "every row has an unregistered calc_type"
        assert queued == 0

    asyncio.run(_run())


def test_every_job_is_seen_exactly_once_even_when_many_share_a_timestamp() -> None:
    """`backfill_jobs`'s half of the same regression, over `job_records`/`completed_at`."""

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            tie = datetime(2026, 1, 1, tzinfo=UTC)
            for i in range(5):
                await _insert_job(conn, f"job-{i}", tie)
            await conn.commit()

        seen, queued, skipped = await backfill.backfill_jobs(dry_run=True, batch=2)

        assert seen == 5
        assert skipped == 5, "`x.unregistered-job` matches no projector prefix"
        assert queued == 0

    asyncio.run(_run())


def test_a_row_with_a_registered_projector_is_queued_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip/queue split itself, decoupled from the real outbox and projection machinery.

    `enqueue_payload` is stubbed here rather than driven for real: what this module owns is
    deciding whether a row *has* a projector and dispatching to `outbox.enqueue_payload`
    accordingly, not what that call then does with a valid payload — `test_publish_outbox.py` and
    `test_publish_project*.py` are where that is proven.
    """

    async def _fake_enqueue(**_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(outbox, "enqueue_payload", _fake_enqueue)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            await _insert_cached(conn, "known", datetime(2026, 1, 1, tzinfo=UTC), calc_type="pka")
            await _insert_cached(
                conn, "unknown", datetime(2026, 1, 1, tzinfo=UTC), calc_type="no-such-calculator"
            )
            await conn.commit()

        seen, queued, skipped = await backfill.backfill_cached(dry_run=False, batch=10)

        assert seen == 2
        assert queued == 1, "the row with a registered projector must reach enqueue_payload"
        assert skipped == 1

    asyncio.run(_run())


def test_dry_run_counts_without_calling_the_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dry_run=True` must be a read-only preview: no row reaches `outbox.enqueue_payload`."""

    async def _explode(**_kwargs: Any) -> int:
        raise AssertionError("dry_run must not enqueue anything")

    monkeypatch.setattr(outbox, "enqueue_payload", _explode)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            await _insert_cached(conn, "known", datetime(2026, 1, 1, tzinfo=UTC), calc_type="pka")
            await conn.commit()

        seen, queued, skipped = await backfill.backfill_cached(dry_run=True, batch=10)

        assert (seen, queued, skipped) == (1, 1, 0)

    asyncio.run(_run())


def test_requeue_failed_returns_failed_rows_to_pending() -> None:
    """An operator's fix (rotated credential, applied DDL) is a resource nothing else recovers."""

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            await conn.execute(
                "INSERT INTO result_publications "
                "(sink, calc_ref, document, schema_version, state, attempts, last_error) "
                "VALUES ('a', 'r-1', %s, 1, 'failed', 3, 'boom'), "
                "       ('a', 'r-2', %s, 1, 'pending', 0, '')",
                (Jsonb({}), Jsonb({})),
            )
            await conn.commit()

        reset_count = await backfill.requeue_failed()
        assert reset_count == 1

        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(
                "SELECT state, attempts, last_error FROM result_publications WHERE calc_ref = 'r-1'"
            )
            row = await cursor.fetchone()
        assert row is not None
        assert tuple(row) == ("pending", 0, "")

    asyncio.run(_run())
