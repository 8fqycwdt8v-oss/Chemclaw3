"""The audit sink is one buffer and one flusher for the whole process; its rows are not.

The flusher task runs in the context of whichever request started it, so a tenant read at flush time
is the first caller's. Each event carries the tenant of the call that recorded it, and a batch is
written for that tenant alone.
"""

import logging

import pytest

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.audit_store import PostgresAuditSink
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.tenancy import bound_tenant
from tests.tenancy_db import enable_tenancy, tenancy_database


def _event(label: str) -> AuditEvent:
    """One distinguishable audit event."""
    return AuditEvent(
        correlation_id=f"c-{label}",
        actor="person-1",
        tool="find_notes",
        arguments="{}",
        outcome="ok",
        latency_ms=1.0,
    )


@pytest.fixture
def tenancy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy enabled with no process tenant."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")


async def _filed(owner_dsn: str) -> dict[str, str]:
    """Which tenant each recorded event was filed under, read as the superuser."""
    async with await connect(owner_dsn) as conn:
        cursor = await conn.execute("SELECT correlation_id, tenant_id FROM audit_events")
        return {str(row[0]): str(row[1]) for row in await cursor.fetchall()}


async def test_two_tenants_events_in_one_buffer_are_filed_under_their_own(
    tenancy_on: None,
) -> None:
    """Acme records, globex records, one flush: each row carries the tenant that recorded it."""
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        sink = PostgresAuditSink(database.app_dsn)
        with bound_tenant("acme"):
            await sink.record(_event("a1"))
        with bound_tenant("globex"):
            await sink.record(_event("g1"))
        with bound_tenant("acme"):
            await sink.record(_event("a2"))
        await sink.flush()
        assert await _filed(database.owner_dsn) == {
            "c-a1": "acme",
            "c-g1": "globex",
            "c-a2": "acme",
        }


async def test_an_event_recorded_with_no_tenant_is_dropped_not_filed_under_the_flusher(
    tenancy_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    """With nothing bound where it was recorded, an event is lost loudly, never given a tenant."""
    caplog.set_level(logging.ERROR)
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme")
        sink = PostgresAuditSink(database.app_dsn)
        await sink.record(_event("orphan"))
        with bound_tenant("acme"):
            await sink.record(_event("a1"))
            await sink.flush()
        assert await _filed(database.owner_dsn) == {"c-a1": "acme"}
    assert any("audit_sink_failure" in r.getMessage() for r in caplog.records)
