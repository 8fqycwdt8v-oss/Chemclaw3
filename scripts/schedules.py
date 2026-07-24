"""Create/update the Temporal Schedules that drive the periodic background jobs.

The ELN sync and the three memory-synthesis workflows are worker-registered but only run
when something fires them. That "something" is a Temporal Schedule — durability lives in
Temporal, not host cron (D-006 reasoning) — created here and applied idempotently by
`make schedules-apply`, so re-running updates each Schedule in place rather than erroring.

Intervals come from config (`*_schedule_minutes`). Every Schedule targets the
`background-jobs` queue. The ELN sync is self-cursoring (it loads/stores its high-water mark
in `sync_cursors`), so its Schedule passes no argument; the memory-synthesis jobs re-scan
the whole corpus and carry no state either. `planned_schedules()` is the pure, testable list
of what will be applied; `main()` connects and applies it.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleUpdate,
)

from chemclaw.config import settings
from chemclaw.logging import configure_logging
from chemclaw.temporal_client import connect
from workflows.eln_sync import ElnSyncWorkflow
from workflows.eval_drift import EvalDriftWorkflow
from workflows.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedSchedule:
    """One Schedule to apply: its stable id, the workflow it fires, and how often."""

    schedule_id: str
    workflow: type
    interval: timedelta


# Every Schedule id this script has ever owned — the prune namespace. Pruning must only
# ever delete this script's own Schedules, so the namespace is a fixed explicit set (never
# a prefix match against a shared Temporal namespace). `test_schedules.py` asserts the plan
# stays inside this set, so a new planned job that forgets to register here fails a test.
OWNED_SCHEDULE_IDS = frozenset(
    {
        "eln-sync",
        "campaign-synthesis",
        "playbook-distillation",
        "optimization-campaign",
        "eval-drift",
    }
)


def planned_schedules() -> list[PlannedSchedule]:
    """The Schedules this script maintains — the ELN sync plus the three memory jobs.

    Pure and side-effect-free (no client), so a test can assert the set of jobs and their
    configured cadences without a live Temporal server.
    """
    eln_every = timedelta(minutes=settings.eln_sync_schedule_minutes)
    memory_every = timedelta(minutes=settings.memory_synthesis_schedule_minutes)
    schedules = [
        PlannedSchedule("eln-sync", ElnSyncWorkflow, eln_every),
        PlannedSchedule("campaign-synthesis", CampaignSynthesisWorkflow, memory_every),
        PlannedSchedule("playbook-distillation", PlaybookDistillationWorkflow, memory_every),
        PlannedSchedule("optimization-campaign", OptimizationCampaignWorkflow, memory_every),
    ]
    # The drift check is opt-in (plan F10-F2): it only earns a Schedule where a committed baseline
    # is maintained, so an unconfigured deployment does not fire an eval it has no baseline for.
    if settings.eval_drift_enabled:
        drift_every = timedelta(minutes=settings.eval_drift_schedule_minutes)
        schedules.append(PlannedSchedule("eval-drift", EvalDriftWorkflow, drift_every))
    return schedules


def _build_schedule(job: PlannedSchedule) -> Schedule:
    """Build the Temporal `Schedule` for one planned job (no-arg workflow on the bg queue)."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            job.workflow.run,  # type: ignore[attr-defined]
            id=f"{job.schedule_id}-scheduled",
            task_queue=settings.background_task_queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=job.interval)]),
    )


async def _apply(client: Client, job: PlannedSchedule) -> str:
    """Create the Schedule, or update it in place if it already exists. Returns the action taken."""
    schedule = _build_schedule(job)
    try:
        await client.create_schedule(job.schedule_id, schedule)
        return "created"
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(job.schedule_id)
        await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))
        return "updated"


async def _prune(client: Client, planned_ids: set[str]) -> None:
    """Delete script-owned Schedules that exist in Temporal but are no longer planned.

    Without this, a job removed from the plan (e.g. `eval-drift` after
    `eval_drift_enabled` is switched off) keeps firing forever. Only ids inside
    `OWNED_SCHEDULE_IDS` are ever deleted, so Schedules created by anything else
    in the namespace are untouched.
    """
    stale = OWNED_SCHEDULE_IDS - planned_ids
    if not stale:
        return
    async for listing in await client.list_schedules():
        if listing.id in stale:
            await client.get_schedule_handle(listing.id).delete()
            logger.info("deleted stale schedule %s (no longer planned)", listing.id)


async def apply_schedules(client: Client, jobs: Sequence[PlannedSchedule] | None = None) -> None:
    """Apply every planned Schedule idempotently against `client`, then prune stale ones.

    Pruning makes a re-apply declarative: the Schedules in Temporal end up exactly the
    planned set (within this script's owned id namespace), not a monotone accumulation.
    """
    plan = list(jobs) if jobs is not None else planned_schedules()
    for job in plan:
        action = await _apply(client, job)
        logger.info(
            "%s schedule %s (every %s) -> %s",
            action,
            job.schedule_id,
            job.interval,
            job.workflow.__name__,
        )
    await _prune(client, {job.schedule_id for job in plan})


async def main() -> None:
    """Connect to Temporal and apply the periodic-job Schedules."""
    configure_logging()
    client = await connect()
    await apply_schedules(client)


if __name__ == "__main__":
    asyncio.run(main())
