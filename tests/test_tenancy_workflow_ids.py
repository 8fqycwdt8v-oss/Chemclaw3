"""Whose a workflow id is, for the runs that are not started by one tenant's request path.

A deterministic id is how identical requests join one run, and Temporal's namespace is shared by
every tenant, so an id that names no tenant is shared by all of them. The queued tool call minted
one from its arguments alone; a scheduled run's id names its schedule and nobody else.
"""

import pytest

from chemclaw.connectors.queued import queued_workflow_id
from chemclaw.core.config import settings
from chemclaw.core.tenancy import bound_tenant, owns_workflow, workflow_owner


@pytest.fixture(autouse=True)
def _tenancy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy enabled with no process tenant."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")


def test_identical_queued_calls_from_two_tenants_do_not_share_a_run() -> None:
    """Tenant B's identical in-flight call must not join A's run and read its result."""
    args = {"smiles": "CCO"}
    with bound_tenant("acme"):
        acme = queued_workflow_id("calc", "run", args)
        again = queued_workflow_id("calc", "run", args)
    with bound_tenant("globex"):
        globex = queued_workflow_id("calc", "run", args)
    with bound_tenant("default"):
        default = queued_workflow_id("calc", "run", args)
    assert acme == again, "one tenant's identical calls still join one run"
    assert len({acme, globex, default}) == 3
    assert acme.startswith("acme~") and globex.startswith("globex~")
    assert not default.startswith("default~"), "the default tenant's ids are unchanged"


def test_a_tenant_owns_its_queued_call_and_not_anothers() -> None:
    """A detached heavy call's id is found by the tenant that started it and by no other."""
    with bound_tenant("acme"):
        mine = queued_workflow_id("calc", "run", {"x": 1})
        assert owns_workflow(mine)
    with bound_tenant("globex"):
        assert not owns_workflow(mine)


@pytest.mark.parametrize(
    ("workflow_id", "owner"),
    [
        ("acme~bo-start-abc", "acme"),
        ("acme~bo-start-abc:approval", "acme"),
        ("bo-start-abc", "default"),
        ("retention@acme-scheduled-2026-10-15T00:00:00Z", "acme"),
        ("retention@acme-corp-scheduled-2026-10-15T00:00:00Z", "acme-corp"),
        ("retention-scheduled-2026-10-15T00:00:00Z", "default"),
    ],
)
def test_the_owner_of_an_id_is_read_from_it(workflow_id: str, owner: str) -> None:
    """A scheduled run belongs to the tenant its schedule is named for."""
    assert workflow_owner(workflow_id) == owner


def test_another_tenants_scheduled_run_is_not_status_or_cancellable() -> None:
    """The default tenant's reviewer is not the owner of acme's retention run, nor vice versa."""
    run = "retention@acme-scheduled-2026-10-15T00:00:00Z"
    with bound_tenant("acme"):
        assert owns_workflow(run)
    with bound_tenant("globex"):
        assert not owns_workflow(run)
    with bound_tenant("default"):
        assert not owns_workflow(run)
