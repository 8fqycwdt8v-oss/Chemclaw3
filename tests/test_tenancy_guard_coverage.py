"""What the startup guard looks at: every database, the pooler in front of it, and again later.

`verify_deployment` once read `postgres_dsn` alone, once, and said nothing when it could not look.
These hold it to every runtime DSN, to a pooler that cannot carry a session setting, and to a
periodic recheck whose answer reaches `/readyz` and two gauges.
"""

import asyncio
import itertools
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.core import db
from chemclaw.core import deployment as deployment_module
from chemclaw.core.config import settings
from chemclaw.core.deployment import (
    DeploymentRefused,
    forget_deployment_status,
    pooler_problem,
    recheck_deployment,
    verify_deployment,
)
from chemclaw.core.metrics import METRICS
from tests.tenancy_db import enable_tenancy, tenancy_database


@pytest.fixture(autouse=True)
def _tenancy_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tenancy enabled with no process tenant, and no remembered check."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    monkeypatch.setattr(settings, "deployment_id", "")
    forget_deployment_status()
    yield
    forget_deployment_status()


# --- the pooler ------------------------------------------------------------------------------


async def test_a_session_mode_endpoint_passes_the_probe() -> None:
    """Postgres itself keeps a connection's settings and gives two clients two backends."""
    async with tenancy_database() as database:
        assert await pooler_problem(database.app_dsn, "postgres_dsn") is None


async def test_two_clients_sharing_one_backend_are_a_pooler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transaction-mode pooler with one server connection hands both clients the same backend."""
    async with tenancy_database() as database:
        shared = await db.connect(database.app_dsn)

        async def connect(_dsn: str) -> Any:
            return shared

        monkeypatch.setattr(db, "connect", connect)
        problem = await pooler_problem(database.app_dsn, "postgres_dsn")
        assert problem is not None
        assert "postgres_dsn" in problem and "session-mode" in problem


class _Rotating:
    """One client connection served by two server connections in turn: a statement-mode pooler."""

    def __init__(self, servers: list[Any]) -> None:
        self._servers = servers
        self._turn = itertools.cycle(range(len(servers)))

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await self._servers[next(self._turn)].execute(*args, **kwargs)

    async def set_autocommit(self, value: bool) -> None:
        for server in self._servers:
            await server.set_autocommit(value)

    async def close(self) -> None:
        for server in self._servers:
            await server.close()


async def test_a_setting_lost_between_statements_is_a_pooler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the tenant binding cannot survive: the next statement lands on another server."""
    async with tenancy_database() as database:
        real_connect = db.connect

        async def connect(dsn: str) -> Any:
            return _Rotating([await real_connect(dsn), await real_connect(dsn)])

        monkeypatch.setattr(db, "connect", connect)
        problem = await pooler_problem(database.app_dsn, "session_store_dsn")
        assert problem is not None and "session_store_dsn" in problem


async def test_the_start_refuses_a_pooled_endpoint_when_tenancy_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe is part of the check, so a transaction-mode pooler stops the pod."""
    async with tenancy_database() as database:
        await enable_tenancy(database)
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        await verify_deployment()
        shared = await db.connect(database.app_dsn)
        real_connect = db.connect

        async def connect(dsn: str, **kwargs: Any) -> Any:
            # The probe asks for a bare connection; everything else keeps its own.
            return await real_connect(dsn, **kwargs) if kwargs else shared

        monkeypatch.setattr(db, "connect", connect)
        with pytest.raises(DeploymentRefused, match="session-mode"):
            await verify_deployment()


# --- every database ---------------------------------------------------------------------------


async def test_a_session_store_role_that_bypasses_the_policies_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session tables hold every conversation: their role is checked, not only the first's."""
    async with tenancy_database() as database:
        await enable_tenancy(database)
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        monkeypatch.setattr(settings, "session_store_dsn", "")
        await verify_deployment()
        monkeypatch.setattr(settings, "session_store_dsn", database.owner_dsn)
        with pytest.raises(DeploymentRefused) as refused:
            await verify_deployment()
        assert "session_store_dsn: " in str(refused.value) and "BYPASSRLS" in str(refused.value)


# --- again, later -----------------------------------------------------------------------------


async def test_a_recheck_that_finds_a_contradiction_fails_readiness_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role changed under a running pod drains it; the gauges say why, the body does not."""
    async with tenancy_database() as database:
        await enable_tenancy(database)
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        await recheck_deployment()
        assert deployment_module.deployment_status().problems == ()
        monkeypatch.setattr(settings, "postgres_dsn", database.owner_dsn)
        await recheck_deployment()
        assert any("BYPASSRLS" in p for p in deployment_module.deployment_status().problems)
        assert _gauge("chemclaw_deployment_refused") == 1.0
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        await recheck_deployment()
        assert deployment_module.deployment_status().problems == ()


async def test_a_database_that_cannot_be_read_is_reported_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving without having looked is a state with a flag, a gauge and a log line."""
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://nobody:x@127.0.0.1:1/none")
    await recheck_deployment()
    status = deployment_module.deployment_status()
    assert status.unchecked is True and status.problems == ()
    assert _gauge("chemclaw_deployment_unchecked") == 1.0


def _gauge(name: str) -> float:
    """A bound gauge's current reading, from the exposition a scrape would get."""
    deployment_module.bind_deployment_gauges()
    match = re.search(rf"^{name} (\S+)$", METRICS.render(), re.MULTILINE)
    assert match is not None, f"{name} is not exposed"
    return float(match.group(1))


def _no_connectors(_profile: str | None = None) -> list[Any]:
    return []


def test_readyz_reports_a_refusal_and_an_unchecked_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 `deployment refused` with no reason in a public body; `deployment: unchecked` too."""
    monkeypatch.setattr(settings, "session_store", "memory")
    client = TestClient(create_app(connector_factory=_no_connectors))
    assert client.get("/readyz").status_code == 200

    deployment_module.deployment_status().unchecked = True
    body = client.get("/readyz").json()
    assert body["deployment"] == "unchecked"

    deployment_module.deployment_status().problems = ("a reason that names a role",)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "deployment refused"
    assert "role" not in response.text


def test_the_loop_is_started_only_where_there_is_something_to_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No identity, tenancy off and sessions in memory: nothing to look at, nothing scheduled."""
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    monkeypatch.setattr(settings, "session_store", "memory")
    assert deployment_module.checks_anything() is False
    asyncio.run(recheck_deployment())
    assert deployment_module.deployment_status().checked_at is None
