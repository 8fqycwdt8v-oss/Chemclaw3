"""The Schedule plan covers every periodic background job at its configured cadence.

Pure tests: `planned_schedules()` is the source of truth for `make schedules-apply`. Apply and
prune run against a recording fake of the client's Schedule surface; the outcome tests at the
end need a live broker.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio import workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionExecutionStartWorkflow,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleState,
    ScheduleUpdate,
    WorkflowExecutionStatus,
)
from temporalio.exceptions import ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from chemclaw.core.config import settings
from chemclaw.durable.check_in import CheckInWorkflow
from chemclaw.durable.digest import DigestWorkflow
from chemclaw.durable.eln_sync import ElnSyncWorkflow
from chemclaw.durable.eval_drift import EvalDriftWorkflow
from chemclaw.durable.label_sync import ReactionLabelWorkflow
from chemclaw.durable.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
)
from chemclaw.durable.note_index import NoteReindexWorkflow
from chemclaw.durable.orphaned_waits import OrphanedWaitsWorkflow
from chemclaw.durable.retention import RetentionWorkflow
from chemclaw.durable.schedules import (
    OWNED_SCHEDULE_IDS,
    PlannedSchedule,
    ScheduleHealth,
    _build_schedule,
    _describe,
    _jitter,
    _preserving_pause,
    apply_schedules,
    describe_schedules,
    planned_schedules,
)
from tests.temporal_env import start_local_env_or_skip


class _FakeHandle:
    """Handle to one fake Schedule: applies updates/deletes against the recording store."""

    def __init__(self, store: "_FakeTemporal", schedule_id: str) -> None:
        self._store = store
        self._id = schedule_id

    async def update(self, updater: Callable[[object], ScheduleUpdate]) -> None:
        self._store.updated.append(self._id)

    async def delete(self) -> None:
        self._store.schedules.discard(self._id)
        self._store.deleted.append(self._id)


class _FakeTemporal:
    """A recording stand-in for the Temporal client's Schedule surface (offline test)."""

    def __init__(self, existing: set[str]) -> None:
        self.schedules = set(existing)
        self.created: list[str] = []
        self.updated: list[str] = []
        self.deleted: list[str] = []

    async def create_schedule(self, schedule_id: str, schedule: Schedule) -> None:
        if schedule_id in self.schedules:
            raise ScheduleAlreadyRunningError()
        self.schedules.add(schedule_id)
        self.created.append(schedule_id)

    def get_schedule_handle(self, schedule_id: str) -> _FakeHandle:
        return _FakeHandle(self, schedule_id)

    async def list_schedules(self) -> AsyncIterator[SimpleNamespace]:
        async def _iter() -> AsyncIterator[SimpleNamespace]:
            for schedule_id in sorted(self.schedules):
                yield SimpleNamespace(id=schedule_id)

        return _iter()


def test_plan_covers_all_periodic_jobs() -> None:
    """The jobs a plain reaction corpus earns by default, each planned exactly once.

    Ingest and labelling go together. The digest is on by default so a `watch_for` subscription is
    evaluated. The check-in is on by default so a requester hears about a suspended campaign before
    it expires. The orphaned-wait sweep has no setting, since any deployment can raise a wait.
    Everything else is gated on a setting or a second declaration.
    """
    plan = planned_schedules()
    assert {p.workflow for p in plan} == {
        ElnSyncWorkflow,
        ReactionLabelWorkflow,
        DigestWorkflow,
        CheckInWorkflow,
        OrphanedWaitsWorkflow,
    }
    assert len({p.schedule_id for p in plan}) == len(plan)  # unique ids


