"""The Schedule plan covers every periodic background job at its configured cadence.

Pure tests (no Temporal server): `planned_schedules()` is the source of truth for what
`make schedules-apply` maintains, so a dropped job or a wrong interval is caught here.
The apply/prune behavior is proven against a recording fake of the client's Schedule
surface, since a live Temporal server is unavailable offline.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from temporalio.client import Client, Schedule, ScheduleAlreadyRunningError, ScheduleUpdate

from chemclaw.config import settings
from scripts.schedules import (
    OWNED_SCHEDULE_IDS,
    PlannedSchedule,
    apply_schedules,
    planned_schedules,
)
from workflows.eln_sync import ElnSyncWorkflow
from workflows.eval_drift import EvalDriftWorkflow
from workflows.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
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
