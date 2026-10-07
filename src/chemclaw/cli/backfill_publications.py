"""Queue results that were computed before a results store was attached.

    python -m chemclaw.cli.backfill_publications --dry-run   # what would be queued
    python -m chemclaw.cli.backfill_publications             # queue it
    python -m chemclaw.cli.backfill_publications --requeue   # also retry rows that gave up

Idempotent (the outbox's identity index) and safe while live. Rows with no projector are skipped;
rows a projector cannot read are reported separately, since they need a code change. `--dry-run`
projects too, so its counts match.
"""

import argparse
import asyncio
import logging

from chemclaw.core.logging import configure_logging
from chemclaw.publish.backfill import backfill_cached, backfill_jobs, requeue_failed
from chemclaw.publish.registry import publishing_enabled

logger = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> int:
    """Do the walk and report it."""
    if not publishing_enabled() and not args.dry_run:
        logger.error(
            "no result sink is enabled (CHEMCLAW_RESULT_SINKS is empty), so nothing would be "
            "queued. Enable one, or pass --dry-run to see what a backfill would cover."
        )
        return 1

    if args.requeue:
        reset = await requeue_failed(dry_run=args.dry_run)
        logger.info(
            "%d retired publication(s) %s the queue",
            reset,
            "would return to" if args.dry_run else "returned to",
        )

    total_queued = total_failed = 0
    for label, walk in (("calculation cache", backfill_cached), ("job records", backfill_jobs)):
        counts = await walk(dry_run=args.dry_run, batch=args.batch)
        total_queued += counts.queued
        total_failed += counts.failed
        # Row counts and the record count on separate lines: one row can decompose into several
        # records.
        logger.info(
            "%s: %d row(s) seen = %d %s + %d skipped (no projector in this release) + "
            "%d unreadable by this release's projector",
            label,
            counts.seen,
            counts.queued,
            "would be queued" if args.dry_run else "queued",
            counts.skipped,
            counts.failed,
        )
        logger.info(
            "%s: those %d row(s) %s %d scientific record(s)",
            label,
            counts.queued,
            "would produce" if args.dry_run else "produced",
            counts.records,
        )
    if total_failed:
        # WARNING: unlike a skip this is a defect in this release that recurs every pass.
        # `outbox.project_payload` already logged each row with its traceback.
        logger.warning(
            "%d row(s) have a projector in this release that could not read them, counted above "
            "as unreadable and queued nowhere. Nothing is lost — neither source table is ever "
            "pruned, so a release that reads them re-runs this walk — but no pass will cover them "
            "until the projector changes. The calc refs are on the projection failures above.",
            total_failed,
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
