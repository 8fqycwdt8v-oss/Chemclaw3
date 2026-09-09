"""The corpus-wide backfill walk: every row seen exactly once, and skip vs. queue counted right.

`backfill_cached`/`backfill_jobs` page through `calculation_results`/`job_records` by **keyset**:
each page asks for the rows after the last one read, `(created_at, key)` and `(completed_at,
job_id)`. `created_at`/`completed_at` are not unique on their own — concurrent calculator workers,
or a bulk import in one transaction, can give several rows the identical instant — so what is under
test here is the tiebreaker (`key`/`job_id`, each table's own primary key): every row inserted must
be `seen` exactly once by a walk whose batch size is smaller than a run of tied rows, however the
ties land.

The walk used to be `LIMIT`/`OFFSET`, which is O(n²/batch) — measured on 500 000 rows, page 1 costs
1.4 ms and page 400 costs 388.7 ms, so 500k rows in 1 000-row pages is ~90 s of pure skipping and a
5M-row calculation cache is hours of it. A cursor predicate is not only faster, it is *correct
under concurrent writes*, which is the half a row count cannot see and
`test_a_row_arriving_behind_the_cursor_does_not_shift_the_walk` is for.

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
    """A cached row whose payload actually projects.

    It used to be `{"pka": 4.2}` with no subject at all, which `_pka` cannot build a record from —
    harmless while a dry run only *routed*, and a projection failure the moment one projects. A
    fixture that cannot be read is the wrong control for "was this row queued".
    """
    await conn.execute(
        "INSERT INTO calculation_results "
        "(key, calc_type, calc_version, input_hash, params_hash, result, created_at) "
        "VALUES (%s, %s, 'v1', 'h', 'p', %s, %s)",
        (key, calc_type, Jsonb({"smiles": "CCO", "pka": 4.2}), created_at),
    )


async def _insert_job(conn: Any, job_id: str, completed_at: datetime, connector: str = "x") -> None:
    await conn.execute(
        "INSERT INTO job_records "
        "(job_id, connector, job, rationale, requested_by, summary, result, completed_at) "
        "VALUES (%s, %s, 'unregistered-job', 'r', 'tester', 's', %s, %s)",
        (job_id, connector, Jsonb({"ok": True}), completed_at),
    )


def test_the_queries_break_ties_on_a_unique_column_and_walk_by_keyset() -> None:
    """A tiebreaker, and a cursor rather than an offset — the two shapes this walk depends on.

    Offline and exact, so a future edit that drops either fails here immediately rather than
    waiting on the non-deterministic Postgres behaviour it would take to reproduce a skipped row.
    The `OFFSET` half is an absence check for the same reason: it is what regressed, it reads as
    harmless, and its cost is invisible until a deployment has enough rows to page through.
    """
    assert "ORDER BY created_at, key" in backfill._CACHED
    assert "ORDER BY completed_at, job_id" in backfill._JOBS
    assert "(created_at, key) > (%s, %s)" in backfill._CACHED
    assert "(completed_at, job_id) > (%s, %s)" in backfill._JOBS
    assert "OFFSET" not in backfill._CACHED and "OFFSET" not in backfill._JOBS


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

        counts = await backfill.backfill_cached(dry_run=True, batch=2)
        seen, queued, skipped = counts.seen, counts.queued, counts.skipped

        assert seen == 7
        assert skipped == 7, "every row has an unregistered calc_type"
        assert queued == 0

    asyncio.run(_run())


def test_a_row_arriving_behind_the_cursor_does_not_shift_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent insert older than the cursor must not push a row out of the walk.

    This is what a keyset buys beyond speed, and it is why the fix is a different *predicate*
    rather than a bigger batch. `OFFSET n` means "skip the first n rows **of the query as it is
    now**", so a row landing before the cursor between two pages shifts every later page by one:
    the row on the boundary is fetched twice and its neighbour never at all — silently, with no
    error and no `skipped` increment. This walk is exactly where that happens. It is the
    long-running one (a 5M-row cache took hours of pure skipping before this), and
    `calculation_results` is written by every calculator worker while it runs.

    So the assertion is on the *sequence of keys visited*, not on a count: under an offset walk the
    count can even come out right while the list holds a duplicate and misses a row.
    """
    visited: list[str] = []
    intruded = False

    def _recording_project(**kwargs: Any) -> list[Any]:
        visited.append(str(kwargs["calc_ref"]))
        return [object()]

    async def _intruding_enqueue(records: list[Any]) -> int:
        """The intrusion rides on the walk's own `await`, so it is committed before the next page.

        It was on `enqueue_payload`, which the walk no longer calls; splitting that into a
        projection and a write moved the only awaited step to here. A background task would have
        made the insert race the next page read, which is the one thing this test must not leave to
        chance — it asserts the *sequence of keys visited*.
        """
        nonlocal intruded
        if not intruded:
            intruded = True
            # Older than every row still ahead of the cursor, which is the one insert an offset
            # walk cannot survive. Its own connection, because the walk holds none between pages.
            async with db.connection(settings.postgres_dsn) as conn:
                await _insert_cached(conn, "walk-earlier", datetime(2026, 1, 1, tzinfo=UTC))
                await conn.commit()
        return len(records)

    monkeypatch.setattr(outbox, "project_payload", _recording_project)
    monkeypatch.setattr(outbox, "enqueue", _intruding_enqueue)

    async def _run() -> int:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            for index in range(6):
                await _insert_cached(
                    conn, f"walk-{index}", datetime(2026, 2, 1 + index, tzinfo=UTC)
                )
            await conn.commit()
        return (await backfill.backfill_cached(dry_run=False, batch=2)).seen

    seen = asyncio.run(_run())
    assert visited == [f"walk-{index}" for index in range(6)], (
        f"the walk visited {visited}, so a page boundary moved when a row landed behind the cursor"
    )
    assert seen == 6, f"the walk reports {seen} rows over 6 originals plus one arriving behind it"


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

        counts = await backfill.backfill_jobs(dry_run=True, batch=2)
        seen, queued, skipped = counts.seen, counts.queued, counts.skipped

        assert seen == 5
        assert skipped == 5, "`x.unregistered-job` matches no projector prefix"
        assert queued == 0

    asyncio.run(_run())


