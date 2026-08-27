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

**The write is off the tool-call path, and that is this module's second job.** `record` used to
borrow a pooled connection, INSERT and COMMIT before the tool's result could propagate — one
database round trip serialized into *every* tool call, and a parallel batch of K calls holding K of
the pool's connections at once for a row nobody reads mid-turn. `record` now appends to an
in-process buffer and returns; a single flusher task per sink drains the buffer in batches, so a
30-step turn's ~90 rows land as a handful of `executemany` transactions that overlap the model's
own work instead of preceding it. The trade is the one `agent/turn_cost.py` already made for the
same reason and documents in the same words: telemetry booked off the hot path can be lost if the
process dies with rows still buffered, and failing a tool call that already answered in order to
record it would be the tail wagging the dog. `flush()` is the seam that bounds the window — the
runner awaits it at turn end, and a test awaits it before asserting rows.
"""

import asyncio
import logging
from typing import Any

from chemclaw.agent.audit import AuditEvent
from chemclaw.core import db
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

_INSERT = """
    INSERT INTO audit_events
        (correlation_id, session_id, purpose, actor, agent, tool, arguments, outcome, detail,
         latency_ms, revision, tool_revision)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _count_lost(lost: float, metrics: Any) -> None:
    """Increment the sink-failure counter by the size of a dropped batch."""
    metrics.increment("chemclaw_audit_sink_failures_total", lost)


def _row(event: AuditEvent) -> tuple[object, ...]:
    """One event as the parameter tuple `_INSERT` binds."""
    return (
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
    )


class PostgresAuditSink:
    """Append-only `AuditSink` backed by Postgres, batching its writes off the tool-call path."""

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        self._buffer: list[AuditEvent] = []
        self._flusher: asyncio.Task[None] | None = None

    async def record(self, event: AuditEvent) -> None:
        """Buffer one audit event and return; the flusher task persists it.

        Still `async` because it implements the `AuditSink` protocol, whose other members do real
        awaiting; this one suspends nowhere, which is exactly what keeps the database out of the
        tool call's latency.
        """
        self._buffer.append(event)
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_all(), name="audit-flush")

    async def flush(self) -> None:
        """Wait until everything recorded so far has been written (or failed and been logged).

        The seam that bounds the off-path window: the runner awaits this at turn end so a
        completed turn leaves no buffered rows behind, and a test awaits it before asserting
        table contents. Never raises — a failed batch already logged itself inside the flusher.
        """
        while self._flusher is not None and not self._flusher.done():
            await asyncio.shield(self._flusher)
        if self._buffer:
            # A row recorded after the last flusher finished but before anyone awaited: rare, and
            # exactly what a drain must not leave behind.
            await self._flush_all()

    async def _flush_all(self) -> None:
        """Write the buffer in batches until it is empty.

        One connection and one transaction per batch rather than per row: rows are independent,
        so `executemany` under a single COMMIT is the same trail at a fraction of the round trips.
        A failed batch is logged and *dropped* — re-queueing it would make a broken database grow
        the buffer without bound, and `chemclaw_audit_sink_failures_total` (incremented by the
        caller-side `_emit` for sinks that raise) has a flusher-side twin here for the same
        dashboard.
        """
        while self._buffer:
            batch, self._buffer = self._buffer, []
            try:
                async with db.connection(self._dsn) as conn:
                    async with conn.cursor() as cur:
                        await cur.executemany(_INSERT, [_row(event) for event in batch])
                    await conn.commit()
            except Exception:
                from functools import partial

                from chemclaw.core.metrics_bridge import record_metric

                lost = float(len(batch))
                record_metric(partial(_count_lost, lost))
                logger.exception(
                    "audit_sink_failure: %d buffered audit event(s) could not be written and "
                    "are lost to the durable trail (the stdlib log above still carries each)",
                    len(batch),
                )
