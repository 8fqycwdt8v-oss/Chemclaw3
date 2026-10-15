"""Durable Postgres backing for the tool-audit trail (append-only).

`PostgresAuditSink` writes each `AuditEvent` to `audit_events`. It is separate from
`chemclaw.agent.audit` so the hot-path middleware has no database dependency.

Append-only is a privilege, not a promise: the application role has `INSERT` but neither
`UPDATE` nor `DELETE` on the table. The `prev_hash`, `row_hash` and `chain_version` columns are
unused and stay at their defaults.

Writes are off the tool-call path: `record` appends to an in-process buffer and one flusher task
per sink drains it in batched transactions. Rows still buffered when the process dies are lost;
`flush()` bounds that window and the runner awaits it at turn end. The buffer is bounded by
`agent_audit_buffer_max_events`, shedding the oldest so a slow database cannot exhaust memory.

Invariant: the buffer is shared by every request in the process and the flusher runs in the context
of whichever request started it, so an event carries the tenant of the call that recorded it and a
batch is written per tenant, bound explicitly. An event with no tenant is dropped and counted, never
filed under whoever happens to be flushing.
"""

import asyncio
import logging
from typing import Any

from chemclaw.agent.audit import AuditEvent
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.tenancy import bound_tenant, current_tenant

logger = logging.getLogger(__name__)

# `ts` is bound explicitly, stamped when the call started: buffering means a column default would
# date (and order) rows by flush time, and `chemclaw explain` relies on that order.
_INSERT = """
    INSERT INTO audit_events
        (ts, correlation_id, session_id, purpose, plan_step, actor, agent, tool, arguments,
         outcome, detail, latency_ms, revision, tool_revision)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _count_lost(lost: float, metrics: Any) -> None:
    """Increment the sink-failure counter by the size of a dropped batch."""
    metrics.increment("chemclaw_audit_sink_failures_total", lost)


def _count_shed(shed: float, metrics: Any) -> None:
    """Increment the buffer-shed counter by the number of events dropped to stay in bound."""
    metrics.increment("chemclaw_audit_events_shed_total", shed)


def _row(event: AuditEvent) -> tuple[object, ...]:
    """One event as the parameter tuple `_INSERT` binds."""
    return (
        event.ts,
        event.correlation_id,
        event.session_id,
        event.purpose,
        event.plan_step,
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


def _by_tenant(pending: list[tuple[str, AuditEvent]]) -> dict[str, list[AuditEvent]]:
    """`pending` grouped by the tenant that recorded each event, each group in recorded order."""
    grouped: dict[str, list[AuditEvent]] = {}
    for tenant, event in pending:
        grouped.setdefault(tenant, []).append(event)
    return grouped


class PostgresAuditSink:
    """Append-only `AuditSink` backed by Postgres, batching its writes off the tool-call path."""

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        self._buffer: list[tuple[str, AuditEvent]] = []
        self._flusher: asyncio.Task[None] | None = None
        # Events in a batch still awaiting its round trip; the bound charges buffered plus
        # in-flight.
        self._in_flight = 0
        # How many have been shed since the buffer last came off its bound, and whether it is on
        # it now. Both exist to make the WARNING one-per-episode instead of one-per-event.
        self._shed_since_full = 0
        self._at_bound = False

    async def record(self, event: AuditEvent) -> None:
        """Buffer one audit event and return; the flusher task persists it.

        `async` only to satisfy the `AuditSink` protocol; it never suspends.
        """
        self._buffer.append((current_tenant(), event))
        self._shed_to_bound()
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_all(), name="audit-flush")

    def _shed_to_bound(self) -> None:
        """Drop the oldest buffered events once buffered plus in-flight passes the configured bound.

        Bounds the buffer against a slow database (`_flush_all` handles a down one). The oldest go
        because every event already reached the stdlib log; what is lost is durability, not the
        record. Logs one WARNING on entering the bound; `_drained_notice` reports the total on
        leaving it.
        """
        bound = settings.agent_audit_buffer_max_events
        if not bound:
            return
        resident = len(self._buffer) + self._in_flight
        if resident <= bound:
            return
        from functools import partial

        from chemclaw.core.metrics_bridge import record_metric

        shed = min(resident - bound, len(self._buffer))
        if not shed:  # The whole overage is in flight; the next completed batch releases it.
            return
        del self._buffer[:shed]
        record_metric(partial(_count_shed, float(shed)))
        self._shed_since_full += shed
        if not self._at_bound:
            self._at_bound = True
            logger.warning(
                "audit_buffer_full: the audit buffer reached its %d-event bound and is dropping "
                "the oldest events; the durable trail will have a gap and the stdlib log above "
                "still carries each. The running total is chemclaw_audit_events_shed_total, and "
                "audit_buffer_drained reports it when the sink catches up",
                bound,
            )

    def _drained_notice(self) -> None:
        """Close a shedding episode, logging the total shed.

        Called when a batch lands with nothing left behind. Silent unless something was shed.
        """
        if not self._at_bound:
            return
        logger.warning(
            "audit_buffer_drained: the audit sink caught up after dropping %d audit event(s); "
            "the durable trail has a gap of that size and the stdlib log carries each",
            self._shed_since_full,
        )
        self._at_bound = False
        self._shed_since_full = 0

    async def flush(self) -> None:
        """Wait until everything recorded so far has been written (or failed and been logged).

        The runner awaits this at turn end; tests await it before asserting rows. Never raises.
        """
        while self._flusher is not None and not self._flusher.done():
            await asyncio.shield(self._flusher)
        if self._buffer:
            # A row recorded after the last flusher finished but before anyone awaited: rare, and
            # exactly what a drain must not leave behind.
            await self._flush_all()

    async def _flush_all(self) -> None:
        """Write the buffer in batches until it is empty.

        One transaction per batch via `executemany`. A failed batch is logged, counted and dropped,
        since re-queueing it would grow the buffer without bound while the database is down.
        """
        while self._buffer:
            pending, self._buffer = self._buffer, []
            # The batch stays resident until its round trip returns, so it counts against the bound.
            self._in_flight += len(pending)
            try:
                for tenant, events in _by_tenant(pending).items():
                    await self._write(tenant, events)
            finally:
                self._in_flight -= len(pending)
        self._drained_notice()

    async def _write(self, tenant: str, batch: list[AuditEvent]) -> None:
        """Write one tenant's events in one transaction, acting for that tenant and no other."""
        try:
            if not tenant:
                raise ValueError("no tenant was bound where the event was recorded")
            with bound_tenant(tenant):
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
