"""Create/update the Temporal Schedules that drive the periodic background jobs.

The ELN sync and the three memory-synthesis workflows are worker-registered but only run
when something fires them. That "something" is a Temporal Schedule — durability lives in
Temporal, not host cron (D-006 reasoning) — created here and applied idempotently by
`make schedules-apply`, so re-running updates each Schedule in place rather than erroring.

Intervals come from config (`*_schedule_minutes`). Every Schedule targets the
`background-jobs` queue. The ELN sync is self-cursoring (it loads/stores its high-water mark
in `sync_cursors`), so its Schedule passes no argument; the memory-synthesis jobs re-scan
the whole corpus and carry no state either. `planned_schedules()` is the pure, testable list
of what will be applied; `apply_schedules()` applies it against a live client.

This is durable-layer library code, not an entrypoint: a Temporal Schedule is Temporal's own
durability primitive, and `chemclaw.api.app` imports `ScheduleHealth`/`describe_schedules` here at
module scope for the `/schedules` health endpoint. It used to live in `chemclaw.cli`, which put
the front door's health check reaching into the entrypoint layer for library functions. The CLI
surface (`python -m chemclaw.cli.schedules`, `make schedules-apply`) is now a thin `main()` shim in
`chemclaw.cli.schedules` that calls `apply_schedules()` here.
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
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
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
from chemclaw.durable.corpus_sync import ReactionCorpusWorkflow, corpus_sources
from chemclaw.durable.digest import DigestWorkflow
from chemclaw.durable.document_sync import DocumentShareSyncWorkflow, share_sources
from chemclaw.durable.eln_sync import ElnSyncWorkflow
from chemclaw.durable.eval_drift import EvalDriftWorkflow
from chemclaw.durable.label_sync import ReactionLabelWorkflow
from chemclaw.durable.note_index import NoteReindexWorkflow
from chemclaw.durable.observation_jobs import ObservationSynthesisWorkflow
from chemclaw.durable.publish_results import PublishResultsWorkflow
from chemclaw.durable.retention import RetentionWorkflow
from chemclaw.ingest.sources.registry import active_ingest_source_names
from chemclaw.publish.registry import publishing_enabled

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
        # Retired with the audit hash chain. The id stays because this set is the *prune*
        # namespace: it is the only thing authorising the applier to delete a Schedule, so
        # dropping the name would strand a live `audit-verify` Schedule firing a workflow no
        # worker registers. It is deleted on the next apply and can go once every deployment
        # has run one.
        "audit-verify",
        "digest",
        "artifact-eviction",
        "observations",
        "document-sync",
        "reaction-labels",
        "reaction-corpus",
        # Planned since the result sinks shipped and registered here only after an audit found it
        # missing: `_prune` computes `OWNED_SCHEDULE_IDS - planned_ids`, so a deployment that set
        # `CHEMCLAW_RESULT_SINKS`, got the Schedule, and later cleared the setting kept firing
        # `PublishResultsWorkflow` through every subsequent `helm upgrade`. The guard that was
        # supposed to catch this enabled one conditional job and passed vacuously; it now builds
        # the full plan.
        "result-publish",
    }
)


def _retention_windows_are_set() -> bool:
    """Whether any table has a retention window, i.e. whether the sweep would delete anything.

    Reads the same four settings `retention._window_days` maps, because the condition being asked
    is exactly "would that function return a non-zero for anything". Kept a predicate here rather
    than imported from there so the plan stays free of the workflow module's own imports.
    """
    return any(
        (
            settings.retention_session_events_days,
            settings.retention_session_messages_days,
            settings.retention_tool_results_days,
            settings.retention_checkpoints_days,
        )
    )


def planned_schedules() -> list[PlannedSchedule]:
    """The Schedules this script maintains.

    Pure and side-effect-free (no client), so a test can assert the set of jobs and their
    configured cadences without a live Temporal server.

    **No Schedule here opens a pull request** (D-2026-08-25). Campaign synthesis, playbook
    distillation and optimization-campaign detection used to fire hourly and propose PR-gated notes
    with nobody having asked, which is knowledge arriving on a timer. The miners are unchanged and
    still run — `CampaignSynthesisWorkflow`, `PlaybookDistillationWorkflow` and
    `OptimizationCampaignWorkflow` are started on demand, by a chemist or by an agent workflow that
    has a reason to look. What is left on a timer is ingestion, indexing, eviction and retention:
    jobs that make the corpus queryable and none that decide what it means.
    """
    eln_every = timedelta(minutes=settings.eln_sync_schedule_minutes)
    schedules: list[PlannedSchedule] = []
    # The ELN sync earns a Schedule only where there is an ELN to sync — the same question
    # `document-sync` asks seventeen lines below, asked of the same registry. It was the one
    # periodic job planned unconditionally, and with no source configured (the default) that is an
    # hourly Schedule firing a workflow whose first act is to enumerate zero sources and merge an
    # empty list. Asked of the manifests rather than of a new `eln_sync_enabled` setting, for the
    # reason `document-sync` records: `CHEMCLAW_DATA_SOURCES` is already the enable switch (D-018),
    # and a second flag could only restate it or contradict it.
    if active_ingest_source_names():
        schedules.append(PlannedSchedule("eln-sync", ElnSyncWorkflow, eln_every))
    # The drift check is opt-in (plan F10-F2): it only earns a Schedule where a committed baseline
    # is maintained, so an unconfigured deployment does not fire an eval it has no baseline for.
    if settings.eval_drift_enabled:
        drift_every = timedelta(minutes=settings.eval_drift_schedule_minutes)
        schedules.append(PlannedSchedule("eval-drift", EvalDriftWorkflow, drift_every))
    # The derived note index only earns a Schedule where a hybrid retrieval leg actually reads it
    # (gap SCH-2). A graph-only deployment would otherwise pay to rebuild an index nothing queries.
    # `note_reindex_effective` is derived from the source list unless a deployment overrides it —
    # so enabling `vector`/`lexical` cannot leave both legs querying a never-built index.
    if settings.note_reindex_effective:
        reindex_every = timedelta(minutes=settings.note_reindex_schedule_minutes)
        schedules.append(PlannedSchedule("note-reindex", NoteReindexWorkflow, reindex_every))
    # A document share earns a Schedule only where one is actually enabled. Asked of the manifests
    # rather than of a second setting: `CHEMCLAW_DATA_SOURCES` is the enable switch (D-018), and a
    # `document_sync_enabled` flag beside it could only ever say something the source list already
    # says, or say it wrongly.
    if share_sources():
        share_every = timedelta(minutes=settings.document_sync_schedule_minutes)
        schedules.append(PlannedSchedule("document-sync", DocumentShareSyncWorkflow, share_every))
    # The labelling drain earns a Schedule wherever there is a reaction corpus to label — an
    # ingest half that writes record rows, or a bulk corpus binding. Still the manifests rather
    # than a flag, for the third time in this file, because `CHEMCLAW_DATA_SOURCES` plus a
    # declaration already answers it; and a deployment with neither still gets no Schedule, which
    # is the concern that mattered — it would otherwise ask the labelling server for its version
    # every hour and then label nothing.
    #
    # **Not `label_policies()`.** That was the gate until it was measured, and it is the wrong
    # question: exactly one source in this tree declares a `labels:` block and it ships disabled,
    # so on a stock deployment this Schedule was never created and the ELN corpus was never
    # labelled by anything. A block says what a source already *carries*, not whether its rows may
    # be labelled — `D-2026-08-25-a-label-is-derived-not-recorded` reads `provides` for the
    # coverage report and the `override` subset check, and for nothing else.
    if active_ingest_source_names() or corpus_sources():
        label_every = timedelta(minutes=settings.label_sync_schedule_minutes)
        schedules.append(PlannedSchedule("reaction-labels", ReactionLabelWorkflow, label_every))
    # And the corpus drain earns one only where a source declares a `corpus:` binding. Daily
    # rather than hourly: a release changes when a vendor ships one, so an hourly re-walk would
    # read a warehouse to learn nothing.
    if corpus_sources():
        corpus_every = timedelta(minutes=settings.corpus_sync_schedule_minutes)
        schedules.append(PlannedSchedule("reaction-corpus", ReactionCorpusWorkflow, corpus_every))
    # Digests earn a Schedule where a deployment turns them on (gap IDEA-1, default off); with the
    # flag clear the job would sweep the corpus daily to deliver nothing.
    #
    # **The flag is enough now, and for the whole first life of this job it was not.** The digest
    # lands in a `session_events` mailbox keyed `digest-<owner>`, and until
    # `D-2026-08-27-a-digest-nobody-can-read-is-not-delivered` nothing in the tree could read one:
    # the ack still fired on the insert, so every run moved a subscriber's watermark past matches
    # no surface could ever show them, and `_is_new` cannot re-qualify a note once it has. Turning
    # this on lost matches rather than merely failing to deliver them. `api/routes/streams.py`'s
    # `GET /digests` is the reader that makes the acknowledgement true, which is what leaves this
    # condition an ordinary opt-in rather than one that has to ask whether a consumer exists.
    if settings.digest_enabled:
        digest_every = timedelta(minutes=settings.digest_schedule_minutes)
        schedules.append(PlannedSchedule("digest", DigestWorkflow, digest_every))
    # Retention only earns a Schedule where the deployment has stated a policy (gap SCH-1); an
    # unconfigured deployment must never start deleting records on a default it did not choose.
    #
    # **The policy is the windows, not the boolean.** `retention_enabled` turns the *Schedule* on
    # and `retention_*_days > 0` turns the *work* on, and all four windows default to 0 — so the
    # documented act of "stating a policy" produced a job that swept nothing, reported
    # `skipped: [... (retention disabled)]` for every table, and showed healthy in
    # `describe_schedules` forever. Two expressions of one condition, in two files, disagreeing.
    # Asking for both here makes "on but inert" unrepresentable, which is what `share_sources()`
    # already does for `document-sync`.
    if settings.retention_enabled and _retention_windows_are_set():
        retention_every = timedelta(minutes=settings.retention_schedule_minutes)
        schedules.append(PlannedSchedule("retention", RetentionWorkflow, retention_every))
    # Artifact eviction earns a Schedule as soon as either of its two bounds is set — those two
    # settings *are* the documented way to turn eviction on, and until this entry existed they
    # turned on nothing: the workflow was decorated, imported by the background worker and
    # advertised on the queue, with no schedule, no route and no caller anywhere. Written,
    # registered, never fired — the failure `durable/registry.py` exists to prevent, one level up.
    if settings.artifact_store_max_bytes or settings.artifact_evict_idle_days:
        eviction_every = timedelta(minutes=settings.artifact_eviction_schedule_minutes)
        schedules.append(
            PlannedSchedule("artifact-eviction", ArtifactEvictionWorkflow, eviction_every)
        )
    # Draining the result outbox earns a Schedule only where a sink is actually enabled - the same
    # question `document-sync` and `eln-sync` ask of their own registries, and asked of the sink
    # registry rather than of a second `result_publish_enabled` flag, because
    # `CHEMCLAW_RESULT_SINKS` is already the enable switch (D-018) and a second flag could only
    # restate it or contradict it. With no sink configured the queue is empty by construction, so
    # this would be a job sweeping a table nothing writes.
    if publishing_enabled():
        publish_every = timedelta(minutes=settings.result_publish_schedule_minutes)
        schedules.append(PlannedSchedule("result-publish", PublishResultsWorkflow, publish_every))
    # The observations tier is the one knowledge surface no human reviews before the agent reads
    # it, so it fires only where a deployment has consciously turned it on (D-161). Without this
    # guard the table would fill on a default nobody chose.
    if settings.observations_enabled:
        observations_every = timedelta(minutes=settings.observation_schedule_minutes)
        schedules.append(
            PlannedSchedule("observations", ObservationSynthesisWorkflow, observations_every)
        )
    return schedules


def _jitter(job: PlannedSchedule) -> timedelta:
    """A deterministic per-job offset inside its interval, so co-scheduled jobs do not collide.

    Jobs sharing one configured cadence would otherwise fire simultaneously against a single
    background worker (`replicas: 1`) and contend for the same reads (gap SCH-3). That was written
    for the three memory-synthesis jobs, which no longer have Schedules (D-2026-08-25); the
    collision it prevents is a property of any two schedules sharing a cadence, so the offset
    stays. Temporal jitter is a *random*
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
            # A ceiling on one run, because `SKIP` below makes a run that never ends the worst
            # failure this file can have: every subsequent fire is skipped, indefinitely, and a
            # skipped fire is not an error in `describe_schedules`, in a log or on a dashboard —
            # the job simply stops running and nothing says so. Every activity these workflows
            # schedule is now bounded on both sides (`durable/publish.py::queue_wait_timeout`), so
            # this is the backstop for what that cannot see: a child that hangs, a timer, a wait.
            # `schedule_run_timeout_seconds` explains why a day is the right size and why a
            # terminated run is safe here.
            #
            # **`run_timeout`, not `execution_timeout`, and the difference is the whole point.**
            # `execution_timeout` is Temporal's WorkflowExecutionTimeout: it bounds the entire
            # `continue_as_new` chain, and a continued run cannot extend it — the continue-as-new
            # command carries a run timeout and a task timeout and no execution timeout. Four of
            # the jobs scheduled here drain by continuing as new (`corpus_sync`, `document_sync`,
            # `label_sync`, `eln_sync`), so a chain-wide ceiling would not bound "one run" at all:
            # it would kill a first load of a multi-million-row corpus a day into the drain, and a
            # release-mode `corpus_sync` persists no cursor at all, so the next fire would start
            # again from its first page and never finish. (A source binding `append_only: true`
            # keeps its keyset position in `corpus_cursors` and would resume — but the argument
            # has to hold for the default, which is the release.) Measured against a live
            # broker on a chain of ten
            # one-second runs under a five-second ceiling: `execution_timeout` failed it at 5.64 s,
            # `run_timeout` completed it in 12.38 s.
            run_timeout=timedelta(seconds=settings.schedule_run_timeout_seconds),
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


