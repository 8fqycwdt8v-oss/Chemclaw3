"""How the periodic jobs are bound to tenants.

A Schedule has no request to inherit a tenant from. With tenancy off the plan is what it was. With
it on, a job that sweeps tenant-scoped tables runs once per registered tenant, each copy
carrying its tenant, and a job bound to the deployment's data sources runs once for the tenant that
owns them: running it per tenant would copy every source into every tenant.
"""

from datetime import timedelta

import pytest

from chemclaw.core.config import settings
from chemclaw.durable.retention import RetentionWorkflow
from chemclaw.durable.schedules import (
    OWNED_SCHEDULE_IDS,
    PlannedSchedule,
    ScheduleTenantError,
    _owns,
    tenant_schedules,
)

_PLAN = [
    PlannedSchedule("retention", RetentionWorkflow, timedelta(hours=1)),
    PlannedSchedule("eln-sync", RetentionWorkflow, timedelta(hours=1)),
    PlannedSchedule("orphaned-waits", RetentionWorkflow, timedelta(hours=1)),
]


def test_with_tenancy_off_the_plan_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same ids, no tenant, whatever the registry holds."""
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    assert tenant_schedules(_PLAN, ["acme", "globex"]) == _PLAN


def test_a_sweep_runs_once_per_tenant_and_a_source_job_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`retention@acme`, `retention@globex`; one `eln-sync` for the tenant that owns the sources."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "acme")
    bound = tenant_schedules(_PLAN, ["acme", "globex"])
    assert [(job.schedule_id, job.tenant) for job in bound] == [
        ("retention@acme", "acme"),
        ("retention@globex", "globex"),
        ("eln-sync", "acme"),
        ("orphaned-waits@acme", "acme"),
        ("orphaned-waits@globex", "globex"),
    ]
    assert all(job.interval == timedelta(hours=1) for job in bound)


def test_a_source_job_with_no_owning_tenant_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Which tenant would the ELN be ingested into? Say so rather than choose."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    with pytest.raises(ScheduleTenantError, match="eln-sync"):
        tenant_schedules(_PLAN, ["acme"])


def test_a_tenants_copy_of_a_job_is_owned_so_it_is_pruned_with_it() -> None:
    """Pruning deletes `retention@acme` once tenancy is off, never a Schedule it did not make."""
    assert _owns("retention")
    assert _owns("retention@acme")
    assert not _owns("somebody-elses-job@acme")
    assert not _owns("retention-extra")
    assert "retention" in OWNED_SCHEDULE_IDS
