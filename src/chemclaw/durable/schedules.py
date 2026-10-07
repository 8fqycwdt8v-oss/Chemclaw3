"""Create/update the Temporal Schedules that drive the periodic background jobs.

Background workflows run only when a Temporal Schedule fires them (durability lives in Temporal,
not host cron). `planned_schedules()` is the pure list of what to apply; `apply_schedules()`
reconciles it idempotently against a live client (`make schedules-apply`), and
`describe_schedules()` backs the front door's `/schedules` health endpoint. Every Schedule targets
`background-jobs` and passes no argument: the jobs are self-cursoring or full re-scans.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from pydantic import BaseModel
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionExecution,
    ScheduleActionExecutionStartWorkflow,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleInfo,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)

from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.temporal_client import connect
from chemclaw.durable.artifact_eviction import ArtifactEvictionWorkflow
from chemclaw.durable.check_in import CheckInWorkflow
from chemclaw.durable.commitment_sync import CommitmentSyncWorkflow
from chemclaw.durable.corpus_sync import ReactionCorpusWorkflow, corpus_sources
from chemclaw.durable.digest import DigestWorkflow
from chemclaw.durable.document_sync import DocumentShareSyncWorkflow, share_sources
from chemclaw.durable.eln_sync import ElnSyncWorkflow
from chemclaw.durable.eval_drift import EvalDriftWorkflow
from chemclaw.durable.label_sync import ReactionLabelWorkflow
from chemclaw.durable.note_index import NoteReindexWorkflow
from chemclaw.durable.observation_jobs import ObservationSynthesisWorkflow
from chemclaw.durable.orphaned_waits import OrphanedWaitsWorkflow
from chemclaw.durable.publish_results import PublishResultsWorkflow
from chemclaw.durable.retention import ExhibitPushPruneWorkflow, RetentionWorkflow
from chemclaw.ingest.sources.registry import (
    active_commitment_sources,
    active_ingest_source_names,
)
from chemclaw.publish.registry import publishing_enabled

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedSchedule:
    """One Schedule to apply: its stable id, the workflow it fires, and how often."""

    schedule_id: str
    workflow: type
    interval: timedelta


# Every Schedule id this module has ever owned — the prune namespace. Pruning deletes only these
# ids (never a prefix match in a shared namespace); `test_schedules.py` asserts the plan stays
# inside this set.
OWNED_SCHEDULE_IDS = frozenset(
    {
        "eln-sync",
        "campaign-synthesis",
        "playbook-distillation",
        "optimization-campaign",
        "eval-drift",
        "note-reindex",
        "retention",
        # Retired job. Kept so the next apply prunes a still-live Schedule; remove once every
        # deployment has applied once.
        "audit-verify",
        "digest",
        "agent-check-in",
        "artifact-eviction",
        "observations",
        "document-sync",
        "reaction-labels",
        "reaction-corpus",
        # Conditional job: listed so dropping its source from `CHEMCLAW_DATA_SOURCES` prunes the
        # Schedule instead of leaving it firing.
        "commitment-mirror",
        # Conditional job: listed so clearing `CHEMCLAW_RESULT_SINKS` prunes the Schedule.
        "result-publish",
        "orphaned-waits",
        "exhibit-pushes",
    }
)


def _retention_windows_are_set() -> bool:
    """Whether any table has a retention window, i.e. whether the sweep would delete anything.

    Derived from every `retention_*_days` setting rather than a list, so a new window counts with no
    edit; `tests/test_schedules.py` holds that against the sweep's own map. Kept here rather than
    imported from `retention` so the plan avoids the workflow module's imports.
    """
    return any(getattr(settings, name) for name in retention_window_fields())


def retention_window_fields() -> list[str]:
    """Every `retention_*_days` setting — each one a table's window, in days, 0 meaning off."""
    return sorted(
        name
        for name in type(settings).model_fields
        if name.startswith("retention_") and name.endswith("_days")
    )


