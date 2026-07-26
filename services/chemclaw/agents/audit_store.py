"""Durable Postgres backing for the GxP tool-audit trail (append-only, hash-chained).

`PostgresAuditSink` writes each `AuditEvent` to the `audit_events` table
(`infra/sql/006_audit_events.sql`) — the compliant, queryable "who ran what, when, to what
effect" record the stdlib log alone cannot provide. It is kept separate from `agents.audit`
so the hot-path middleware module carries no database dependency for deployments that run
log-only (the default `NullAuditSink`).

Writes are append-only (no update or delete path) **and tamper-evident**: each row stores the
previous row's `row_hash` as its `prev_hash` and its own `row_hash =
chain_hash(prev_hash, event)` (`infra/sql/011_audit_hash_chain.sql`, plan F10-G1). Modifying,
reordering, or deleting an interior row — or deleting the leading (genesis) rows — breaks the chain,
which `scripts.verify_audit_chain` (`make audit-verify`) detects; deleting the trailing rows (tip
truncation) is the one alteration the chain alone cannot catch (see that module's known-limit note).
Appends are serialized with a transaction-level advisory lock so two concurrent inserts cannot read
the same chain tip and fork it — this depends on the connection running in a transaction (psycopg's
default, `autocommit=False`); the lock is `pg_advisory_xact_lock`, released only on commit.
"""

from agents.audit import AuditEvent
from chemclaw import db
from chemclaw.config import settings
from chemclaw.ids import stable_hash

# Full 256-bit digest (all 64 hex chars) for the chain link — this is tamper evidence, not a
# content-addressed cache key, so it uses the strongest width `stable_hash` offers.
_CHAIN_HASH_CHARS = 64
# A fixed key for the transaction advisory lock that serializes chain appends. Arbitrary but
# stable; scoped to this table's append path so it never contends with unrelated locks.
_AUDIT_CHAIN_LOCK_KEY = 0x43484D4157_00_01  # "CHMAW" + a table-local discriminator

_TIP = "SELECT row_hash FROM audit_events ORDER BY id DESC LIMIT 1"
_INSERT = """
    INSERT INTO audit_events
        (correlation_id, actor, tool, arguments, outcome, detail, latency_ms,
         revision, prev_hash, row_hash)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def chain_hash(prev_hash: str, event: AuditEvent) -> str:
    """The chain link for `event` following `prev_hash`: SHA-256 over both (deterministic).

    Shared by the writer (`PostgresAuditSink.record`) and the verifier
    (`scripts.verify_audit_chain`) so the exact bytes hashed can never drift — the single
    definition of "what a row's `row_hash` must be". `event.model_dump()` covers every audited
    field, so tampering with any of them changes the hash.
    """
    return stable_hash({"prev": prev_hash, "event": event.model_dump()}, chars=_CHAIN_HASH_CHARS)


class PostgresAuditSink:
    """Append-only, hash-chained `AuditSink` backed by Postgres. One connection per event."""

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def record(self, event: AuditEvent) -> None:
        """Append one audit event, chained to the current tip under a serializing advisory lock."""
        async with await db.connect(
            self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        ) as conn:
            # Serialize appenders so two concurrent inserts cannot read the same tip and fork the
            # chain. The xact lock releases on commit/rollback, bounding contention to one insert.
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_CHAIN_LOCK_KEY,))
            cursor = await conn.execute(_TIP)
            row = await cursor.fetchone()
            prev_hash = row[0] if row is not None else ""
            row_hash = chain_hash(prev_hash, event)
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
                    event.revision,
                    prev_hash,
                    row_hash,
                ),
            )
            await conn.commit()
