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
from datetime import datetime, timedelta

from pydantic import BaseModel
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
)

from chemclaw.config import settings
from chemclaw.ids import stable_hash
from chemclaw.logging import configure_logging
from chemclaw.temporal_client import connect
from workflows.audit_verify import AuditChainVerifyWorkflow
from workflows.eln_sync import ElnSyncWorkflow
from workflows.eval_drift import EvalDriftWorkflow
from workflows.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
)
from workflows.note_index import NoteReindexWorkflow
from workflows.retention import RetentionWorkflow

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
        "note-reindex",
        "retention",
        "audit-verify",
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
    # The derived note index only earns a Schedule where a hybrid retrieval leg actually reads it
    # (gap SCH-2). A graph-only deployment would otherwise pay to rebuild an index nothing queries.
    if settings.note_reindex_enabled:
        reindex_every = timedelta(minutes=settings.note_reindex_schedule_minutes)
        schedules.append(PlannedSchedule("note-reindex", NoteReindexWorkflow, reindex_every))
    # The integrity check only earns a Schedule where a durable audit sink exists to verify
    # (gap SCH-5); an offline/dev deployment has no chain and would alert on an empty table.
    if settings.audit_verify_enabled:
        verify_every = timedelta(minutes=settings.audit_verify_schedule_minutes)
        schedules.append(PlannedSchedule("audit-verify", AuditChainVerifyWorkflow, verify_every))
    # Retention only earns a Schedule where the deployment has stated a policy (gap SCH-1); an
    # unconfigured deployment must never start deleting records on a default it did not choose.
    if settings.retention_enabled:
        retention_every = timedelta(minutes=settings.retention_schedule_minutes)
        schedules.append(PlannedSchedule("retention", RetentionWorkflow, retention_every))
    return schedules


def _jitter(job: PlannedSchedule) -> timedelta:
    """A deterministic per-job offset inside its interval, so co-scheduled jobs do not collide.

    The three memory-synthesis jobs share one configured cadence and each re-scans the whole
    corpus, so without an offset they fire simultaneously against a single background worker
    (`replicas: 1`) and contend for the same reads (gap SCH-3). Temporal jitter is a *random*
    delay drawn per fire; this is instead a fixed per-schedule phase offset derived from the
    schedule id, which is stable across re-applies (so `apply_schedules` stays a reconcile, not a
    reshuffle) and spreads the jobs deterministically.

    Bounded to a fraction of the interval so a job never drifts into the next window.
    """
    span = job.interval * settings.schedule_jitter_fraction
    if not span:
        return timedelta(0)
    # A stable hash of the id, mapped into [0, span). `stable_hash` is the repo's one hashing
    # scheme (D-033), so this cannot drift from the ids it is derived from.
    bucket = int(stable_hash(job.schedule_id, chars=8), 16) % 10_000
    return span * (bucket / 10_000)


def _build_schedule(job: PlannedSchedule) -> Schedule:
    """Build the Temporal `Schedule` for one planned job (no-arg workflow on the bg queue)."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            job.workflow.run,  # type: ignore[attr-defined]
            id=f"{job.schedule_id}-scheduled",
            task_queue=settings.background_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=job.interval, offset=_jitter(job))],
        ),
        # SKIP, not the default BUFFER_ONE: every job here is a full re-scan or a full reindex, so
        # a run that overruns its interval must be allowed to finish rather than have the next fire
        # queue behind it. Buffering would let a slow corpus scan accumulate a backlog it can never
        # drain — the run it would buffer is redundant anyway, since the next fire re-scans
        # everything (gap SCH-3). The ELN sync is cursored, so a skipped fire loses nothing either.
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
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


class ScheduleHealth(BaseModel):
    """One periodic job's operational state, as an admin surface renders it (gap SCH-4).

    Read from Temporal's own schedule state rather than a mirrored table: Temporal is already the
    authority on when a Schedule fired and how often, and a second copy could only ever drift.

    `skipped_overlap` is the load signal worth watching — it counts fires dropped because the
    previous run was still going (the SKIP policy from gap SCH-3). A steadily climbing value means
    the job no longer fits inside its interval, which is the early warning that a corpus has
    outgrown its cadence.
    """

    schedule_id: str
    interval_seconds: float
    paused: bool = False
    last_run: datetime | None = None
    runs_total: int = 0
    skipped_overlap: int = 0
    running_now: int = 0
    note: str = ""


async def describe_schedules(client: Client | None = None) -> list[ScheduleHealth]:
    """Report every planned Schedule's health, in plan order.

    A planned Schedule that does not exist in Temporal is reported with a note, never omitted:
    "the job was never created" is precisely the failure this surface exists to show, and a silent
    omission is indistinguishable from a healthy quiet job.
    """
    connection = client if client is not None else await connect()
    health: list[ScheduleHealth] = []
    for job in planned_schedules():
        entry = ScheduleHealth(
            schedule_id=job.schedule_id,
            interval_seconds=job.interval.total_seconds(),
        )
        try:
            description = await connection.get_schedule_handle(job.schedule_id).describe()
        except Exception as exc:  # noqa: BLE001 - a lookup failure is reported, never raised
            entry.note = f"not found in Temporal ({type(exc).__name__}) — was it ever applied?"
            health.append(entry)
            continue
        info = description.info
        entry.paused = description.schedule.state.paused
        entry.runs_total = info.num_actions
        entry.skipped_overlap = info.num_actions_skipped_overlap
        entry.running_now = len(list(info.running_actions or []))
        recent = list(info.recent_actions or [])
        if recent:
            entry.last_run = recent[-1].started_at
        else:
            entry.note = "no run recorded yet"
        health.append(entry)
    return health


async def main() -> None:
    """Connect to Temporal and apply the periodic-job Schedules."""
    configure_logging()
    client = await connect()
    await apply_schedules(client)


if __name__ == "__main__":
    asyncio.run(main())
