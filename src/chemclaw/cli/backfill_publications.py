"""Queue results that were computed before a results store was attached.

**The gap this closes.** Publishing hooks a calculation as it completes, so attaching a sink to a
deployment that has been running for a year would publish only what it computes from that moment
on — while `calculation_results` and `job_records` hold everything before it, and neither is ever
pruned. That corpus is the more valuable half.

    python -m chemclaw.cli.backfill_publications --dry-run   # what would be queued
    python -m chemclaw.cli.backfill_publications             # queue it
    python -m chemclaw.cli.backfill_publications --requeue   # also retry rows that gave up

Safe to run twice: the outbox's identity index makes a second pass a no-op. Safe to run while the
system is live: it writes to the same queue the hooks do, and the drain does not care which put a
row there.

**Rows this release has no projector for are skipped, not failed.** A deployment legitimately holds
results from calculators that no longer ship, and a backfill that aborted on the first one would
never reach the rest.
"""

import argparse
import asyncio
import logging

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging
from chemclaw.publish import outbox
from chemclaw.publish.project import projector_for
from chemclaw.publish.record import Publication
from chemclaw.publish.registry import publishing_enabled

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


async def _backfill_cached(*, dry_run: bool, batch: int) -> tuple[int, int, int]:
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


async def _backfill_jobs(*, dry_run: bool, batch: int) -> tuple[int, int, int]:
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


async def _requeue_failed() -> int:
    """Return retired rows to the queue. Returns how many were reset.

    A row that spent its attempt budget is kept rather than deleted, precisely so this is possible:
    once the cause is fixed — the site ran the DDL, the credential was rotated — an operator puts
    them back rather than re-deriving them.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(_REQUEUE)
        await conn.commit()
        return int(cursor.rowcount)


async def _run(args: argparse.Namespace) -> int:
    """Do the walk and report it."""
    if not publishing_enabled() and not args.dry_run:
        logger.error(
            "no result sink is enabled (CHEMCLAW_RESULT_SINKS is empty), so nothing would be "
            "queued. Enable one, or pass --dry-run to see what a backfill would cover."
        )
        return 1

    if args.requeue:
        reset = await _requeue_failed()
        logger.info("returned %d retired publication(s) to the queue", reset)

    total_queued = 0
    for label, walk in (("calculation cache", _backfill_cached), ("job records", _backfill_jobs)):
        seen, queued, skipped = await walk(dry_run=args.dry_run, batch=args.batch)
        total_queued += queued
        logger.info(
            "%s: %d row(s) seen, %d %s, %d skipped (no projector in this release)",
            label,
            seen,
            queued,
            "would be queued" if args.dry_run else "queued",
            skipped,
        )
    if args.dry_run:
        logger.info("dry run: nothing was written. %d row(s) would be queued.", total_queued)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the backfill."""
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.backfill_publications",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be queued, write nothing"
    )
    parser.add_argument(
        "--requeue",
        action="store_true",
        help="also return publications that exhausted their attempts to the queue",
    )
    parser.add_argument(
        "--batch", type=int, default=500, help="rows read per round trip (default: 500)"
    )
    args = parser.parse_args(argv)
    configure_logging()
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
