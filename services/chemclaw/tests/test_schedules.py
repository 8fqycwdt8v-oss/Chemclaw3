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
from typing import cast

import pytest
from temporalio.client import (
    Client,
    Schedule,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleUpdate,
)

from chemclaw.config import settings
from scripts.schedules import (
    OWNED_SCHEDULE_IDS,
    PlannedSchedule,
    _build_schedule,
    _jitter,
    apply_schedules,
    describe_schedules,
    planned_schedules,
)
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
    """The four always-on Schedule-driven workflows are all planned, each exactly once."""
    plan = planned_schedules()
    assert {p.workflow for p in plan} == {
        ElnSyncWorkflow,
        CampaignSynthesisWorkflow,
        PlaybookDistillationWorkflow,
        OptimizationCampaignWorkflow,
    }
    assert len({p.schedule_id for p in plan}) == len(plan)  # unique ids


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


def test_planned_ids_stay_inside_owned_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every plannable id is registered in the prune namespace, else prune could miss it."""
    monkeypatch.setattr(settings, "eval_drift_enabled", True)
    assert {p.schedule_id for p in planned_schedules()} <= OWNED_SCHEDULE_IDS


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
    """The ELN sync and memory jobs fire at their configured intervals (no hardcoding)."""
    by_workflow = {p.workflow: p.interval for p in planned_schedules()}
    assert by_workflow[ElnSyncWorkflow] == timedelta(minutes=settings.eln_sync_schedule_minutes)
    assert by_workflow[CampaignSynthesisWorkflow] == timedelta(
        minutes=settings.memory_synthesis_schedule_minutes
    )


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
    plan = planned_schedules()
    retention = next(p for p in plan if p.workflow is RetentionWorkflow)
    assert retention.schedule_id == "retention"
    assert retention.interval == timedelta(minutes=60)


def test_audit_verify_schedule_is_added_only_when_a_sink_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chain checked only by a manual `make audit-verify` is not a control (gap SCH-5).

    Gated because an offline/dev deployment has no durable sink and would alert on an empty table.
    """
    monkeypatch.setattr(settings, "audit_verify_enabled", False)
    assert AuditChainVerifyWorkflow not in {p.workflow for p in planned_schedules()}
    monkeypatch.setattr(settings, "audit_verify_enabled", True)
    monkeypatch.setattr(settings, "audit_verify_schedule_minutes", 360)
    verify = next(p for p in planned_schedules() if p.workflow is AuditChainVerifyWorkflow)
    assert verify.schedule_id == "audit-verify"
    assert verify.interval == timedelta(minutes=360)


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
