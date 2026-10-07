"""Entrypoint for `make schedules-apply`: apply the Temporal Schedules for the periodic jobs.

A CLI shim over `chemclaw.durable.schedules`, which holds the definitions and apply/prune logic.

Applying is the default (unlike `erase_actor` or `rekey_campaigns`) because the Helm schedules Job
runs it bare; a preview-by-default would make that Job a silent no-op. `--dry-run` prints the
plan without touching the broker.
"""

import argparse
import asyncio
import logging
import sys

from chemclaw.core.logging import configure_logging
from chemclaw.core.temporal_client import connect
from chemclaw.durable.schedules import (
    OWNED_SCHEDULE_IDS,
    apply_schedules,
    planned_schedules,
)

logger = logging.getLogger(__name__)


def _print_plan() -> None:
    """Print what an apply would do, computed without connecting to anything.

    `planned_schedules()` is pure and the prune set is `OWNED_SCHEDULE_IDS` minus the planned ids,
    so the whole plan is derivable offline. Prunes are reported as "would delete if present", since
    only Temporal knows whether a stale Schedule exists.
    """
    plan = planned_schedules()
    print(f"{len(plan)} schedule(s) planned by this configuration:")
    for job in plan:
        print(f"  apply  {job.schedule_id} (every {job.interval}) -> {job.workflow.__name__}")
    stale = sorted(OWNED_SCHEDULE_IDS - {job.schedule_id for job in plan})
    print(f"{len(stale)} owned schedule id(s) not planned, deleted if they exist in Temporal:")
    for schedule_id in stale:
        print(f"  prune  {schedule_id}")
    print("dry run: nothing was applied and no connection to Temporal was made.")


async def _apply() -> None:
    """Connect to Temporal and apply the periodic-job Schedules."""
    client = await connect()
    await apply_schedules(client)


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, then apply the plan or print it."""
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.schedules",
        description="Create, update and prune the Temporal Schedules for the periodic background "
        "jobs. Idempotent: re-running updates each Schedule in place.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan this configuration produces and apply nothing. Reads no broker.",
    )
    args = parser.parse_args(argv)
    configure_logging()
    if args.dry_run:
        _print_plan()
        return 0
    asyncio.run(_apply())
    return 0


if __name__ == "__main__":
    sys.exit(main())
