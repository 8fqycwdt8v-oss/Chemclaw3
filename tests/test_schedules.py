"""The Schedule plan covers every periodic background job at its configured cadence.

Pure test (no Temporal server): `planned_schedules()` is the source of truth for what
`make schedules-apply` maintains, so a dropped job or a wrong interval is caught here.
"""

from datetime import timedelta

import pytest

from chemclaw.config import settings
from scripts.schedules import planned_schedules
from workflows.eln_sync import ElnSyncWorkflow
from workflows.eval_drift import EvalDriftWorkflow
from workflows.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
)


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


def test_intervals_come_from_config() -> None:
    """The ELN sync and memory jobs fire at their configured intervals (no hardcoding)."""
    by_workflow = {p.workflow: p.interval for p in planned_schedules()}
    assert by_workflow[ElnSyncWorkflow] == timedelta(minutes=settings.eln_sync_schedule_minutes)
    assert by_workflow[CampaignSynthesisWorkflow] == timedelta(
        minutes=settings.memory_synthesis_schedule_minutes
    )