def planned_schedules() -> list[PlannedSchedule]:
    """The Schedules this module maintains.

    Pure and side-effect-free (no client), so a test can assert the jobs and cadences without a live
    Temporal server. No Schedule mines knowledge on a timer: the campaign, playbook and
    optimization miners run on demand. What runs on a timer is ingestion, indexing, eviction and
    retention.
    """
    eln_every = timedelta(minutes=settings.eln_sync_schedule_minutes)
    schedules: list[PlannedSchedule] = []
    # Only where there is an ELN to sync. Asked of the manifests, because `CHEMCLAW_DATA_SOURCES` is
    # already the enable switch; a second flag could only restate or contradict it.
    if active_ingest_source_names():
        schedules.append(PlannedSchedule("eln-sync", ElnSyncWorkflow, eln_every))
    # Opt-in: only where a committed baseline is maintained, so a deployment never fires an eval it
    # has no baseline for.
    if settings.eval_drift_enabled:
        drift_every = timedelta(minutes=settings.eval_drift_schedule_minutes)
        schedules.append(PlannedSchedule("eval-drift", EvalDriftWorkflow, drift_every))
    # Only where a hybrid retrieval leg reads the derived note index. `note_reindex_effective`
    # derives from the source list unless overridden, so enabling `vector`/`lexical` always builds
    # the index.
    if settings.note_reindex_effective:
        reindex_every = timedelta(minutes=settings.note_reindex_schedule_minutes)
        schedules.append(PlannedSchedule("note-reindex", NoteReindexWorkflow, reindex_every))
    # Only where a document share is enabled, asked of the manifests (the source list is the
    # switch).
    if share_sources():
        share_every = timedelta(minutes=settings.document_sync_schedule_minutes)
        schedules.append(PlannedSchedule("document-sync", DocumentShareSyncWorkflow, share_every))
    # Wherever there is a reaction corpus to label: an ingest half writing record rows, or a bulk
    # corpus binding. Not gated on `label_policies()`: a `labels:` block says what a source already
    # carries, not whether its rows may be labelled.
    if active_ingest_source_names() or corpus_sources():
        label_every = timedelta(minutes=settings.label_sync_schedule_minutes)
        schedules.append(PlannedSchedule("reaction-labels", ReactionLabelWorkflow, label_every))
    # Only where a source declares a `corpus:` binding. Daily: vendor releases are infrequent.
    if corpus_sources():
        corpus_every = timedelta(minutes=settings.corpus_sync_schedule_minutes)
        schedules.append(PlannedSchedule("reaction-corpus", ReactionCorpusWorkflow, corpus_every))
    # Only where a source declares a `commitments:` half. Daily: portfolio dates move on a human
    # cadence.
    if active_commitment_sources():
        commitment_every = timedelta(minutes=settings.commitment_sync_schedule_minutes)
        schedules.append(
            PlannedSchedule("commitment-mirror", CommitmentSyncWorkflow, commitment_every)
        )
    # Opt-in (default off). Digests land in a `digest-<owner>` mailbox that `GET /digests` reads.
    if settings.digest_enabled:
        digest_every = timedelta(minutes=settings.digest_schedule_minutes)
        schedules.append(PlannedSchedule("digest", DigestWorkflow, digest_every))
    # Gated on a flag rather than a registry: it reports on `pending_requests`, which every
    # deployment has, so the choice is whether people want to hear about it.
    if settings.check_in_enabled:
        check_in_every = timedelta(minutes=settings.check_in_schedule_minutes)
        schedules.append(PlannedSchedule("agent-check-in", CheckInWorkflow, check_in_every))
    # Only where the deployment stated a policy: `retention_enabled` **and** at least one window, so
    # "on but inert" is unrepresentable. An unconfigured deployment never deletes on a default.
    if settings.retention_enabled and _retention_windows_are_set():
        retention_every = timedelta(minutes=settings.retention_schedule_minutes)
        schedules.append(PlannedSchedule("retention", RetentionWorkflow, retention_every))
    # Artefact pushes expire on their own window wherever the durable session store holds them,
    # whether or not a retention policy is stated: a push is a notification. Fires once a window, so
    # a push outlives its window by at most one more.
    if settings.session_store == "postgres":
        push_every = timedelta(hours=settings.exhibit_push_retention_hours)
        schedules.append(PlannedSchedule("exhibit-pushes", ExhibitPushPruneWorkflow, push_every))
    # Either bound is the documented switch for artifact eviction.
    if settings.artifact_store_max_bytes or settings.artifact_evict_idle_days:
        eviction_every = timedelta(minutes=settings.artifact_eviction_schedule_minutes)
        schedules.append(
            PlannedSchedule("artifact-eviction", ArtifactEvictionWorkflow, eviction_every)
        )
    # Unconditional: any deployment can raise a wait, and a wait whose run was terminated is stuck
    # regardless of configuration. With no waits it is one empty query.
    orphan_every = timedelta(minutes=settings.awaiting_orphan_sweep_minutes)
    schedules.append(PlannedSchedule("orphaned-waits", OrphanedWaitsWorkflow, orphan_every))
    # Only where a result sink is enabled (`CHEMCLAW_RESULT_SINKS` is the switch); with none, the
    # outbox is empty by construction.
    if publishing_enabled():
        publish_every = timedelta(minutes=settings.result_publish_schedule_minutes)
        schedules.append(PlannedSchedule("result-publish", PublishResultsWorkflow, publish_every))
    # Opt-in: the observations tier is the one knowledge surface no human reviews before the agent
    # reads it.
    if settings.observations_enabled:
        observations_every = timedelta(minutes=settings.observation_schedule_minutes)
        schedules.append(
            PlannedSchedule("observations", ObservationSynthesisWorkflow, observations_every)
        )
    return schedules