def test_a_row_with_a_registered_projector_is_queued_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip/queue split itself, decoupled from the real outbox and projection machinery.

    The projection is stubbed here rather than driven for real: what this module owns is deciding
    whether a row *has* a projector and dispatching accordingly, not what the projection then does
    with a valid payload — `test_publish_outbox.py` and `test_publish_project*.py` are where that
    is proven.
    """

    def _one_record(**_kwargs: Any) -> list[Any]:
        return [object()]

    async def _fake_enqueue(records: list[Any]) -> int:
        return len(records)

    monkeypatch.setattr(outbox, "project_payload", _one_record)
    monkeypatch.setattr(outbox, "enqueue", _fake_enqueue)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            await _insert_cached(conn, "known", datetime(2026, 1, 1, tzinfo=UTC), calc_type="pka")
            await _insert_cached(
                conn, "unknown", datetime(2026, 1, 1, tzinfo=UTC), calc_type="no-such-calculator"
            )
            await conn.commit()

        counts = await backfill.backfill_cached(dry_run=False, batch=10)

        assert counts.seen == 2
        assert counts.queued == 1, "the row with a registered projector must reach the outbox"
        assert counts.skipped == 1
        assert counts.failed == 0

    asyncio.run(_run())


def test_dry_run_counts_without_calling_the_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dry_run=True` must be a read-only preview: no row reaches the outbox *write*.

    It reaches the *projection* now, deliberately — that is what makes the preview's numbers the
    numbers the real pass produces — so the line this guards moved from `enqueue_payload` to
    `enqueue`, which is where the write is.
    """

    async def _explode(records: list[Any]) -> int:
        raise AssertionError("dry_run must not enqueue anything")

    monkeypatch.setattr(outbox, "enqueue", _explode)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            await _insert_cached(conn, "known", datetime(2026, 1, 1, tzinfo=UTC), calc_type="pka")
            await conn.commit()

        counts = await backfill.backfill_cached(dry_run=True, batch=10)

        assert (counts.seen, counts.queued, counts.skipped, counts.failed) == (1, 1, 0, 0)

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