def test_the_digest_schedule_is_dropped_when_a_deployment_turns_digests_off() -> None:
    """The opt-out still reaches the plan, which is what makes the default a default.

    Asserted beside the test above rather than folded into it: "on by default" and "off when asked"
    are two claims, and a change that hard-wired the Schedule would satisfy the first alone.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "digest_enabled", False)
        assert DigestWorkflow not in {p.workflow for p in planned_schedules()}


def test_no_scheduled_job_opens_a_pull_request() -> None:
    """No scheduled job writes knowledge: knowledge never arrives on a timer.

    Asserted over whatever `planned_schedules()` returns, so a later note-writing Schedule fails.
    """
    proposing = {
        CampaignSynthesisWorkflow,
        PlaybookDistillationWorkflow,
        OptimizationCampaignWorkflow,
    }
    scheduled = {p.workflow for p in planned_schedules()}
    assert not (scheduled & proposing), (
        "a Schedule fires these without a user asking, and each one opens pull requests"
    )


def test_drift_schedule_is_added_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The eval-drift Schedule appears only when drift detection is switched on (F10-F2)."""
    monkeypatch.setattr(settings, "eval_drift_enabled", False)
    assert EvalDriftWorkflow not in {p.workflow for p in planned_schedules()}
    monkeypatch.setattr(settings, "eval_drift_enabled", True)
    monkeypatch.setattr(settings, "eval_drift_schedule_minutes", 720)
    plan = planned_schedules()
    drift = next(p for p in plan if p.workflow is EvalDriftWorkflow)
    assert drift.schedule_id == "eval-drift"
    assert drift.interval == timedelta(minutes=720)
    assert len({p.schedule_id for p in plan}) == len(plan)  # still unique


def test_reindex_schedule_is_added_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note index gets a reindex Schedule only where a hybrid leg reads it.

    RRF fusion carries no staleness signal, so an unrefreshed index ranks stale hits confidently.
    """
    monkeypatch.setattr(settings, "note_reindex_enabled", False)
    assert NoteReindexWorkflow not in {p.workflow for p in planned_schedules()}
    monkeypatch.setattr(settings, "note_reindex_enabled", True)
    monkeypatch.setattr(settings, "note_reindex_schedule_minutes", 30)
    plan = planned_schedules()
    reindex = next(p for p in plan if p.workflow is NoteReindexWorkflow)
    assert reindex.schedule_id == "note-reindex"
    assert reindex.interval == timedelta(minutes=30)
    assert len({p.schedule_id for p in plan}) == len(plan)


def test_document_sync_schedule_is_added_only_when_a_share_is_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crawl is planned only where an enabled source carries a share to crawl.

    `CHEMCLAW_DATA_SOURCES` is the enable switch; a second flag could only contradict it.
    """
    from chemclaw.durable import schedules as schedules_module
    from chemclaw.durable.document_sync import DocumentShareSyncWorkflow

    monkeypatch.setattr(schedules_module, "share_sources", dict)
    assert DocumentShareSyncWorkflow not in {p.workflow for p in planned_schedules()}

    monkeypatch.setattr(schedules_module, "share_sources", lambda: {"sharedrive": object()})
    monkeypatch.setattr(settings, "document_sync_schedule_minutes", 90)
    plan = planned_schedules()
    crawl = next(p for p in plan if p.workflow is DocumentShareSyncWorkflow)
    assert crawl.schedule_id == "document-sync"
    assert crawl.interval == timedelta(minutes=90)
    assert len({p.schedule_id for p in plan}) == len(plan)