def _jitter(job: PlannedSchedule) -> timedelta:
    """A deterministic per-job offset inside its interval, so co-scheduled jobs do not collide.

    Jobs sharing a cadence would otherwise fire together against the single background worker. A
    fixed phase derived from the schedule id (unlike Temporal's random per-fire jitter) is stable
    across re-applies, so applying stays a reconcile. Bounded to a fraction of the interval so a job
    never drifts into the next window.
    """
    span = job.interval * settings.schedule_jitter_fraction
    if not span:
        return timedelta(0)
    # A stable hash of the id, mapped into [0, span), using the repo's one hashing scheme.
    bucket = int(stable_hash(job.schedule_id, chars=8), 16) % 10_000
    return span * (bucket / 10_000)


def _build_schedule(job: PlannedSchedule) -> Schedule:
    """Build the Temporal `Schedule` for one planned job (no-arg workflow on the bg queue)."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            job.workflow.run,  # type: ignore[attr-defined]
            id=f"{job.schedule_id}-scheduled",
            task_queue=settings.background_task_queue,
            # A ceiling on one run: with `SKIP` below, a run that never ends silently skips every
            # later fire. Activities are already bounded; this backstops hung children, timers and
            # waits.
            #
            # `run_timeout`, not `execution_timeout`: the latter bounds the whole `continue_as_new`
            # chain, and the draining jobs (`corpus_sync`, `document_sync`, `label_sync`,
            # `eln_sync`) continue as new, so it would kill a long first load that may have no
            # cursor to resume from.
            run_timeout=timedelta(seconds=settings.schedule_run_timeout_seconds),
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=job.interval, offset=_jitter(job))],
        ),
        # SKIP, not the default BUFFER_ONE: every job is a full re-scan or a cursored sync, so an
        # overrunning run should finish and the buffered fire would be redundant.
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


def _preserving_pause(job: PlannedSchedule) -> Callable[[ScheduleUpdateInput], ScheduleUpdate]:
    """Build the update callback: this repository's spec, the *cluster's* paused state.

    A reconcile must not undo an operator's pause: the applier runs on every `helm upgrade`, and a
    fresh `Schedule` defaults to unpaused. Spec, action and overlap policy stay declarative; only
    the paused bit is a cluster fact, read from the live description the callback is handed.
    """

    def update(current: ScheduleUpdateInput) -> ScheduleUpdate:
        return ScheduleUpdate(
            schedule=replace(_build_schedule(job), state=current.description.schedule.state)
        )

    return update


async def _apply(client: Client, job: PlannedSchedule) -> str:
    """Create the Schedule, or update it in place if it already exists. Returns the action taken."""
    try:
        await client.create_schedule(job.schedule_id, _build_schedule(job))
        return "created"
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(job.schedule_id)
        await handle.update(_preserving_pause(job))
        return "updated"


async def _prune(client: Client, planned_ids: set[str]) -> None:
    """Delete owned Schedules that exist in Temporal but are no longer planned.

    Without this, a job removed from the plan keeps firing forever. Only ids in
    `OWNED_SCHEDULE_IDS` are ever deleted.
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

    Pruning makes a re-apply declarative: the owned Schedules end up exactly the planned set.
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

    **`last_outcome` is the field that makes a dead job distinguishable from a quiet one**, and
    every other field on this model was measured to be blind to it. A schedule whose every run is
    killed by `schedule_run_timeout_seconds` reports `runs_total` climbing, `last_run` advancing,
    `running_now` 0 and `skipped_overlap` 0 — the same six values a healthy job reports, because
    Temporal's `ScheduleInfo` carries no outcome anywhere: `recent_actions` names the workflow and
    when it started, and there is no failure counter beside `num_actions_skipped_overlap`. The
    wedge the ceiling replaced *did* have a signature here (`last_run` frozen, `running_now` stuck
    at 1, `skipped_overlap` climbing), so the ceiling was a real fix that moved the failure to a
    surface that said nothing.
    """

    schedule_id: str
    interval_seconds: float
    paused: bool = False
    last_run: datetime | None = None
    runs_total: int = 0
    skipped_overlap: int = 0
    running_now: int = 0
    # Temporal's `WorkflowExecutionStatus` name for the newest *finished* run. Empty means none has
    # finished yet; `unknown` means it could not be described (`note` says why). An in-flight run
    # has no outcome; `running_now` reports that.
    last_outcome: str = ""
    note: str = ""


async def describe_schedules(client: Client | None = None) -> list[ScheduleHealth]:
    """Report every planned Schedule's health, in plan order.

    A planned Schedule missing from Temporal is reported with a note, never omitted. This runs on
    the front door's event loop, so the lookups run concurrently (`gather` keeps plan order) and
    each is bounded by `connector_health_timeout_seconds` — `describe()` takes no `retry` argument.
    Each schedule costs two lookups (itself, then its newest finished run), so the worst case is
    twice the probe timeout.
    """
    connection = client if client is not None else await connect()
    return list(await asyncio.gather(*(_describe(connection, job) for job in planned_schedules())))


async def _describe(connection: Client, job: PlannedSchedule) -> ScheduleHealth:
    """One planned Schedule's health — never raising, so one dead lookup cannot end the sweep."""
    entry = ScheduleHealth(
        schedule_id=job.schedule_id,
        interval_seconds=job.interval.total_seconds(),
    )
    try:
        description = await asyncio.wait_for(
            connection.get_schedule_handle(job.schedule_id).describe(),
            settings.connector_health_timeout_seconds,
        )
    except Exception as exc:
        entry.note = f"not found in Temporal ({type(exc).__name__}) — was it ever applied?"
        return entry
    info = description.info
    entry.paused = description.schedule.state.paused
    entry.runs_total = info.num_actions
    entry.skipped_overlap = info.num_actions_skipped_overlap
    entry.running_now = len(list(info.running_actions or []))
    recent = list(info.recent_actions or [])
    if not recent:
        entry.note = "no run recorded yet"
        return entry
    entry.last_run = recent[-1].started_at
    entry.last_outcome, entry.note = await _last_outcome(connection, info)
    return entry