def test_a_requeue_dry_run_counts_the_retired_rows_without_touching_them() -> None:
    """The preview must not make the one write that destroys what it is previewing.

    `--requeue` reset every retired row whatever `--dry-run` said, and the run then printed "dry
    run: nothing was written" — so an operator previewing a backfill cleared the attempt budget and
    the recorded error on every dead-lettered publication in the deployment, and the drain
    redelivered them. Driven through the CLI's own `main`, because the walks below honoured
    `dry_run` all along and only the entry point's requeue branch did not.
    """
    import chemclaw.cli.backfill_publications as backfill_publications

    async def _seed() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            await conn.execute(
                "INSERT INTO result_publications "
                "(sink, calc_ref, document, schema_version, state, attempts, last_error) "
                "VALUES ('a', 'r-1', %s, 1, 'failed', 3, 'boom')",
                (Jsonb({}),),
            )
            await conn.commit()

    asyncio.run(_seed())

    assert backfill_publications.main(["--dry-run", "--requeue"]) == 0

    async def _after() -> tuple[Any, int]:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(
                "SELECT state, attempts, last_error FROM result_publications WHERE calc_ref = 'r-1'"
            )
            row = await cursor.fetchone()
        # Reported, not merely skipped: the operator asked what a requeue would cover.
        return row, await backfill.requeue_failed(dry_run=True)

    row, would_reset = asyncio.run(_after())
    assert row is not None
    assert tuple(row) == ("failed", 3, "boom")
    assert would_reset == 1


async def _insert_legacy_scan(conn: Any, key: str, created_at: datetime) -> None:
    """An `xtb.scan` row from a calculator that wrote `energy`, not `energy_hartree`.

    A real shape rather than a synthetic one: `scan` was an `XtbTask` before
    `D-2026-08-16-the-physics-leaves-the-cache-stays` and is not one now, and
    `_CALC_TYPE_PROJECTORS` keeps its projector precisely because `calculation_results` is never
    pruned. So this is the row an upgrading deployment actually holds — a projector exists for it
    and cannot read it, which is the whole distinction under test.
    """
    await conn.execute(
        "INSERT INTO calculation_results "
        "(key, calc_type, calc_version, input_hash, params_hash, result, created_at) "
        "VALUES (%s, 'xtb.scan', 'v0', 'h', 'p', %s, %s)",
        (
            key,
            Jsonb(
                {
                    "smiles": "CCO",
                    "coordinate": "dihedral",
                    "points": [{"value": 0.0, "energy": -1.0}],
                }
            ),
            created_at,
        ),
    )


async def _insert_composite(
    conn: Any, job_id: str, at: datetime, job: str, payload_kind: str, result: dict[str, Any]
) -> None:
    """A durable composite, addressed by its route and *routed* by its `payload_kind`."""
    await conn.execute(
        "INSERT INTO job_records "
        "(job_id, connector, job, rationale, requested_by, summary, result, completed_at, "
        "payload_kind) VALUES (%s, 'calc', %s, 'r', 'tester', 's', %s, %s, %s)",
        (job_id, job, Jsonb(result), at, payload_kind),
    )


