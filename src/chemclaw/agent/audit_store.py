"""Durable Postgres backing for the tool-audit trail (append-only).

`PostgresAuditSink` writes each `AuditEvent` to the `audit_events` table
(`infra/sql/006_audit_events.sql`) — the queryable "who ran what, when, to what effect" record the
stdlib log alone cannot provide. It is kept separate from `chemclaw.agent.audit` so the hot-path
middleware module carries no database dependency for deployments that run log-only (the default
`NullAuditSink`).

Writes are append-only, and that is a *privilege* rather than a promise: the application role is
granted `INSERT` on this table and neither `UPDATE` nor `DELETE`
(`infra/sql/grants/app_privileges.sql`), so the credential that writes a row cannot rewrite it. The
trail once carried a per-row hash chain and signed high-water anchors on top of that, built to make
tampering cryptographically detectable for a regulated deployment. Chemclaw is not one, and the
chain cost a serializing advisory lock on every audit write plus a verifier, a schedule and a key to
manage — so the grant is now the whole of the integrity story, and it is stated rather than implied.
The `prev_hash`, `row_hash` and `chain_version` columns still exist because the schema is
forward-only; nothing writes them, and they sit at their defaults.
"""

from chemclaw.agent.audit import AuditEvent
from chemclaw.core import db
from chemclaw.core.config import settings

_INSERT = """
    INSERT INTO audit_events
        (correlation_id, session_id, purpose, actor, agent, tool, arguments, outcome, detail,
         latency_ms, revision, tool_revision)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class PostgresAuditSink:
    """Append-only `AuditSink` backed by Postgres. One connection per event."""

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def record(self, event: AuditEvent) -> None:
        """Append one audit event.

        A plain insert, with no lock and no read: rows are independent of each other, so concurrent
        appenders contend only for the table the way any other writer does.
        """
        async with db.connection(self._dsn) as conn:
            await conn.execute(
                _INSERT,
                (
                    event.correlation_id,
                    event.session_id,
                    event.purpose,
                    event.actor,
                    event.agent,
                    event.tool,
                    event.arguments,
                    event.outcome,
                    event.detail,
                    event.latency_ms,
                    event.revision,
                    event.tool_revision,
                ),
            )
            await conn.commit()