def test_planned_ids_stay_inside_owned_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every plannable id is registered in the prune namespace, else prune could miss it.

    `OWNED_SCHEDULE_IDS` is what authorises `_prune` to delete a Schedule, so an unregistered id
    keeps firing after its feature is turned off. Every conditional job is enabled here explicitly
    (never by relying on a default), and the count is the whole plan, so a job added without being
    enabled here fails.
    """
    from chemclaw.durable import schedules as schedules_module

    for flag in (
        "eval_drift_enabled",
        "note_reindex_enabled",
        "digest_enabled",
        "retention_enabled",
        "observations_enabled",
        "check_in_enabled",
    ):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, "retention_session_events_days", 30)
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 1)
    monkeypatch.setattr(schedules_module, "share_sources", lambda: {"sharedrive": object()})
    monkeypatch.setattr(schedules_module, "corpus_sources", lambda: {"pistachio": object()})
    monkeypatch.setattr(schedules_module, "active_ingest_source_names", lambda: ["eln-json"])
    monkeypatch.setattr(schedules_module, "publishing_enabled", lambda: True)
    monkeypatch.setattr(
        schedules_module, "active_commitment_sources", lambda: {"portfolio": object()}
    )

    planned = {p.schedule_id for p in planned_schedules()}

    # The guard is only worth anything if the plan is actually full — an empty plan is a subset of
    # everything. Every conditional job in this file is enabled above, beside the one that is not
    # conditional at all, which makes fourteen.
    assert len(planned) == 14, (
        f"the plan is not fully enabled, so the subset below is vacuous: {sorted(planned)}"
    )
    assert planned <= OWNED_SCHEDULE_IDS, (
        f"planned but not in the prune namespace: {sorted(planned - OWNED_SCHEDULE_IDS)} — "
        "_prune can never delete these, so they outlive the setting that created them"
    )


def test_apply_prunes_stale_owned_schedule_only() -> None:
    """A Schedule dropped from the plan is deleted; a foreign Schedule is never touched."""
    fake = _FakeTemporal(existing={"eval-drift", "eln-sync", "chemist-manual-schedule"})
    plan = [PlannedSchedule("eln-sync", ElnSyncWorkflow, timedelta(minutes=30))]
    asyncio.run(apply_schedules(cast(Client, fake), plan))
    assert fake.deleted == ["eval-drift"]  # no longer planned -> stops firing
    assert fake.updated == ["eln-sync"]  # existing planned Schedule updated in place
    assert fake.schedules == {"eln-sync", "chemist-manual-schedule"}  # foreign id intact


def test_apply_creates_missing_and_deletes_nothing_when_plan_is_current() -> None:
    """A fresh apply creates every planned Schedule and prunes nothing."""
    fake = _FakeTemporal(existing=set())
    plan = planned_schedules()
    asyncio.run(apply_schedules(cast(Client, fake), plan))
    assert set(fake.created) == {p.schedule_id for p in plan}
    assert fake.deleted == []
    assert fake.updated == []


def test_intervals_come_from_config() -> None:
    """The ELN sync fires at its configured interval (no hardcoding)."""
    by_workflow = {p.workflow: p.interval for p in planned_schedules()}
    assert by_workflow[ElnSyncWorkflow] == timedelta(minutes=settings.eln_sync_schedule_minutes)


def test_every_schedule_skips_an_overrunning_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlap is SKIP, not the default BUFFER_ONE.

    Every job re-scans or is cursored, so a buffered run is redundant and could build a backlog.
    """
    for job in planned_schedules():
        schedule = _build_schedule(job)
        assert schedule.policy is not None
        assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP, job.schedule_id


def test_every_schedule_bounds_one_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scheduled run has a ceiling, and it is the per-run one, not the chain-wide one.

    With SKIP, a run that never ends silently stops the family, so `run_timeout` makes it a failed
    run. `execution_timeout` spans the whole `continue_as_new` chain and would kill a long drain
    mid-way. The absence is asserted because the time-skipping server cannot tell the knobs apart.
    """
    for job in planned_schedules():
        action = _build_schedule(job).action
        assert isinstance(action, ScheduleActionStartWorkflow)
        assert action.run_timeout == timedelta(seconds=settings.schedule_run_timeout_seconds), (
            job.schedule_id
        )
        assert action.execution_timeout is None, (
            f"{job.schedule_id} carries a chain-wide execution timeout; a drain that continues as "
            "new would be killed mid-chain and restart from its first page on the next fire"
        )


def test_co_scheduled_jobs_are_spread_deterministically() -> None:
    """The three memory jobs share one cadence; without an offset they fire together (SCH-3).

    Deterministic (a stable hash of the schedule id), not random, so re-applying the plan stays a
    reconcile rather than a reshuffle — and the offsets stay inside the interval.
    """
    memory_jobs = [
        job
        for job in planned_schedules()
        if job.workflow
        in {CampaignSynthesisWorkflow, PlaybookDistillationWorkflow, OptimizationCampaignWorkflow}
    ]
    offsets = [_jitter(job) for job in memory_jobs]
    assert len(set(offsets)) == len(offsets), "co-scheduled jobs would fire simultaneously"
    for job, offset in zip(memory_jobs, offsets, strict=True):
        assert timedelta(0) <= offset < job.interval
    # Stable across calls: applying the plan twice must not move a job.
    assert [_jitter(job) for job in memory_jobs] == offsets


def test_retention_schedule_is_added_only_when_a_policy_is_stated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment must choose to delete records; it must never inherit that from a default."""
    monkeypatch.setattr(settings, "retention_enabled", False)
    assert RetentionWorkflow not in {p.workflow for p in planned_schedules()}
    monkeypatch.setattr(settings, "retention_enabled", True)
    monkeypatch.setattr(settings, "retention_schedule_minutes", 60)
    monkeypatch.setattr(settings, "retention_session_events_days", 90)
    plan = planned_schedules()
    retention = next(p for p in plan if p.workflow is RetentionWorkflow)
    assert retention.schedule_id == "retention"
    assert retention.interval == timedelta(minutes=60)