def _workflow_id(execution: ScheduleActionExecution) -> str:
    """The workflow id a schedule action started, or `""` for an action shape we cannot read.

    An `isinstance`, not a cast, so a future non-workflow action drops out instead of raising in a
    health probe.
    """
    return (
        execution.workflow_id if isinstance(execution, ScheduleActionExecutionStartWorkflow) else ""
    )


async def _last_outcome(connection: Client, info: ScheduleInfo) -> tuple[str, str]:
    """The newest *finished* run's status, and a note when it could not be read.

    `ScheduleInfo.running_actions` excludes in-flight runs without a lookup, so this costs one
    `describe` per schedule, bounded by the same probe timeout. Described by workflow id without a
    `run_id`, which answers with a `continue_as_new` chain's tail; the action's
    `first_execution_run_id` would always report the head as `CONTINUED_AS_NEW`. A run past its
    retention is `unknown` with a note, never an exception.
    """
    in_flight = {_workflow_id(action) for action in (info.running_actions or [])}
    finished = [
        result
        for result in (info.recent_actions or [])
        if _workflow_id(result.action) and _workflow_id(result.action) not in in_flight
    ]
    if not finished:
        return "", ""
    try:
        described = await asyncio.wait_for(
            connection.get_workflow_handle(_workflow_id(finished[-1].action)).describe(),
            settings.connector_health_timeout_seconds,
        )
    except Exception as exc:
        return "unknown", f"run outcome unavailable ({type(exc).__name__}) — retention expired?"
    status = described.status
    return ("unknown" if status is None else status.name), ""
