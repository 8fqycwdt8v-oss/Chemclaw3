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


def test_a_dropped_tool_composite_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recovery source a tool composite did not have.

    `publish/outbox.enqueue` swallows every failure by construction — a completed calculation must
    not be failed by a queue write — and a *tool* composite is written to neither
    `calculation_results` nor `job_records`, because its key would name its own output. So the
    outbox row was the only copy: measured on the shipped code with the outbox unwritable, the hook
    returned 0, `backfill_cached` and `backfill_jobs` found nothing, and there was no third walk to
    run. Nothing in this deployment could produce that result again without re-running the science.

    `result_composites` is that record and `backfill_composites` is the walk over it.
    """
    from chemclaw.publish import composites, hooks, outbox
    from chemclaw.science.calc.models import LogdResult

    result = LogdResult(
        smiles="CC(=O)Nc1ccc(O)cc1", ph=7.4, clogp=1.35, pka=9.5, log_d=1.35, uncertainty=0.7
    )

    async def _run() -> None:
        await migrated_db_or_skip()
        monkeypatch.setattr(hooks, "publishing_enabled", lambda: True)
        monkeypatch.setattr(outbox, "publishing_enabled", lambda: True)
        monkeypatch.setattr(outbox, "enabled_names", lambda: ["alpha"])
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM result_composites")
            await conn.execute("DELETE FROM result_publications WHERE sink = 'alpha'")
            await conn.commit()

        # The local database is briefly unwritable for the *queue*, which is exactly the case the
        # outbox is built to swallow.
        def _explode(_operation: str) -> Any:
            raise ConnectionError("the outbox is unreachable")

        monkeypatch.setattr(outbox, "_connect", _explode)
        queued = await hooks.publish_tool_result(
            connector="calc",
            tool="predict_logd",
            arguments={"smiles": result.smiles},
            result=result,
        )
        assert queued == 0, "the enqueue failed, and the tool still returned its answer"

        monkeypatch.undo()
        monkeypatch.setattr(outbox, "publishing_enabled", lambda: True)
        monkeypatch.setattr(outbox, "enabled_names", lambda: ["alpha"])
        seen, requeued, skipped = await backfill.backfill_composites(dry_run=False, batch=10)
        assert (seen, requeued, skipped) == (1, 1, 0)

        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(
                "SELECT document->>'payload_kind' FROM result_publications WHERE sink = 'alpha'"
            )
            assert [row[0] for row in await cursor.fetchall()] == ["LogdResult"]

        # And it is idempotent, like its two siblings: the walk is safe to run twice.
        assert (await backfill.backfill_composites(dry_run=False, batch=10))[1] == 0
        assert (
            await composites.record_composite(
                calc_ref=hooks._composite_ref(
                    "calc", "predict_logd", result.model_dump(mode="json")
                ),
                calc_type="calc.predict_logd",
                payload_kind="LogdResult",
                input_hash="x",
                payload=result.model_dump(mode="json"),
            )
            is False
        )

    asyncio.run(_run())