def _publishing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on the one shipped sink, so the real pass reaches the outbox rather than returning 0."""
    monkeypatch.setattr(settings, "result_sinks", "postgres", raising=False)


def test_a_row_no_projector_can_read_is_its_own_bucket_not_a_silent_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every row visited lands in exactly one bucket, and the dry run predicts the real pass.

    The regression: `outbox.enqueue_payload` swallows a projection failure and returns 0, and this
    module added that 0 to `queued` and touched nothing else — so a row written by an older
    calculator was seen, not queued, not skipped, and named nowhere. Measured on this corpus, the
    dry run reported `(seen=4, queued=3, skipped=1)` and the real pass `(seen=4, queued=2,
    skipped=1)`: an operator reading "4 row(s) seen, 2 queued, 1 skipped" over four rows was told a
    complete-looking story with one row missing from it.

    Both halves are asserted because either alone passes on the broken code: the partition alone
    would pass on a dry run that never projects, and dry==real alone would pass if both agreed on
    the *wrong* number.
    """
    _publishing(monkeypatch)

    async def _run() -> tuple[backfill.WalkCounts, backfill.WalkCounts]:
        await migrated_db_or_skip()
        at = datetime(2026, 1, 1, tzinfo=UTC)
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            await _insert_cached(conn, "k1", at, calc_type="pka")
            await _insert_cached(conn, "k2", at, calc_type="pka")
            await _insert_legacy_scan(conn, "k3", at)
            await _insert_cached(conn, "k4", at, calc_type="no-such-calculator")
            await conn.commit()

        dry = await backfill.backfill_cached(dry_run=True, batch=10)
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM result_publications")
            await conn.commit()
        real = await backfill.backfill_cached(dry_run=False, batch=10)
        return dry, real

    dry, real = asyncio.run(_run())

    for label, counts in (("dry", dry), ("real", real)):
        assert counts.seen == counts.queued + counts.skipped + counts.failed, (
            f"the {label} pass saw {counts.seen} row(s) and accounted for "
            f"{counts.queued + counts.skipped + counts.failed}: a row is in no bucket, so "
            f"'the backfill is done' rests on a number that cannot see what it left behind "
            f"({counts})"
        )
    assert dry == real, (
        f"a dry run must predict the pass it previews: dry={dry} real={real}. A preview that "
        f"routes without projecting agrees with the real pass only when every row is readable, "
        f"which is the one case it does not need to be run for"
    )
    assert real.failed == 1, f"the legacy `xtb.scan` row is the failure: {real}"
    assert real.skipped == 1, f"the unregistered calc_type is the skip: {real}"
    assert real.queued == 2


