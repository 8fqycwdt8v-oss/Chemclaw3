"""Entrypoint for `make schedules-apply`: apply the Temporal Schedules for the periodic jobs.

The Schedule definitions and the apply/prune logic are durable-layer library code — a Temporal
Schedule is Temporal's own durability primitive — and live in `chemclaw.durable.schedules`, which
`chemclaw.api.app` also imports at module scope for the `/schedules` health endpoint. This module
is only the CLI shim: `python -m chemclaw.cli.schedules` (what `make schedules-apply` runs)
connects to Temporal and applies the plan.

**It parses its command line, which it did not.** `asyncio.run(main())` ran unconditionally under
`if __name__ == "__main__"`, so every argument was discarded: `--help` and `--delete-everything`
alike *applied the Schedules* and exited 0. Asking a broker-mutating command what it does performed
it — the same defect `validate_kg`'s docstring records as fixed one directory over, with a
mutation on the other side of it.

**Applying stays the default, unlike its data-touching siblings**, and the asymmetry is
deliberate rather than an oversight. `erase_actor` and `rekey_campaigns` preview by default because
an operator types them by hand; this one is a container command in
`deploy/helm/chemclaw/templates/schedules-job.yaml`, invoked bare with no arguments, so a
preview-by-default would turn every deployment's Schedule Job into a no-op that exits 0 — the same
green-while-doing-nothing failure, moved rather than fixed. What an operator gets instead is
`--dry-run`, which reads the plan and touches no broker at all.
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

    `planned_schedules()` is pure by design ("no client, so a test can assert the set of jobs and
    their configured cadences without a live Temporal server"), and the prune set is
    `OWNED_SCHEDULE_IDS` minus the planned ids by the same arithmetic `_prune` uses. So the whole
    plan is derivable offline, which is what makes a dry run worth having: it answers "what will
    this deployment's configuration produce" without a broker in reach.

    The prune half is stated as *would delete if present* rather than as a deletion, because
    whether a stale Schedule exists is the one part of the plan only Temporal can answer.
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
