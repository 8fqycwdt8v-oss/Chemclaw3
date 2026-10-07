"""The corpus-wide backfill walk: every row seen exactly once, and skip vs. queue counted right.

`backfill_cached`/`backfill_jobs` page by keyset on `(created_at, key)` and `(completed_at,
job_id)`. The timestamps tie under concurrent workers or bulk imports, so the primary-key
tiebreaker must make a walk with a batch smaller than a run of ties see every row once. A keyset is
also correct under concurrent writes, which an `OFFSET` walk is not.
"""

import asyncio
from collections.abc import Iterator
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


@pytest.fixture(autouse=True, scope="module")
def _leave_the_corpus_as_it_was_found() -> Iterator[None]:
    """Empty the three tables again when this module finishes.

    Resetting on the way in protects this file but leaks its rows to later files, whose exact-roster
    assertions then fail depending on test order; `tests/pg.py` isolates runs, not files.
    """
    yield

    async def _clean() -> None:
        # Defensive rather than gated on `migrated_db_or_skip`: this runs in teardown, where a skip
        # would be raised at the wrong moment, and on a run with no database there is nothing to
        # clean and nothing to say about it.
        try:
            async with db.connection(settings.postgres_dsn) as conn:
                await _reset(conn)
        except Exception:
            return

    asyncio.run(_clean())


async def _insert_cached(conn: Any, key: str, created_at: datetime, calc_type: str = "pka") -> None:
    """A cached row whose payload actually projects, so it is a valid control for "was this row
    queued".
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
    """The queries break ties on a unique column and walk by keyset, not `OFFSET`.

    Offline and exact, so dropping either fails at once rather than via non-deterministic Postgres
    behaviour.
    """
    assert "ORDER BY created_at, key" in backfill._CACHED
    assert "ORDER BY completed_at, job_id" in backfill._JOBS
    assert "(created_at, key) > (%s, %s)" in backfill._CACHED
    assert "(completed_at, job_id) > (%s, %s)" in backfill._JOBS
    assert "OFFSET" not in backfill._CACHED and "OFFSET" not in backfill._JOBS


async def test_every_row_is_seen_exactly_once_even_when_many_share_a_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: a batch smaller than a run of tied `created_at` values must still see all.

    Every row here carries an unregistered `calc_type`, so the count under test is `seen` — the
    walk's own accounting of how many rows it visited — not anything projection-dependent.
    """
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


def test_a_row_arriving_behind_the_cursor_does_not_shift_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent insert older than the cursor does not shift the walk.

    With `OFFSET`, a row landing before the cursor shifts later pages, fetching one row twice and
    another never, silently. `calculation_results` is written by every worker while this long walk
    runs. Asserted on the sequence of keys visited, since a count can come out right regardless.
    """
    visited: list[str] = []
    intruded = False

    def _recording_project(**kwargs: Any) -> list[Any]:
        visited.append(str(kwargs["calc_ref"]))
        return [object()]

    async def _intruding_enqueue(records: list[Any]) -> int:
        """The intrusion rides on the walk's own `await`, so it is committed before the next page is
        read; a background task would race the read.
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


async def test_every_job_is_seen_exactly_once_even_when_many_share_a_timestamp() -> None:
    """`backfill_jobs`'s half of the same regression, over `job_records`/`completed_at`."""
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


