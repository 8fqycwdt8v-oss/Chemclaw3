"""Walking the stored corpus and queueing what has not been published yet.

**Why this is not in `cli/`, where it started.** Two callers need it — an operator running
`python -m chemclaw.cli.backfill_publications`, and a chemist launching the `results` bundle's
`republish_calculations` job — and a connector may not import a CLI module. That is not a
formality: `tests/test_layering.py` caught the inversion, and the rule behind it is the one
`cli/schedules.py` already records, that a terminal entrypoint is a thin `main()` over an
implementation that lives in the layer that owns the work.

Keeping one walk rather than two is the point. An operator and a chemist must cover exactly the
same rows; two implementations that agreed today would diverge on the next table added to either
store.

**Rows this release has no projector for are skipped, not failed.** `calculation_results` is never
pruned, so a deployment legitimately holds results from calculators that no longer ship, and a walk
that aborted on the first one would never reach the rest.
"""

import logging
from datetime import UTC, datetime

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.publish import outbox
from chemclaw.publish.project import projector_for
from chemclaw.publish.record import Publication

logger = logging.getLogger(__name__)

# The keyset cursor before the first row: earlier than any stored timestamp, and the empty string
# sorts before every key and job id. A sentinel rather than a second "first page" statement,
# because two statements for one walk is where the projection and the ordering drift apart.
_WALK_START = (datetime.min.replace(tzinfo=UTC), "")


# Oldest first, so a run that is interrupted has made contiguous progress rather than a scatter.
#
# `key` breaks ties on `created_at`, which is not unique: microsecond resolution lets concurrent
# calculator workers share a value, and `_UPSERT` stamps `created_at = now()` on every row of a
# bulk import in one transaction, giving each of them the identical instant. `key` is this table's
# primary key, so `(created_at, key)` is a total order and every row is fetched exactly once.
#
# **Keyset, not `OFFSET`.** `OFFSET n` makes the server produce and discard the first `n` rows on
# every page, so the walk is O(n²/batch): measured on 500 000 rows, `LIMIT 1000 OFFSET 0` is
# 1.4 ms and `LIMIT 1000 OFFSET 400000` is **388.7 ms** — 500k rows in 1 000-row pages is ~90 s of
# pure skipping, and `docs/planning/BACKLOG.md` asks an operator to run exactly this against a
# populated deployment. The row comparison below is the same total order expressed as a *predicate*
# — `calc_results_created_at_idx` already exists — so every page costs what page 1 costs.
#
# The tuple comparison is one predicate rather than the three-way `a > x OR (a = x AND b > y)`
# expansion because Postgres can drive a composite index scan from a row constructor directly, and
# because a hand-expanded version is where an off-by-one silently drops or repeats a row.
_CACHED = """
    SELECT key, calc_type, calc_version, input_hash, params_hash, result, structure_id,
           compute_seconds, created_at
    FROM calculation_results
    WHERE (created_at, key) > (%s, %s)
    ORDER BY created_at, key
    LIMIT %s
"""

# The composites. `job_records.result` is the envelope's own data - the shape that has no cache row
# and therefore reaches a results store through no other path.
#
# `job_id` breaks ties on `completed_at`, and the walk is keyset-paginated, both for the reasons
# `_CACHED` states — the 388.7 ms page-400 measurement above is this table's.
_JOBS = """
    SELECT job_id, connector, job, result, calc_refs, requested_by, session_id, correlation_id,
           rationale, completed_at, payload_kind
    FROM job_records
    WHERE result <> '{}'::jsonb
      AND (completed_at, job_id) > (%s, %s)
    ORDER BY completed_at, job_id
    LIMIT %s
"""

_REQUEUE = """
    UPDATE result_publications
    SET state = 'pending', attempts = 0, last_error = ''
    WHERE state = 'failed'
"""


async def backfill_cached(*, dry_run: bool, batch: int) -> tuple[int, int, int]:
    """Walk the calculation cache. Returns `(seen, queued, skipped)`."""
    seen = queued = skipped = 0
    cursor_key: tuple[datetime, str] = _WALK_START
    while True:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(_CACHED, (*cursor_key, batch))
            rows = list(await cursor.fetchall())
        if not rows:
            return seen, queued, skipped
        # Advance before the page is worked: the cursor is the *last row read*, so an exception
        # mid-page re-reads that page on the next run rather than skipping it, and `enqueue_payload`
        # is an upsert on the calc ref, so re-reading costs nothing.
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
            if dry_run:
                queued += 1
                continue
            queued += await outbox.enqueue_payload(
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


async def backfill_jobs(*, dry_run: bool, batch: int) -> tuple[int, int, int]:
    """Walk the durable job record. Returns `(seen, queued, skipped)`."""
    seen = queued = skipped = 0
    cursor_key: tuple[datetime, str] = _WALK_START
    while True:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(_JOBS, (*cursor_key, batch))
            rows = list(await cursor.fetchall())
        if not rows:
            return seen, queued, skipped
        # See `backfill_cached` for why the cursor advances before the page is worked.
        cursor_key = (rows[-1][9], rows[-1][0])
        for row in rows:
            seen += 1
            job_id, connector, job, result, calc_refs = row[0], row[1], row[2], row[3], row[4]
            requested_by, session_id, correlation_id, rationale, completed_at = row[5:10]
            payload_kind = row[10] or ""
            # `<connector>.<job>` is a *route*, and no projector prefix matches one — so before
            # `payload_kind` existed this skipped every composite in the table. It is still passed
            # as the `calc_type` because that is what the row is addressed by; `payload_kind` is
            # what routes it, and an empty one (a row written before migration 055) falls back to
            # the prefix inference exactly as it did before.
            calc_type = f"{connector}.{job}"
            if projector_for(calc_type, payload_kind) is None:
                skipped += 1
                continue
            if dry_run:
                queued += 1
                continue
            queued += await outbox.enqueue_payload(
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
                ),
            )


async def requeue_failed() -> int:
    """Return retired rows to the queue. Returns how many were reset.

    A row that spent its attempt budget is kept rather than deleted, precisely so this is possible:
    once the cause is fixed — the site ran the DDL, the credential was rotated — an operator puts
    them back rather than re-deriving them.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(_REQUEUE)
        await conn.commit()
        return int(cursor.rowcount)