def test_a_second_pass_counts_a_row_it_already_queued_as_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency must not read as loss.

    The enqueue is `ON CONFLICT DO NOTHING`, so a second pass writes no rows — and `queued` used to
    be the *rows written*, so re-running a completed backfill reported every row as queued zero and
    skipped zero: three rows in no bucket, on a corpus with nothing wrong with it at all. `queued`
    counts rows whose records reached the outbox, which is what makes the partition hold on every
    pass rather than only the first.
    """
    _publishing(monkeypatch)

    async def _run() -> tuple[backfill.WalkCounts, backfill.WalkCounts]:
        await migrated_db_or_skip()
        at = datetime(2026, 1, 1, tzinfo=UTC)
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            for index in range(3):
                await _insert_cached(conn, f"twice-{index}", at, calc_type="pka")
            await conn.commit()
        first = await backfill.backfill_cached(dry_run=False, batch=10)
        second = await backfill.backfill_cached(dry_run=False, batch=10)
        return first, second

    first, second = asyncio.run(_run())
    assert first == second, f"a re-run covers the same rows: first={first} second={second}"
    assert second.queued == 3 and second.failed == 0
    assert second.seen == second.queued + second.skipped + second.failed


def test_the_jobs_walk_partitions_its_rows_and_counts_records_in_their_own_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All four buckets and the unit split, on the walk where a row really does decompose.

    `backfill_jobs` covers `job_records`, whose payloads are the composites — and a solvent screen
    projects into an aggregate plus one record per medium. While `queued` carried the record count,
    a walk over such a corpus reported **more queued than seen**, which is both wrong on its face
    and the reason `seen - skipped` could not be used to recover the missing bucket.

    Driven end to end rather than through a stub, on three real shapes: one that decomposes, one
    whose projector raises on an incomplete payload, and one nothing routes. What comes back is
    `(seen=3, queued=1, skipped=1, failed=1, records=3)` — the partition holds, `queued` is a row
    count that cannot exceed `seen`, and `records` is the number that is allowed to.
    """
    _publishing(monkeypatch)

    async def _run() -> tuple[backfill.WalkCounts, backfill.WalkCounts]:
        await migrated_db_or_skip()
        at = datetime(2026, 1, 1, tzinfo=UTC)
        async with db.connection(settings.postgres_dsn) as conn:
            await _reset(conn)
            # Decomposes: the aggregate plus one record per medium.
            await _insert_composite(
                conn,
                "j1",
                at,
                "compare_solvents",
                "SolventComparisonResult",
                {
                    "reactants": ["C=C", "C=CC=C"],
                    "products": ["C1CCCCC1"],
                    "method": "GFN2-xTB",
                    "temperature_k": 298.15,
                    "level": "standard",
                    "effects": [
                        {
                            "solvent": "thf",
                            "delta_e_kcal": -38.0,
                            "delta_h_kcal": -36.0,
                            "delta_g_kcal": -22.0,
                        },
                        {
                            "solvent": "toluene",
                            "delta_e_kcal": -37.5,
                            "delta_h_kcal": -35.5,
                            "delta_g_kcal": -24.8,
                        },
                    ],
                    "best_solvent": "toluene",
                    "spread_kcal": 2.8,
                    "uncertainty_kcal": 3.0,
                },
            )
            # Routes, and `_reaction` refuses it: the shape an older envelope can carry.
            await _insert_composite(
                conn,
                "j2",
                at,
                "compute_reaction_energy",
                "ReactionEnergyResult",
                {"method": "GFN2-xTB"},
            )
            # `x.unregistered-job` matches no projector prefix and states no payload kind.
            await _insert_job(conn, "j3", at)
            await conn.commit()

        dry = await backfill.backfill_jobs(dry_run=True, batch=10)
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM result_publications")
            await conn.commit()
        real = await backfill.backfill_jobs(dry_run=False, batch=10)
        return dry, real

    dry, real = asyncio.run(_run())

    assert dry == real, f"a dry run must predict the pass it previews: dry={dry} real={real}"
    assert real.seen == real.queued + real.skipped + real.failed, real
    assert (real.seen, real.queued, real.skipped, real.failed) == (3, 1, 1, 1), real
    assert real.queued <= real.seen, (
        f"{real.queued} queued over {real.seen} row(s) seen: the two are in different units"
    )
    assert real.records == 3, (
        f"one solvent screen is an aggregate plus two media: {real.records} record(s)"
    )


def test_both_entrypoints_of_one_walk_refuse_when_this_deployment_publishes_nowhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard existed on the operator's half and not on the chemist's.

    `enqueue` is a no-op with `CHEMCLAW_RESULT_SINKS` empty, so `republish_calculations` ran a full
    scan of two never-pruned tables, wrote nothing, and reported `calculations_seen: 10,
    calculations_queued: 0` — indistinguishable from a corpus with nothing left to publish, at the
    end of a job that can take hours, for the caller least able to diagnose it. Measured before the
    fix, with ten queueable rows in the cache: exactly that report, and zero rows in
    `result_publications`.

    Asserted as a pair rather than one test each, because the defect was the *asymmetry*: one walk,
    two entrypoints, and only one of them checked. `ResultSinkError` is already in
    `durable/publish._BAD_DATA_TYPES`, so the job fails fast rather than spending eight attempts on
    a setting no retry changes.
    """
    from chemclaw.cli import backfill_publications
    from chemclaw.connectors.results import workflows
    from chemclaw.connectors.results.specs import RepublishSpec
    from chemclaw.publish.registry import ResultSinkError

    monkeypatch.setattr(settings, "result_sinks", "", raising=False)

    async def _explode(**_kwargs: Any) -> None:
        raise AssertionError("a walk must not start when there is nowhere to publish")

    monkeypatch.setattr(workflows, "backfill_cached", _explode)
    monkeypatch.setattr(workflows, "backfill_jobs", _explode)

    with pytest.raises(ResultSinkError, match="CHEMCLAW_RESULT_SINKS"):
        asyncio.run(workflows._walk(RepublishSpec()))

    assert backfill_publications.main([]) == 1, (
        "the operator's half of the same walk has always refused; the two must not disagree"
    )
