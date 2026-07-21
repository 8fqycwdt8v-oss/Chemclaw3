"""Durable Postgres backing for the GxP tool-audit trail (append-only).

`PostgresAuditSink` writes each `AuditEvent` to the `audit_events` table
(`infra/sql/006_audit_events.sql`) as a single insert — the compliant, queryable "who
ran what, when, to what effect" record the stdlib log alone cannot provide. It is kept
separate from `agents.audit` so the hot-path middleware module carries no database
dependency for deployments that run log-only (the default `NullAuditSink`).

Writes are append-only: there is no update or delete path. A tamper-evident hash chain
is a later hardening step (Phase 6, GxP sign-off), noted where the table is defined.
"""

from agents.audit import AuditEvent
from chemclaw import db
from chemclaw.config import settings

_INSERT = """
    INSERT INTO audit_events
        (correlation_id, actor, tool, arguments, outcome, detail, latency_ms)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


class PostgresAuditSink:
    """Append-only `AuditSink` backed by Postgres. One short-lived connection per event."""

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def record(self, event: AuditEvent) -> None:
        """Persist one audit event as a single append-only insert."""
        async with await db.connect(
            self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        ) as conn:
            await conn.execute(
                _INSERT,
                (
                    event.correlation_id,
                    event.actor,
                    event.tool,
                    event.arguments,
                    event.outcome,
                    event.detail,
                    event.latency_ms,
                ),
            )
            await conn.commit()
