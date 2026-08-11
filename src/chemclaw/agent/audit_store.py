"""Durable Postgres backing for the GxP tool-audit trail (append-only, hash-chained).

`PostgresAuditSink` writes each `AuditEvent` to the `audit_events` table
(`infra/sql/006_audit_events.sql`) — the compliant, queryable "who ran what, when, to what
effect" record the stdlib log alone cannot provide. It is kept separate from `chemclaw.agent.audit`
so the hot-path middleware module carries no database dependency for deployments that run
log-only (the default `NullAuditSink`).

Writes are append-only (no update or delete path) **and tamper-evident**: each row stores the
previous row's `row_hash` as its `prev_hash` and its own `row_hash =
chain_hash(prev_hash, event)` (`infra/sql/011_audit_hash_chain.sql`, plan F10-G1). Modifying,
reordering, or deleting an interior row — or deleting the leading (genesis) rows — breaks the chain,
which `chemclaw.durable.audit_chain` (`make audit-verify`) detects; deleting the trailing rows (tip
truncation) is the one alteration the chain alone cannot catch (see that module's known-limit note).
Appends are serialized with a transaction-level advisory lock so two concurrent inserts cannot read
the same chain tip and fork it — this depends on the connection running in a transaction (psycopg's
default, `autocommit=False`); the lock is `pg_advisory_xact_lock`, released only on commit.
"""

from chemclaw.agent.audit import AuditEvent
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash

# Full 256-bit digest (all 64 hex chars) for the chain link — this is tamper evidence, not a
# content-addressed cache key, so it uses the strongest width `stable_hash` offers.
_CHAIN_HASH_CHARS = 64

# Which field set a row's `row_hash` covers (`infra/sql/026_audit_provenance.sql`,
# `infra/sql/044_audit_agent.sql`, D-2026-07-31-the-audit-chain-is-versioned).
#
# `chain_hash` hashes the whole `AuditEvent`, so **adding a field to that model changes what every
# historical row should hash to**. Widening the event without this would fail verification across
# the entire trail — and a compliance record that reports itself tampered with is worse than one
# that reports nothing, because the first question an auditor asks is which of the two happened,
# and that would be unanswerable. So each row records the shape it was hashed under.
#
# v1 is everything written before `session_id`/`purpose` existed; v2 adds those two; v3 adds `agent`
# (D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor, invariant 3). Reconstructing an older
# shape by *selecting* its keys is exact rather than approximate: `stable_hash` canonicalizes with
# `sort_keys=True`, so the subset serializes byte-identically to what the narrower model dumped.
#
# **Every superseded shape is frozen here, not just the newest one.** The switch used to be a single
# `version < CHAIN_VERSION` test against one field tuple, which was correct while exactly one older
# shape existed and silently wrong the moment a second appeared — it would have hashed v2 rows under
# v1's eight fields and reported the whole middle of the trail as tampered with. A version is a key
# into a table of frozen shapes, so adding v4 is adding a row here and nothing else.
CHAIN_VERSION = 3
_V1_FIELDS = (
    "correlation_id",
    "actor",
    "tool",
    "arguments",
    "outcome",
    "detail",
    "latency_ms",
    "revision",
)
# Written as "v1 plus the two columns migration 026 added" because that is what it is, and stating
# the relationship keeps the two tuples from drifting into disagreeing accounts of one history.
_V2_FIELDS = (*_V1_FIELDS, "session_id", "purpose")
# v3 is the current `AuditEvent` in full, so it has no entry: a version this map does not know is
# hashed over the whole model. That is also what keeps a row written by a *newer* deployment from
# being silently rehashed under an older shape by an older verifier — it fails loudly instead.
_FROZEN_FIELDS: dict[int, tuple[str, ...]] = {1: _V1_FIELDS, 2: _V2_FIELDS}
# A fixed key for the transaction advisory lock that serializes chain appends. Arbitrary but
# stable; scoped to this table's append path so it never contends with unrelated locks.
_AUDIT_CHAIN_LOCK_KEY = 0x43484D4157_00_01  # "CHMAW" + a table-local discriminator

_TIP = "SELECT row_hash FROM audit_events ORDER BY id DESC LIMIT 1"
_INSERT = """
    INSERT INTO audit_events
        (correlation_id, session_id, purpose, actor, agent, tool, arguments, outcome, detail,
         latency_ms, revision, prev_hash, row_hash, chain_version)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def chain_hash(prev_hash: str, event: AuditEvent, *, version: int = CHAIN_VERSION) -> str:
    """The chain link for `event` following `prev_hash`: SHA-256 over both (deterministic).

    Shared by the writer (`PostgresAuditSink.record`) and the verifier
    (`chemclaw.durable.audit_chain`) so the exact bytes hashed can never drift — the single
    definition of "what a row's `row_hash` must be". Every audited field is covered, so tampering
    with any of them changes the hash.

    `version` selects which field set to cover, and the verifier passes each row's stored
    `chain_version` rather than the current one. That is what lets the audited record grow without
    invalidating what is already in it: a v1 row keeps hashing over the eight fields it was written
    with, and a v2 row over its ten, whatever `AuditEvent` gains later.
    """
    payload = event.model_dump()
    frozen = _FROZEN_FIELDS.get(version)
    if frozen is not None:
        payload = {field: payload[field] for field in frozen}
    return stable_hash({"prev": prev_hash, "event": payload}, chars=_CHAIN_HASH_CHARS)


class PostgresAuditSink:
    """Append-only, hash-chained `AuditSink` backed by Postgres. One connection per event."""

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def record(self, event: AuditEvent) -> None:
        """Append one audit event, chained to the current tip under a serializing advisory lock."""
        async with db.connection(self._dsn) as conn:
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
                    prev_hash,
                    row_hash,
                    CHAIN_VERSION,
                ),
            )
            await conn.commit()
