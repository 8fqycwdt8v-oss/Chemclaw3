"""Walking the stored corpus and queueing what has not been published yet.

One walk shared by the operator CLI and the `results` bundle's `republish_calculations` job, so
both cover exactly the same rows (it lives here because a connector may not import a CLI).

Every visited row lands in exactly one bucket: `seen == queued + skipped + failed` (see
`WalkCounts`). Rows with no projector in this release are skipped, not failed, since the cache
is never pruned and may hold results from retired calculators. A dry run runs the same
projection as the real pass and stops before the write, so the preview's counts match the run's.
`records` counts what was projected, not outbox rows (the outbox upserts once per enabled sink;
`chemclaw_results_queued_total` meters writes).
"""

from datetime import UTC, datetime
from typing import NamedTuple

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.publish import outbox
from chemclaw.publish.project import projector_for
from chemclaw.publish.record import Publication


class WalkCounts(NamedTuple):
    """What one walk did. `seen == queued + skipped + failed`, always.

    - `seen`: rows the walk visited.
    - `queued`: rows whose records reached the outbox (an upsert, so a re-run re-covers a row).
    - `skipped`: rows this release has no projector for at all.
    - `failed`: rows a projector exists for but could not read; these fail identically on every
      pass until code changes.
    - `records`: the scientific records the `queued` rows project into, a different unit (one
      solvent screen decomposes into an aggregate and its parts).
    """

    seen: int = 0
    queued: int = 0
    skipped: int = 0
    failed: int = 0
    records: int = 0


# The keyset cursor before the first row: earlier than any timestamp, with `""` sorting before every
# key, so one statement serves every page.
_WALK_START = (datetime.min.replace(tzinfo=UTC), "")


# Oldest first, so an interrupted run has made contiguous progress. `key` (the primary key) breaks
# ties on the non-unique `created_at`, so every row is fetched exactly once.
#
# Keyset rather than `OFFSET`, which re-scans every skipped row and makes the walk quadratic; the
# row-constructor comparison drives `calc_results_created_at_idx` directly and avoids a
# hand-expanded predicate's off-by-one.
_CACHED = """
    SELECT key, calc_type, calc_version, input_hash, params_hash, result, structure_id,
           compute_seconds, created_at
    FROM calculation_results
    WHERE (created_at, key) > (%s, %s)
    ORDER BY created_at, key
    LIMIT %s
"""

# The composites: `job_records.result` is the envelope's data, which has no cache row and no other
# route to a results store. Keyset-paginated on `(completed_at, job_id)` for `_CACHED`'s reasons.
_JOBS = """
    SELECT job_id, connector, job, result, calc_refs, requested_by, session_id, correlation_id,
           rationale, completed_at, payload_kind, note_id
    FROM job_records
    WHERE result <> '{}'::jsonb
      AND (completed_at, job_id) > (%s, %s)
    ORDER BY completed_at, job_id
    LIMIT %s
"""

# One definition of "retired", shared by the reset and by the count a dry run reports, so the two
# can never disagree about which rows the operator is being told about.
_RETIRED = "WHERE state = 'failed'"

_REQUEUE = f"""
    UPDATE result_publications
    SET state = 'pending', attempts = 0, last_error = ''
    {_RETIRED}
"""

_COUNT_RETIRED = f"SELECT count(*) FROM result_publications {_RETIRED}"


async def backfill_cached(*, dry_run: bool, batch: int) -> WalkCounts:
    """Walk the calculation cache. See `WalkCounts` for what the five numbers are."""
    seen = queued = skipped = failed = records = 0
    cursor_key: tuple[datetime, str] = _WALK_START
    while True:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(_CACHED, (*cursor_key, batch))
            rows = list(await cursor.fetchall())
        if not rows:
            return WalkCounts(seen, queued, skipped, failed, records)
        # Advance before the page is worked: an exception mid-page re-reads it next run, and the
        # upsert makes re-reading free.
        cursor_key = (rows[-1][8], rows[-1][0])
        for row in rows:
            seen += 1
            key, calc_type, calc_version, input_hash, params_hash = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
            )
            payload, structure_id, compute_seconds, created_at = row[5], row[6], row[7], row[8]
            if projector_for(calc_type) is None:
                skipped += 1
                continue
            projected = outbox.project_payload(
                calc_ref=key,
                calc_type=calc_type,
                payload=payload,
                calc_version=calc_version,
                input_hash=input_hash,
                params_hash=params_hash,
                structure_id=structure_id or "",
                compute_seconds=compute_seconds,
                computed_at=created_at,
            )
            if projected is None:
                # No second log line: `project_payload` has already logged the row, its calc_type
                # and the traceback at exception level. What this walk adds is the *count*.
                failed += 1
                continue
            queued += 1
            records += len(projected)
            if not dry_run:
                await outbox.enqueue(projected)


async def backfill_jobs(*, dry_run: bool, batch: int) -> WalkCounts:
    """Walk the durable job record. See `WalkCounts` for what the five numbers are."""
    seen = queued = skipped = failed = records = 0
    cursor_key: tuple[datetime, str] = _WALK_START
    while True:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(_JOBS, (*cursor_key, batch))
            rows = list(await cursor.fetchall())
        if not rows:
            return WalkCounts(seen, queued, skipped, failed, records)
        # See `backfill_cached` for why the cursor advances before the page is worked.
        cursor_key = (rows[-1][9], rows[-1][0])
        for row in rows:
            seen += 1
            job_id, connector, job, result, calc_refs = row[0], row[1], row[2], row[3], row[4]
            requested_by, session_id, correlation_id, rationale, completed_at = row[5:10]
            payload_kind = row[10] or ""
            note_id = row[11] or ""
            # `<connector>.<job>` addresses the row; `payload_kind` routes it, and an empty one
            # (older rows) falls back to prefix inference.
            calc_type = f"{connector}.{job}"
            if projector_for(calc_type, payload_kind) is None:
                skipped += 1
                continue
            projected = outbox.project_payload(
                calc_ref=job_id,
                calc_type=calc_type,
                payload_kind=payload_kind,
                payload=result,
                depends_on=list(calc_refs or []),
                computed_at=completed_at,
                publication=Publication(
                    actor=requested_by,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    job_id=job_id,
                    rationale=rationale,
                    note_id=note_id,
                ),
            )
            if projected is None:
                # See `backfill_cached`: the row is already logged where it was projected.
                failed += 1
                continue
            queued += 1
            records += len(projected)
            if not dry_run:
                await outbox.enqueue(projected)


async def requeue_failed(*, dry_run: bool = False) -> int:
    """Return retired rows to the queue. Returns how many were reset, or would be under `dry_run`.

    Retired rows are kept so that once the cause is fixed an operator can requeue them. `dry_run`
    counts instead of resetting, so a preview never clears the recorded errors. Defaulted because
    the durable republish job has no preview mode.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        if dry_run:
            cursor = await conn.execute(_COUNT_RETIRED)
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
        cursor = await conn.execute(_REQUEUE)
        await conn.commit()
        return int(cursor.rowcount)