def test_retention_needs_a_window_and_not_only_the_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention is scheduled only with a non-zero window, not on the boolean alone.

    Otherwise the job fires forever, sweeps nothing and looks healthy.
    """
    monkeypatch.setattr(settings, "retention_enabled", True)
    monkeypatch.setattr(settings, "retention_session_events_days", 0)
    monkeypatch.setattr(settings, "retention_session_messages_days", 0)
    monkeypatch.setattr(settings, "retention_tool_results_days", 0)
    monkeypatch.setattr(settings, "retention_checkpoints_days", 0)

    assert RetentionWorkflow not in {p.workflow for p in planned_schedules()}

    monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
    assert RetentionWorkflow in {p.workflow for p in planned_schedules()}


def test_every_retention_window_turns_the_sweep_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each `retention_*_days` window alone schedules the sweep, and the set is the sweep's own.

    Held against `_window_days`, so a new window counts here with no edit.
    """
    from chemclaw.durable.retention import _PRUNABLE, _window_days
    from chemclaw.durable.schedules import retention_window_fields

    windows = retention_window_fields()
    assert {"retention_session_exhibits_days", "retention_result_publications_days"} <= set(windows)
    for name in windows:
        monkeypatch.setattr(settings, name, 0)
    assert {_window_days(table) for table in _PRUNABLE} == {0}
    monkeypatch.setattr(settings, "retention_enabled", True)
    assert RetentionWorkflow not in {p.workflow for p in planned_schedules()}
    for name in windows:
        monkeypatch.setattr(settings, name, 30)
        assert RetentionWorkflow in {p.workflow for p in planned_schedules()}, name
        monkeypatch.setattr(settings, name, 0)
    # Every window the sweep reads is one of these, so none can be set without scheduling it.
    for name in windows:
        monkeypatch.setattr(settings, name, 7)
    assert all(_window_days(table) == 7 for table in _PRUNABLE)


def test_artefact_pushes_expire_whether_or_not_a_retention_policy_is_stated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artefact-push pruning is planned wherever a push can be written, regardless of retention.

    An unbounded notification queue is not a policy anyone chose.
    """
    from chemclaw.durable.retention import ExhibitPushPruneWorkflow
    from chemclaw.durable.schedules import retention_window_fields

    monkeypatch.setattr(settings, "retention_enabled", False)
    for name in retention_window_fields():
        monkeypatch.setattr(settings, name, 0)
    monkeypatch.setattr(settings, "exhibit_push_retention_hours", 12)
    monkeypatch.setattr(settings, "session_store", "postgres")
    plan = {p.schedule_id: p for p in planned_schedules()}
    assert "retention" not in plan
    pushes = plan["exhibit-pushes"]
    assert pushes.workflow is ExhibitPushPruneWorkflow and pushes.interval == timedelta(hours=12)
    monkeypatch.setattr(settings, "session_store", "memory")
    assert "exhibit-pushes" not in {p.schedule_id for p in planned_schedules()}, (
        "the in-memory session store has no mailbox to hold a push"
    )


def test_the_eln_sync_is_planned_only_where_there_is_an_eln_to_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ELN sync is planned only where an ingest source is configured."""
    from chemclaw.durable import schedules as schedules_module

    monkeypatch.setattr(schedules_module, "active_ingest_source_names", list)
    assert ElnSyncWorkflow not in {p.workflow for p in planned_schedules()}

    monkeypatch.setattr(schedules_module, "active_ingest_source_names", lambda: ["eln"])
    plan = planned_schedules()
    assert ElnSyncWorkflow in {p.workflow for p in plan}
    assert len({p.schedule_id for p in plan}) == len(plan)