def _preserving_pause(job: PlannedSchedule) -> Callable[[ScheduleUpdateInput], ScheduleUpdate]:
    """Build the update callback: this repository's spec, the *cluster's* paused state.

    **A reconcile must not undo an operator's hand.** `_build_schedule` returns a fresh `Schedule`
    whose `state` defaults to `paused=False`, and the chart runs this applier as a
    `post-install,post-upgrade` hook — so pausing `document-sync` because a share is broken, or
    `retention` during an incident, survived exactly until the next unrelated `helm upgrade`, with
    no log line saying it had been resumed. Measured against a live server: pause → `True`, the old
    one-line update → `False`, this callback → `True`.

    The callback is already handed the live description; it was simply ignored, which is why the fix
    is to read it rather than to add a setting recording what an operator paused.

    Everything *else* stays declarative on purpose. The spec, the action and the overlap policy are
    this repository's to state, and re-applying them is the whole point of the hook; only the
    paused bit is a fact about the cluster that no file here can know.
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

    **Concurrently and each bounded, because this is a probe on the front door's event loop.** The
    lookups are independent and there are up to eleven of them; run in sequence with the SDK's own
    retry underneath and no timeout, an unreachable broker made an authenticated route hang for
    (retry budget × 11) — at exactly the moment an operator opens the "is the machinery running"
    page. Both halves are already argued elsewhere in this repository, on the two probes that face
    the same broker: `connectors/health.py` ("concurrent because probes are independent and a serial
    sweep would make startup wait for the sum of the timeouts rather than the slowest one") and
    `api/runner.py` ("`retry=False` keeps it a *probe* — the SDK's default retry would turn one
    unreachable broker into a per-turn backoff loop"). A timeout is what bounds it here, since
    `describe()` takes no `retry` argument.

    `gather` preserves order, so the report is still in plan order.
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
    if recent:
        entry.last_run = recent[-1].started_at
    else:
        entry.note = "no run recorded yet"
    return entry
