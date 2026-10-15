"""An activity with no tenant to act for, or one nobody serves, fails once and says why.

Left alone it reads nothing and has every write refused by a policy, which is a database error the
retry policy would repeat to its limit. The refusal is a `TenantError`, which `durable.retry` lists
as final, and `tenant-add` says what else a new tenant needs.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from chemclaw.cli import tenants as tenants_cli
from chemclaw.core.config import settings
from chemclaw.core.temporal_tenancy import _ActivityInbound, tenant_header
from chemclaw.core.tenancy import TenantError, current_tenant
from chemclaw.durable import retry


class _Next:
    """The activity itself: it answers with the tenant it ran for."""

    async def execute_activity(self, _input: Any) -> str:
        return current_tenant()


def _input(headers: dict[str, Any]) -> Any:
    def my_activity() -> None:
        """A named activity, for the message."""

    return SimpleNamespace(headers=headers, fn=my_activity)


@pytest.fixture(autouse=True)
def _tenancy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy enabled, no process tenant, and a registry that serves acme only."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")

    async def registered(tenant: str) -> bool:
        return tenant == "acme"

    monkeypatch.setattr("chemclaw.core.tenant_registry.is_registered", registered)


async def test_an_activity_with_a_registered_tenant_runs_for_it() -> None:
    """The ordinary case, unchanged."""
    inbound = _ActivityInbound(_Next())  # type: ignore[arg-type]
    assert await inbound.execute_activity(_input(tenant_header("acme"))) == "acme"


async def test_an_activity_with_no_tenant_is_refused_by_name() -> None:
    """A workflow that began before the switch carries no header: final, with the remedy."""
    inbound = _ActivityInbound(_Next())  # type: ignore[arg-type]
    with pytest.raises(TenantError, match="started without a tenant.*before tenancy"):
        await inbound.execute_activity(_input({}))


async def test_a_process_tenant_is_a_tenant_to_act_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker run for one tenant needs no header."""
    monkeypatch.setattr(settings, "tenancy_process_tenant", "acme")
    inbound = _ActivityInbound(_Next())  # type: ignore[arg-type]
    assert await inbound.execute_activity(_input({})) == "acme"


async def test_a_header_naming_an_unserved_tenant_is_refused() -> None:
    """A slug that nobody registered, or another release owns, is not a tenant here."""
    inbound = _ActivityInbound(_Next())  # type: ignore[arg-type]
    with pytest.raises(TenantError, match="'globex'.*does not serve"):
        await inbound.execute_activity(_input(tenant_header("globex")))


async def test_with_tenancy_off_nothing_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-tenant deployment: no header is fine, and the registry is not read."""
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    inbound = _ActivityInbound(_Next())  # type: ignore[arg-type]
    assert await inbound.execute_activity(_input({})) == "default"


def test_the_refusal_is_one_the_retry_policy_treats_as_final() -> None:
    """The reason a header-less activity is not retried."""
    assert "TenantError" in retry._BAD_DATA_TYPES


def test_a_new_tenant_is_told_what_else_it_needs(capsys: pytest.CaptureFixture[str]) -> None:
    """`tenant-add` prints the step that gives the tenant its Schedules."""
    assert "schedules-apply" in tenants_cli.NEXT_STEP
