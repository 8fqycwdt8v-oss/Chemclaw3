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

**The buffer is bounded on the write side too, and it was not for a reason worth naming.**
`_flush_all`'s docstring argues that a *failed* batch must be dropped rather than re-queued,
"because re-queueing it would make a broken database grow the buffer without bound" — which reads
as though the buffer were bounded, and covers only a database that is **down**. A database that is
merely **slow** is the case neither half saw: `record` appends and returns while the drain crawls,
in a pod the chart limits to 1 GiB, at the ~90 rows a turn this module's own first paragraph
measures. `agent_audit_buffer_max_events` bounds it by shedding the oldest and counting the shed on
its own series, because "unreachable" and "cannot keep up" have different remedies.
"""

import asyncio
import logging
from typing import Any

from chemclaw.agent.audit import AuditEvent
from chemclaw.core import db
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# `ts` is bound explicitly rather than left to the column default, and that is the point of it
# being in this list. `record` buffers and returns, so `DEFAULT now()` dated every row at *flush*
# time and `id` — a `BIGSERIAL` also assigned at flush — ordered them the same way. `chemclaw
# explain` reconstructs a turn from that order, so under load the trail's story about a turn was
# the flusher's rather than the tools'. The value comes from `agent/audit.py::_recording`, stamped
# when the call started.
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


class PostgresAuditSink:
    """Append-only `AuditSink` backed by Postgres, batching its writes off the tool-call path."""

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        self._buffer: list[AuditEvent] = []
        self._flusher: asyncio.Task[None] | None = None
        # Events handed to a batch that is still awaiting its round trip. The bound is charged
        # against buffered *plus* in-flight, because `_flush_all` swaps the list out and `record`
        # immediately starts refilling a fresh one: bounding only what `_shed_to_bound` can see
        # left the real ceiling at twice the configured number. Measured at a bound of 10, 400
        # records: peak resident 20.
        self._in_flight = 0
        # How many have been shed since the buffer last came off its bound, and whether it is on
        # it now. Both exist to make the WARNING one-per-episode instead of one-per-event.
        self._shed_since_full = 0
        self._at_bound = False

    async def record(self, event: AuditEvent) -> None:
        """Buffer one audit event and return; the flusher task persists it.

        Still `async` because it implements the `AuditSink` protocol, whose other members do real
        awaiting; this one suspends nowhere, which is exactly what keeps the database out of the
        tool call's latency.
        """
        self._buffer.append(event)
        self._shed_to_bound()
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_all(), name="audit-flush")

    def _shed_to_bound(self) -> None:
        """Drop the oldest buffered events once the buffer passes its configured bound.

        `_flush_all` already refuses to re-queue a *failed* batch, which bounds the buffer against
        a database that is **down**. This bounds it against one that is merely **slow**: `record`
        returns without awaiting, so a producer running at ~90 rows a turn outruns a drain that has
        started taking seconds, and nothing else in this class ever shrinks the list.

        The oldest go because an operator reading this trail is asking what just happened, and
        every event has already reached the stdlib log before it is buffered — so what is lost here
        is durability and ordering, not the record itself.

        **Two things here are corrections to the version that first shipped**, both measured. The
        bound is charged against the in-flight batch as well as the buffer, because `_flush_all`
        takes the list away and `record` refills a fresh one that this method bounds on its own —
        so the real ceiling was `2 * bound`, and every statement of it, here and in the config
        comment and the runbook, was half the truth.

        And the WARNING is one per *episode* rather than one per event. Once the bound is reached
        `shed` is exactly 1 on every subsequent `record`, so at the ~90 rows a turn this module
        measures a slow database produced one WARNING per tool call, each one reading "shed 1" —
        log amplification at precisely the moment an operator is reading logs, and a marker that
        could never name the total the runbook promised it named. It is now logged on entering the
        bound and again in `_drained_notice` on leaving it, where the running total is known.
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
        """Close a shedding episode, naming the total — the number an operator actually needs.

        Called when a batch lands with nothing left behind, which is the only moment the buffer is
        demonstrably off its bound. Silent unless there was something to report.
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
            # The batch is still resident memory until the round trip returns, and `record` is
            # free to run throughout it — so it stays charged against the bound. Without this the
            # ceiling is `2 * bound`, which is what every statement of the bound used to mean.
            self._in_flight += len(batch)
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
            finally:
                self._in_flight -= len(batch)
        self._drained_notice()
