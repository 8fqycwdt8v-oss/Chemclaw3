"""The durable Postgres audit sink persists a GxP tool-audit row (INV-3).

`PostgresAuditSink` is the compliance-relevant "who ran what" record; it had no direct test. This
proves an append-only round trip against a real database (CI provides one; the offline sandbox
skips): a recorded `AuditEvent` lands in `audit_events` with every field intact.
"""

import asyncio

import psycopg

from agents.audit import AuditEvent
from agents.audit_store import PostgresAuditSink
from chemclaw.config import settings
from tests.pg import migrated_db_or_skip


def test_postgres_audit_sink_persists_an_event() -> None:
    """Recording an event writes one append-only row with all fields preserved."""

    async def _run() -> None:
        await migrated_db_or_skip()
        correlation_id = "conv-audit-roundtrip"
        event = AuditEvent(
            correlation_id=correlation_id,
            actor="u-oid-1",
            tool="submit_qm_job",
            arguments='{"smiles": "CCO"}',
            outcome="ok",
            detail="job qm-1 started",
            latency_ms=12.5,
        )
        await PostgresAuditSink().record(event)

        # Read the row back and assert every field survived the insert.
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        try:
            cursor = await conn.execute(
                "SELECT actor, tool, arguments, outcome, detail, latency_ms "
                "FROM audit_events WHERE correlation_id = %s ORDER BY id DESC LIMIT 1",
                (correlation_id,),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()

        assert row is not None
        assert row[0] == "u-oid-1"
        assert row[1] == "submit_qm_job"
        assert row[2] == '{"smiles": "CCO"}'
        assert row[3] == "ok"
        assert row[4] == "job qm-1 started"
        assert float(row[5]) == 12.5

    asyncio.run(_run())
