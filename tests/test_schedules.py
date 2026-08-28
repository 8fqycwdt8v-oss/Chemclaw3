"""The Schedule plan covers every periodic background job at its configured cadence.

Pure tests (no Temporal server): `planned_schedules()` is the source of truth for what
`make schedules-apply` maintains, so a dropped job or a wrong interval is caught here.
The apply/prune behavior is proven against a recording fake of the client's Schedule
surface, since a live Temporal server is unavailable offline.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleState,
    ScheduleUpdate,
)

from chemclaw.core.config import settings
from chemclaw.durable.eln_sync import ElnSyncWorkflow
from chemclaw.durable.eval_drift import EvalDriftWorkflow
from chemclaw.durable.label_sync import ReactionLabelWorkflow
from chemclaw.durable.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
)
from chemclaw.durable.note_index import NoteReindexWorkflow
from chemclaw.durable.retention import RetentionWorkflow
from chemclaw.durable.schedules import (
    OWNED_SCHEDULE_IDS,
    PlannedSchedule,
    _build_schedule,
    _jitter,
    _preserving_pause,
    apply_schedules,
    describe_schedules,
    planned_schedules,
)


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
    """The two Schedules a plain reaction corpus earns, each planned exactly once.

    They travel together because they ask one question between them: an ingest source writes ELN
    entries, and every entry it writes needs labelling. Everything else in this file is gated on a
    setting or a second declaration.
    """
    plan = planned_schedules()
    assert {p.workflow for p in plan} == {ElnSyncWorkflow, ReactionLabelWorkflow}
    assert len({p.schedule_id for p in plan}) == len(plan)  # unique ids


def test_no_scheduled_job_opens_a_pull_request() -> None:
    """The rule D-2026-08-25 turns on: knowledge never arrives on a timer.

    The three memory-synthesis workflows proposed PR-gated notes hourly with nobody having asked.
    They still exist and still do their work — they are started on demand now — so the assertion
    that matters is about the *plan*, not about whether the workflows are gone.

    Asserted over whatever `planned_schedules()` returns rather than against a fixed list, so a
    Schedule added later for a job that proposes notes fails here instead of quietly restoring the
    behaviour this removed.
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
    """The derived note index gets a Schedule only where a hybrid leg reads it (gap SCH-2).

    Before this existed, `note_index` was refreshed only by a manual `make reindex`, so hybrid
    retrieval served whatever the last human run captured — ranked confidently beside live graph
    hits, because RRF fusion carries no staleness signal.
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
    """A crawl is planned only where an enabled source actually carries a share to crawl.

    Asked of the enabled sources rather than of a `document_sync_enabled` flag beside them:
    `CHEMCLAW_DATA_SOURCES` is the enable switch (D-018), and a second setting could only restate
    it or contradict it.
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

    **With every conditional job actually turned on**, which is what makes this an assertion rather
    than a formality. It used to enable `eval_drift_enabled` alone, so nine of the eleven
    conditional jobs were off by default and the subset held vacuously — and `result-publish`
    duly slipped through unregistered. `OWNED_SCHEDULE_IDS` is the only thing authorising `_prune`
    to delete a Schedule, so an unregistered planned id is a Schedule that survives every
    subsequent `helm upgrade` after the deployment turns its feature off, and keeps firing.
    """
    from chemclaw.durable import schedules as schedules_module

    for flag in (
        "eval_drift_enabled",
        "note_reindex_enabled",
        "digest_enabled",
        "retention_enabled",
        "observations_enabled",
    ):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, "retention_session_events_days", 30)
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 1)
    monkeypatch.setattr(schedules_module, "share_sources", lambda: {"sharedrive": object()})
    monkeypatch.setattr(schedules_module, "corpus_sources", lambda: {"pistachio": object()})
    monkeypatch.setattr(schedules_module, "active_ingest_source_names", lambda: ["eln-json"])
    monkeypatch.setattr(schedules_module, "publishing_enabled", lambda: True)

    planned = {p.schedule_id for p in planned_schedules()}

    # The guard is only worth anything if the plan is actually full — an empty plan is a subset of
    # everything. Ten conditional jobs plus the two unconditional ones.
    assert len(planned) >= 11, (
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
    """Overlap is SKIP, not the default BUFFER_ONE (gap SCH-3).

    Every scheduled job here is a full re-scan or a full reindex, so a run that overruns its
    interval must finish rather than have the next fire queue behind it. Buffering would let a slow
    corpus scan accumulate a backlog it can never drain — and the buffered run is redundant anyway,
    because the next fire re-scans everything. The ELN sync is cursored, so a skipped fire loses
    nothing either.
    """
    for job in planned_schedules():
        schedule = _build_schedule(job)
        assert schedule.policy is not None
        assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP, job.schedule_id


def test_every_schedule_bounds_one_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scheduled run has an execution ceiling, because SKIP makes an endless one invisible.

    The pair with the test above is the point: `SKIP` is right, and it is exactly what turns a run
    that never ends into a job family that silently stops running — every subsequent fire is
    skipped, and a skipped fire is an error nowhere. The ceiling is what makes that state a failed
    run instead.
    """
    for job in planned_schedules():
        action = _build_schedule(job).action
        assert isinstance(action, ScheduleActionStartWorkflow)
        assert action.execution_timeout == timedelta(
            seconds=settings.schedule_run_timeout_seconds
        ), job.schedule_id


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
    """The policy is the windows; the boolean alone produced a job that swept nothing.

    The condition lived in two files and they disagreed: `retention_enabled` turned the *Schedule*
    on, `retention_*_days > 0` turned the *work* on, and all four windows default to 0. An operator
    who did the documented thing got a job firing on its cadence forever, reporting
    `skipped: [... (retention disabled)]` for every table, and showing perfectly healthy in
    `describe_schedules`. Asking for both here makes "on but inert" unrepresentable.
    """
    monkeypatch.setattr(settings, "retention_enabled", True)
    monkeypatch.setattr(settings, "retention_session_events_days", 0)
    monkeypatch.setattr(settings, "retention_session_messages_days", 0)
    monkeypatch.setattr(settings, "retention_tool_results_days", 0)
    monkeypatch.setattr(settings, "retention_checkpoints_days", 0)

    assert RetentionWorkflow not in {p.workflow for p in planned_schedules()}

    monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
    assert RetentionWorkflow in {p.workflow for p in planned_schedules()}