async def test_a_row_with_a_registered_projector_is_queued_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with a registered projector is queued, not skipped.

    The projection is stubbed: this module owns the routing decision; projection itself is proven in
    `test_publish_outbox.py` and `test_publish_project*.py`.
    """

    def _one_record(**_kwargs: Any) -> list[Any]:
        return [object()]

    async def _fake_enqueue(records: list[Any]) -> int:
        return len(records)

    monkeypatch.setattr(outbox, "project_payload", _one_record)
    monkeypatch.setattr(outbox, "enqueue", _fake_enqueue)

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


async def test_dry_run_counts_without_calling_the_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dry_run=True` is a read-only preview: no row reaches the outbox write.

    It does run the projection, so the preview's numbers match the real pass; the guarded line is
    `enqueue`.
    """

    async def _explode(records: list[Any]) -> int:
        raise AssertionError("dry_run must not enqueue anything")

    monkeypatch.setattr(outbox, "enqueue", _explode)

    await migrated_db_or_skip()
    async with db.connection(settings.postgres_dsn) as conn:
        await _reset(conn)
        await _insert_cached(conn, "known", datetime(2026, 1, 1, tzinfo=UTC), calc_type="pka")
        await conn.commit()

    counts = await backfill.backfill_cached(dry_run=True, batch=10)

    assert (counts.seen, counts.queued, counts.skipped, counts.failed) == (1, 1, 0, 0)


async def test_requeue_failed_returns_failed_rows_to_pending() -> None:
    """An operator's fix (rotated credential, applied DDL) is a resource nothing else recovers."""
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


def test_a_requeue_dry_run_counts_the_retired_rows_without_touching_them() -> None:
    """A `--requeue` dry run counts retired rows without resetting them.

    Resetting would clear the attempt budget and recorded errors of every dead-lettered publication
    during a preview. Driven through the CLI's `main`, where the requeue branch lives.
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

    `calculation_results` is never pruned, so upgrading deployments hold such rows: a projector
    exists for them and cannot read them.
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

    `outbox.enqueue_payload` swallows a projection failure and returns 0, so an unreadable row must
    be counted in its own bucket. Both halves are needed: the partition alone passes on a dry run
    that never projects, and dry == real alone passes if both are wrong.
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
    """A second pass counts a row it already queued as covered.

    The enqueue is `ON CONFLICT DO NOTHING`, so `queued` counts rows whose records reached the
    outbox, not rows written, and the partition holds on every pass.
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
    """The jobs walk partitions its rows and counts records in their own unit.

    A composite such as a solvent screen projects into several records, so `queued` is a row count
    that cannot exceed `seen` and `records` is the one allowed to. Driven end to end on a
    decomposing shape, a raising projector and an unrouted one.
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
    """Both entrypoints of one walk refuse when this deployment publishes nowhere.

    With `CHEMCLAW_RESULT_SINKS` empty, a full scan would write nothing and report what looks like
    nothing left to publish. Asserted as a pair because one walk has two entrypoints.
    `ResultSinkError` is in `_BAD_DATA_TYPES`, so the job fails fast.
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


async def test_the_jobs_walk_carries_the_note_the_run_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The jobs walk carries the note the run produced
    (`D-2026-09-13-a-publication-carries-the-link-the-system-already-holds`).

    A first-time sink gets its whole history through here. A second row with no note is walked
    beside it, so a hard-coded empty string fails.
    """
    _publishing(monkeypatch)
    captured: list[Any] = []

    def _capture(**kwargs: Any) -> list[Any]:
        captured.append(kwargs["publication"])
        return [object()]

    await migrated_db_or_skip()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    async with db.connection(settings.postgres_dsn) as conn:
        await _reset(conn)
        for job_id, note_id in (("n1", "note-from-the-run"), ("n2", "")):
            await _insert_composite(
                conn,
                job_id,
                at,
                "compute_reaction_energy",
                "ReactionEnergyResult",
                {"method": "GFN2-xTB"},
            )
            await conn.execute(
                "UPDATE job_records SET note_id = %s WHERE job_id = %s", (note_id, job_id)
            )
        await conn.commit()
    monkeypatch.setattr("chemclaw.publish.outbox.project_payload", _capture)
    await backfill.backfill_jobs(dry_run=True, batch=10)

    assert [publication.note_id for publication in captured] == ["note-from-the-run", ""], (
        "the walk that re-publishes a deployment's whole history drops the note link on every row"
    )
