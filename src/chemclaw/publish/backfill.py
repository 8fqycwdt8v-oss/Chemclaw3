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

**Three walks, because there are three durable records and every published shape is in one of
them.** A primitive is a `calculation_results` row, a job composite is a `job_records` row, and a
*tool* composite is a `result_composites` row — the last of those exists because it is the one
shape written to neither of the first two (`publish/composites.py`), so until it did, a tool
composite that failed its enqueue was gone.
"""

import logging

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.publish import composites, outbox
from chemclaw.publish.project import projector_for
from chemclaw.publish.record import Publication

logger = logging.getLogger(__name__)


# Oldest first, so a run that is interrupted has made contiguous progress rather than a scatter.
#
# `key` breaks ties on `created_at`, which is not unique: microsecond resolution lets concurrent
# calculator workers share a value, and `_UPSERT` stamps `created_at = now()` on every row of a
# bulk import in one transaction, giving each of them the identical instant. Postgres does not
# guarantee LIMIT/OFFSET's relative order for tied rows is stable across the separate queries that
# fetch consecutive pages, so a tied row could land on a page boundary and never be fetched by
# either page — silently never queued, with no error and no `skipped` increment. `key` is this
# table's primary key, so `(created_at, key)` is a total order and every row is fetched once.
_CACHED = """
    SELECT key, calc_type, calc_version, input_hash, params_hash, result, structure_id,
           compute_seconds, created_at
    FROM calculation_results
    ORDER BY created_at, key
    LIMIT %s OFFSET %s
"""

# The composites. `job_records.result` is the envelope's own data - the shape that has no cache row
# and therefore reaches a results store through no other path.
#
# `job_id` breaks ties on `completed_at` for the same reason `key` does above — see `_CACHED`.
_JOBS = """
    SELECT job_id, connector, job, result, calc_refs, requested_by, session_id, correlation_id,
           rationale, completed_at, payload_kind
    FROM job_records
    WHERE result <> '{}'::jsonb
    ORDER BY completed_at, job_id
    LIMIT %s OFFSET %s
"""

_REQUEUE = """
    UPDATE result_publications
    SET state = 'pending', attempts = 0, last_error = ''
    WHERE state = 'failed'
"""


async def backfill_cached(*, dry_run: bool, batch: int) -> tuple[int, int, int]:
    """Walk the calculation cache. Returns `(seen, queued, skipped)`."""
    seen = queued = skipped = 0
    offset = 0
    while True:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(_CACHED, (batch, offset))
            rows = list(await cursor.fetchall())
        if not rows:
            return seen, queued, skipped
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
        offset += batch


async def backfill_jobs(*, dry_run: bool, batch: int) -> tuple[int, int, int]:
    """Walk the durable job record. Returns `(seen, queued, skipped)`."""
    seen = queued = skipped = 0
    offset = 0
    while True:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(_JOBS, (batch, offset))
            rows = list(await cursor.fetchall())
        if not rows:
            return seen, queued, skipped
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
        offset += batch


async def backfill_composites(*, dry_run: bool, batch: int) -> tuple[int, int, int]:
    """Walk the tool composites. Returns `(seen, queued, skipped)`.

    The third walk, and the reason it exists is that the first two cannot reach this shape.
    `backfill_cached` recovers `calculation_results` and `backfill_jobs` recovers `job_records`; a
    tool composite is written to neither, because its key would name its own output. Before
    `result_composites` (`publish/composites.py`) that made a tool composite whose enqueue failed —
    or one computed before a sink was attached — permanently unrecoverable, while the two shapes
    beside it were both replayable.

    Same shape as its two siblings deliberately, down to the `projector_for` pre-filter that keeps
    an unprojectable row a `skipped` count rather than an abort. `payload_kind` is what routes a
    composite: no projector prefix matches a `<connector>.<tool>` route.
    """
    seen = queued = skipped = 0
    offset = 0
    while True:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(composites.WALK, (batch, offset))
            rows = list(await cursor.fetchall())
        if not rows:
            return seen, queued, skipped
        for row in rows:
            seen += 1
            calc_ref, calc_type, payload_kind, input_hash = row[0], row[1], row[2], row[3]
            payload, created_at = row[4], row[5]
            if projector_for(calc_type, payload_kind) is None:
                skipped += 1
                continue
            if dry_run:
                queued += 1
                continue
            queued += await outbox.enqueue_payload(
                calc_ref=calc_ref,
                calc_type=calc_type,
                payload_kind=payload_kind,
                payload=payload,
                input_hash=input_hash,
                computed_at=created_at,
            )
        offset += batch


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