def test_the_eln_sync_is_planned_only_where_there_is_an_eln_to_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one periodic job that was planned unconditionally, asked the same question as its peers.

    With no ingest source configured — the default — this was an hourly Schedule firing a workflow
    whose first act is to enumerate zero sources and merge an empty list. `document-sync` already
    asks the registry rather than a second setting; so does this now.
    """
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
    """The gate that decided whether this feature ran at all, and got the question wrong.

    It used to be `if label_policies():` — some enabled source declaring a `labels:` block. Exactly
    one source in this tree declares one and it ships disabled, so on a stock deployment this
    Schedule was never created and no ELN reaction was ever labelled by anything. A block says what
    a source already *carries*; what earns the Schedule is having reactions at all.

    Both halves are asserted, because the original concern is still real: a deployment with neither
    an ingest source nor a corpus binding would otherwise ask the labelling server for its version
    every hour and then label nothing.
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
    """A reconcile restates this repository's spec; it must not undo an operator's hand.

    `_build_schedule` returns a fresh `Schedule` whose `state` defaults to `paused=False`, and the
    chart runs the applier as a `post-install,post-upgrade` hook — so pausing `document-sync`
    because a share is broken, or `retention` during an incident, survived exactly until the next
    unrelated `helm upgrade`, silently. Measured against a live Temporal server before the fix:
    pause → `True`, re-apply → `False`.

    Asserted on the update callback rather than through the fake client, because the callback *is*
    the fix: it is handed the live description and used to ignore it.
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


def test_schedule_health_surfaces_overlap_skips_and_the_last_run() -> None:
    """`skipped_overlap` is the early warning that a job no longer fits inside its interval."""
    when = datetime(2026, 7, 25, 6, 0, tzinfo=UTC)

    class _Handle:
        async def describe(self) -> object:
            return SimpleNamespace(
                schedule=SimpleNamespace(state=SimpleNamespace(paused=True)),
                info=SimpleNamespace(
                    num_actions=12,
                    num_actions_skipped_overlap=3,
                    running_actions=[object()],
                    recent_actions=[SimpleNamespace(started_at=when)],
                ),
            )

    class _Client:
        def get_schedule_handle(self, schedule_id: str) -> _Handle:
            return _Handle()

    health = asyncio.run(describe_schedules(cast(Client, _Client())))
    first = health[0]
    assert first.runs_total == 12
    assert first.skipped_overlap == 3
    assert first.running_now == 1
    assert first.paused is True
    assert first.last_run == when
    assert first.note == ""
