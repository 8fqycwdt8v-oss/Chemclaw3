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


def planned_schedules() -> list[PlannedSchedule]:
    """The Schedules this script maintains — the ELN sync plus the three memory jobs.

    Pure and side-effect-free (no client), so a test can assert the set of jobs and their
    configured cadences without a live Temporal server.
    """
    eln_every = timedelta(minutes=settings.eln_sync_schedule_minutes)
    memory_every = timedelta(minutes=settings.memory_synthesis_schedule_minutes)
    return [
        PlannedSchedule("eln-sync", ElnSyncWorkflow, eln_every),
        PlannedSchedule("campaign-synthesis", CampaignSynthesisWorkflow, memory_every),
        PlannedSchedule("playbook-distillation", PlaybookDistillationWorkflow, memory_every),
        PlannedSchedule("optimization-campaign", OptimizationCampaignWorkflow, memory_every),
    ]


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


async def apply_schedules(client: Client, jobs: Sequence[PlannedSchedule] | None = None) -> None:
    """Apply every planned Schedule idempotently against `client`."""
    for job in jobs if jobs is not None else planned_schedules():
        action = await _apply(client, job)
        logger.info(
            "%s schedule %s (every %s) -> %s",
            action,
            job.schedule_id,
            job.interval,
            job.workflow.__name__,
        )


async def main() -> None:
    """Connect to Temporal and apply the periodic-job Schedules."""
    configure_logging()
    client = await connect()
    await apply_schedules(client)


if __name__ == "__main__":
    asyncio.run(main())