def test_the_labelling_drain_is_planned_wherever_there_is_a_corpus_to_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The labelling drain is planned wherever there are reactions to label.

    Not gated on a `labels:` block, which only says what a source carries. With neither an ingest
    source nor a corpus binding, it is not planned.
    """
    from chemclaw.durable import schedules as schedules_module

    monkeypatch.setattr(schedules_module, "active_ingest_source_names", list)
    monkeypatch.setattr(schedules_module, "corpus_sources", dict)
    assert ReactionLabelWorkflow not in {p.workflow for p in planned_schedules()}

    # An ELN alone earns it, and this is the case the old gate refused: no `labels:` block anywhere.
    monkeypatch.setattr(schedules_module, "active_ingest_source_names", lambda: ["eln-json"])
    assert ReactionLabelWorkflow in {p.workflow for p in planned_schedules()}

    # And so does a bulk corpus with no ingest half at all.
    monkeypatch.setattr(schedules_module, "active_ingest_source_names", list)
    monkeypatch.setattr(schedules_module, "corpus_sources", lambda: {"pistachio": object()})
    assert ReactionLabelWorkflow in {p.workflow for p in planned_schedules()}


def test_a_re_apply_does_not_resume_a_schedule_an_operator_paused() -> None:
    """A re-apply does not resume a Schedule an operator paused.

    The applier runs on every `helm upgrade`; asserted on the update callback, which must keep the
    live description's paused state.
    """
    job = PlannedSchedule("document-sync", NoteReindexWorkflow, timedelta(minutes=30))
    paused = ScheduleState(note="operator paused: share is broken", paused=True)
    described = SimpleNamespace(schedule=SimpleNamespace(state=paused))

    update = _preserving_pause(job)(cast(Any, SimpleNamespace(description=described)))

    assert update.schedule is not None
    assert update.schedule.state.paused is True
    assert update.schedule.state.note == "operator paused: share is broken"
    # The rest is still declarative — the spec, the action and the overlap policy are this
    # repository's to restate, and re-applying them is the whole point of the hook.
    assert update.schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
    assert update.schedule.spec.intervals[0].every == timedelta(minutes=30)


def test_a_running_schedule_stays_running_across_a_re_apply() -> None:
    """The other direction: preserving state must not accidentally pause a healthy Schedule."""
    job = PlannedSchedule("eln-sync", ElnSyncWorkflow, timedelta(minutes=60))
    running = ScheduleState(paused=False)
    described = SimpleNamespace(schedule=SimpleNamespace(state=running))

    update = _preserving_pause(job)(cast(Any, SimpleNamespace(description=described)))

    assert update.schedule is not None
    assert update.schedule.state.paused is False


def test_schedule_health_reports_a_planned_job_that_was_never_created() -> None:
    """A planned job missing from Temporal is the failure this surface exists to show (gap SCH-4).

    Omitting it would make a never-created Schedule indistinguishable from a healthy quiet one —
    which is exactly how a silently failing ELN sync stays invisible for weeks.
    """

    class _MissingHandle:
        async def describe(self) -> object:
            raise RuntimeError("schedule not found")

    class _Client:
        def get_schedule_handle(self, schedule_id: str) -> _MissingHandle:
            return _MissingHandle()

    health = asyncio.run(describe_schedules(cast(Client, _Client())))
    assert {h.schedule_id for h in health} == {p.schedule_id for p in planned_schedules()}
    assert all("not found in Temporal" in h.note for h in health)
    assert all(h.interval_seconds > 0 for h in health)


def _recent(workflow_id: str, when: datetime) -> SimpleNamespace:
    """One `ScheduleActionResult`-shaped fake: when the fire started and what it started.

    `_last_outcome` reads `workflow_id` off `action` to recover the run status.
    """
    return SimpleNamespace(
        started_at=when,
        action=ScheduleActionExecutionStartWorkflow(
            workflow_id=workflow_id, first_execution_run_id="run-1"
        ),
    )


def _health_client(
    info: SimpleNamespace, workflow_describe: Callable[[], object], paused: bool = False
) -> Client:
    """A client exposing just the two lookups `_describe` makes: the schedule, then the run."""

    class _ScheduleHandle:
        async def describe(self) -> object:
            return SimpleNamespace(
                schedule=SimpleNamespace(state=SimpleNamespace(paused=paused)), info=info
            )

    class _WorkflowHandle:
        async def describe(self) -> object:
            return workflow_describe()

    class _Client:
        def get_schedule_handle(self, schedule_id: str) -> _ScheduleHandle:
            return _ScheduleHandle()

        def get_workflow_handle(
            self, workflow_id: str, *, run_id: str | None = None
        ) -> _WorkflowHandle:
            # `run_id` is accepted and ignored: this fake models no chain, so it cannot distinguish
            # the chain's head from its tail.
            # `test_a_chains_own_outcome_is_reported_and_not_its_heads` guards that against a live
            # broker.
            return _WorkflowHandle()

    return cast(Client, _Client())


def test_schedule_health_surfaces_overlap_skips_and_the_last_run() -> None:
    """`skipped_overlap` is the early warning that a job no longer fits inside its interval."""
    when = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
    info = SimpleNamespace(
        num_actions=12,
        num_actions_skipped_overlap=3,
        running_actions=[
            ScheduleActionExecutionStartWorkflow(
                workflow_id="eln-sync-scheduled-now", first_execution_run_id="run-2"
            )
        ],
        recent_actions=[_recent("eln-sync-scheduled-then", when)],
    )
    client = _health_client(
        info,
        lambda: SimpleNamespace(status=WorkflowExecutionStatus.COMPLETED),
        paused=True,
    )

    health = asyncio.run(describe_schedules(client))
    first = health[0]
    assert first.runs_total == 12
    assert first.skipped_overlap == 3
    assert first.running_now == 1
    assert first.paused is True
    assert first.last_run == when
    assert first.last_outcome == "COMPLETED"
    assert first.note == ""


def test_a_run_that_can_no_longer_be_described_degrades_to_unknown() -> None:
    """A run that can no longer be described degrades to unknown, with the reason in `note`.

    One job's lookup failure must not take every other job's report with it.
    """

    def _gone() -> object:
        raise RuntimeError("workflow execution already completed and was deleted")

    info = SimpleNamespace(
        num_actions=4,
        num_actions_skipped_overlap=0,
        running_actions=[],
        recent_actions=[
            _recent("retention-scheduled-then", datetime(2026, 7, 25, 6, 0, tzinfo=UTC))
        ],
    )

    health = asyncio.run(describe_schedules(_health_client(info, _gone)))
    assert len(health) == len(planned_schedules()) >= 1  # the sweep survived, whole
    assert [h.last_outcome for h in health] == ["unknown"] * len(health)
    assert all("run outcome unavailable (RuntimeError)" in h.note for h in health)


def test_a_run_still_in_flight_is_not_reported_as_an_outcome() -> None:
    """A run still in flight is not reported as an outcome; `running_now` already says so."""
    started = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)
    running = ScheduleActionExecutionStartWorkflow(
        workflow_id="only-scheduled-now", first_execution_run_id="run-1"
    )
    info = SimpleNamespace(
        num_actions=1,
        num_actions_skipped_overlap=0,
        running_actions=[running],
        recent_actions=[_recent("only-scheduled-now", started)],
    )

    # Counted rather than raised from: `_last_outcome` degrades *any* failed lookup to `unknown`,
    # so a fake that raised would be swallowed into a plausible-looking value instead of failing.
    described: list[str] = []

    def _record() -> object:
        described.append("described")
        return SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)

    health = asyncio.run(describe_schedules(_health_client(info, _record)))
    first = health[0]
    assert described == []
    assert first.last_run == started
    assert first.running_now == 1
    assert first.last_outcome == ""
    assert first.note == ""


# --- The killed-run signature, against a live broker -------------------------------------------
#
# These tests depend on wall-clock fires and `run_timeout`, which time skipping fast-forwards.


@workflow.defn(name="ScheduleHealthProbeCompletes")
class _CompletingProbeWorkflow:
    """A healthy periodic job: it finishes well inside the ceiling."""

    @workflow.run
    async def run(self) -> str:
        """Return immediately."""
        return "done"


@workflow.defn(name="ScheduleHealthProbeWedges")
class _WedgingProbeWorkflow:
    """A wedged periodic job: every run is killed by `schedule_run_timeout_seconds`."""

    @workflow.run
    async def run(self) -> str:
        """Wait forever, so the run timeout is what ends it."""
        await workflow.wait_condition(lambda: False)
        return "unreachable"


async def _until(check: Callable[[], Awaitable[bool]], what: str, seconds: float = 45.0) -> None:
    """Poll `check` until it holds, or fail naming what never happened."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_schedule_whose_every_run_is_killed_is_not_reported_as_a_healthy_one() -> None:
    """A schedule whose every run is killed is not reported as a healthy one.

    Every other field is equal between a healthy and a killed schedule; only `last_outcome`
    separates `COMPLETED` from `TIMED_OUT`. Both are paused before reading, so the comparison is
    not a race.
    """

    async def _run() -> tuple[ScheduleHealth, ScheduleHealth]:
        async with await start_local_env_or_skip() as env:
            client = env.client
            jobs = [
                PlannedSchedule("probe-healthy", _CompletingProbeWorkflow, timedelta(seconds=3)),
                PlannedSchedule("probe-killed", _WedgingProbeWorkflow, timedelta(seconds=3)),
            ]
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[_CompletingProbeWorkflow, _WedgingProbeWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                for job in jobs:
                    await client.create_schedule(job.schedule_id, _build_schedule(job))

                async def _fired_twice() -> bool:
                    for job in jobs:
                        info = (await client.get_schedule_handle(job.schedule_id).describe()).info
                        if len(list(info.recent_actions or [])) < 2:
                            return False
                    return True

                await _until(_fired_twice, "both schedules to fire twice")
                for job in jobs:
                    await client.get_schedule_handle(job.schedule_id).pause()

                async def _nothing_running() -> bool:
                    for job in jobs:
                        info = (await client.get_schedule_handle(job.schedule_id).describe()).info
                        if list(info.running_actions or []):
                            return False
                    return True

                # The wedged run in flight when the pause landed still has to be killed by the
                # ceiling; until it is, the newest action has no outcome by construction.
                await _until(_nothing_running, "the in-flight killed run to hit its ceiling")
                return await _describe(client, jobs[0]), await _describe(client, jobs[1])

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(settings, "schedule_run_timeout_seconds", 1.0)
    monkeypatched.setattr(settings, "schedule_jitter_fraction", 0.0)
    try:
        healthy, killed = asyncio.run(_run())
    finally:
        monkeypatched.undo()

    # The new field, in both directions.
    assert healthy.last_outcome == "COMPLETED"
    assert killed.last_outcome == "TIMED_OUT"
    # And the old surface, which could not tell them apart. `runs_total` and `last_run` are the
    # two fields that differ only by when each job happened to be sampled, so they are compared
    # for *shape* (both advancing) rather than for equality.
    assert killed.runs_total >= 2 and healthy.runs_total >= 2
    assert killed.last_run is not None and healthy.last_run is not None
    ignored = {"schedule_id", "runs_total", "last_run", "last_outcome"}
    assert killed.model_dump(exclude=ignored) == healthy.model_dump(exclude=ignored)
    assert killed.skipped_overlap == 0
    assert killed.running_now == 0
    assert killed.note == ""


# --- What "the newest finished run" means, and what a chain's outcome is ------------------------
#
# Both are Temporal's behaviour, not this repository's, so they are asserted against a live
# broker rather than a fake.


# Flipped by the mixed-outcome test between two fires of one schedule. A Schedule fires one fixed
# action — every run starts with the same (empty) arguments — so a flag the test can move is the
# only way one schedule produces two different outcomes.
_FAIL_NEXT_FIRE = [False]


@workflow.defn(name="ScheduleHealthProbeFailsOnCue")
class _CuedFailureProbeWorkflow:
    """A periodic job that completes until the test cues it, then declares a failure."""

    @workflow.run
    async def run(self) -> str:
        """Complete, or raise once cued — `ApplicationError` fails the run, not the task."""
        if _FAIL_NEXT_FIRE[0]:
            raise ApplicationError("this fire is the one that fails")
        return "done"


@workflow.defn(name="ScheduleHealthProbeChains")
class _ChainingProbeWorkflow:
    """A draining periodic job: two `continue_as_new` hops, then a park the ceiling ends."""

    @workflow.run
    async def run(self, hop: int = 0) -> str:
        """Hand the chain on twice, then wait for `schedule_run_timeout_seconds` to kill it."""
        if hop < 2:
            workflow.continue_as_new(hop + 1)
        await workflow.wait_condition(lambda: False)
        return "unreachable"


def _started_workflow_id(action: object) -> str:
    """The workflow id a recorded schedule action started (narrowing the SDK's base class)."""
    assert isinstance(action, ScheduleActionExecutionStartWorkflow)
    return action.workflow_id


def test_the_newest_finished_run_is_the_one_reported() -> None:
    """A schedule whose newest run failed must not report the last good one.

    `recent_actions` is oldest-first, so `finished[-1]` is the newest; the order is asserted beside
    the outcomes. The cue flips only once the first fire is observed complete.
    """

    async def _run() -> tuple[ScheduleHealth, list[str], list[datetime]]:
        async with await start_local_env_or_skip() as env:
            client = env.client
            job = PlannedSchedule("probe-mixed", _CuedFailureProbeWorkflow, timedelta(seconds=3))
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[_CuedFailureProbeWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = client.get_schedule_handle(job.schedule_id)
                await client.create_schedule(job.schedule_id, _build_schedule(job))

                async def _first_fire_completed() -> bool:
                    info = (await handle.describe()).info
                    recent = list(info.recent_actions or [])
                    if not recent or list(info.running_actions or []):
                        return False
                    first = await client.get_workflow_handle(
                        _started_workflow_id(recent[0].action)
                    ).describe()
                    return first.status is WorkflowExecutionStatus.COMPLETED

                await _until(_first_fire_completed, "the first fire to complete")
                _FAIL_NEXT_FIRE[0] = True

                async def _a_later_fire_finished() -> bool:
                    info = (await handle.describe()).info
                    return len(list(info.recent_actions or [])) >= 2 and not list(
                        info.running_actions or []
                    )

                await _until(_a_later_fire_finished, "a second fire, finished")
                await handle.pause()
                health = await _describe(client, job)
                recent = list((await handle.describe()).info.recent_actions or [])
                statuses = [
                    (
                        await client.get_workflow_handle(
                            _started_workflow_id(action.action)
                        ).describe()
                    ).status
                    for action in recent
                ]
                return (
                    health,
                    [("" if s is None else s.name) for s in statuses],
                    [action.started_at for action in recent],
                )

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(settings, "schedule_jitter_fraction", 0.0)
    _FAIL_NEXT_FIRE[0] = False
    try:
        health, statuses, started = asyncio.run(_run())
    finally:
        _FAIL_NEXT_FIRE[0] = False
        monkeypatched.undo()

    # The fixture is genuinely mixed — asserted, not assumed, because a homogeneous one would make
    # the assertion below pass whichever end of the list `_last_outcome` reads.
    assert statuses[0] == "COMPLETED", statuses
    assert statuses[-1] == "FAILED", statuses
    assert started == sorted(started), "recent_actions is not oldest-first"
    assert health.last_outcome == "FAILED", (
        f"the newest finished run is {statuses[-1]} and the oldest is {statuses[0]}; "
        "reporting the oldest is a job that has started dying still reading healthy"
    )


def test_a_chains_own_outcome_is_reported_and_not_its_heads() -> None:
    """A `continue_as_new` chain reports what happened to the chain, not to its first run.

    Describing by workflow id answers with the chain's tail; `first_execution_run_id` names the
    head, which reads `CONTINUED_AS_NEW` however the chain ended. Both readings are taken here.
    """

    async def _run() -> tuple[ScheduleHealth, str]:
        async with await start_local_env_or_skip() as env:
            client = env.client
            job = PlannedSchedule("probe-chain", _ChainingProbeWorkflow, timedelta(seconds=5))
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[_ChainingProbeWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = client.get_schedule_handle(job.schedule_id)
                await client.create_schedule(job.schedule_id, _build_schedule(job))

                async def _fired() -> bool:
                    info = (await handle.describe()).info
                    return bool(list(info.recent_actions or []))

                await _until(_fired, "the chain to be fired")
                await handle.pause()

                async def _chain_is_dead() -> bool:
                    info = (await handle.describe()).info
                    return not list(info.running_actions or [])

                await _until(_chain_is_dead, "the chain's last hop to hit its ceiling")
                health = await _describe(client, job)
                action = list((await handle.describe()).info.recent_actions or [])[-1].action
                assert isinstance(action, ScheduleActionExecutionStartWorkflow)
                head = await client.get_workflow_handle(
                    action.workflow_id, run_id=action.first_execution_run_id
                ).describe()
                return health, ("" if head.status is None else head.status.name)

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(settings, "schedule_run_timeout_seconds", 1.0)
    monkeypatched.setattr(settings, "schedule_jitter_fraction", 0.0)
    try:
        health, head_status = asyncio.run(_run())
    finally:
        monkeypatched.undo()

    # The head is what the obvious lookup would have answered, and it is wrong about the chain.
    assert head_status == "CONTINUED_AS_NEW"
    assert health.last_outcome == "TIMED_OUT", (
        "the chain was killed by its run timeout; reporting its head would say "
        f"{head_status}, which is what a healthy hand-off looks like"
    )
