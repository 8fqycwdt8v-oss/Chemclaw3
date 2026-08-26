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
        reset = await requeue_failed()
        logger.info("returned %d retired publication(s) to the queue", reset)

    total_queued = 0
    for label, walk in (("calculation cache", backfill_cached), ("job records", backfill_jobs)):
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
