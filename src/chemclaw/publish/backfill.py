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

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.publish import outbox
from chemclaw.publish.project import projector_for
from chemclaw.publish.record import Publication

logger = logging.getLogger(__name__)


# Oldest first, so a run that is interrupted has made contiguous progress rather than a scatter.
_CACHED = """
    SELECT key, calc_type, calc_version, input_hash, params_hash, result, structure_id,
           compute_seconds, created_at
    FROM calculation_results
    ORDER BY created_at
    LIMIT %s OFFSET %s
"""

# The composites. `job_records.result` is the envelope's own data - the shape that has no cache row
# and therefore reaches a results store through no other path.
_JOBS = """
    SELECT job_id, connector, job, result, calc_refs, requested_by, session_id, correlation_id,
           rationale, completed_at
    FROM job_records
    WHERE result <> '{}'::jsonb
    ORDER BY completed_at
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
            calc_type = f"{connector}.{job}"
            if projector_for(calc_type) is None:
                skipped += 1
                continue
            if dry_run:
                queued += 1
                continue
            queued += await outbox.enqueue_payload(
                calc_ref=job_id,
                calc_type=calc_type,
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
