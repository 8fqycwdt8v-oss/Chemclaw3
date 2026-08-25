"""The durable queue between a finished calculation and the results database.

**Why this exists rather than an inline POST.** A calculation that has finished is science this
deployment already owns. An external results store being unavailable must not fail it — the run
succeeded — and must not lose it either. Publishing inline forces a choice between those two, and
both answers are wrong. An outbox is what refuses the choice: the record is written locally in the
same act that produces it, and a Temporal job drains it with retries.

**Projection happens here, at enqueue, not at drain.** Turning a payload into a record is the step
that can fail on a shape this release cannot read, and failing at enqueue means failing beside the
calculation that produced it, where the context to diagnose it exists. A drain that projected would
surface the same defect hours later inside a background worker, detached from its cause.

**Enqueue never raises into its caller.** Every call site is a *completed* calculation, and a
publish that cannot be queued is strictly less important than the science being returned. Failures
are counted (`chemclaw_result_publish_failures_total`) and logged at warning, which is the same
polarity `publish_note_best_effort` and `notify_session_best_effort` already take for the two other
things that happen after a result is durable.
"""

import logging
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.publish.record import CONTRACT_VERSION, Publication, ResultRecord
from chemclaw.publish.registry import enabled_names, publishing_enabled

logger = logging.getLogger(__name__)

# `ON CONFLICT DO NOTHING` on the identity index is what makes every enqueue path idempotent: the
# three call sites need no coordination, a retried Temporal activity cannot double-queue, and the
# backfill CLI can be run twice with no effect the second time.
_ENQUEUE = """
    INSERT INTO result_publications (sink, calc_ref, document, schema_version)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (sink, calc_ref, schema_version) DO NOTHING
"""

# Oldest first, so a backlog drains in the order it accumulated and a burst of fresh results cannot
# starve what was already waiting. `FOR UPDATE SKIP LOCKED` lets two drain workers share the queue
# without either blocking on the other's batch — the standard claim, and the reason this is a
# `SELECT ... FOR UPDATE` rather than a plain read.
_CLAIM = """
    SELECT id, calc_ref, document
    FROM result_publications
    WHERE sink = %s AND state = 'pending' AND attempts < %s
    ORDER BY enqueued_at
    LIMIT %s
    FOR UPDATE SKIP LOCKED
"""

_MARK_DELIVERED = """
    UPDATE result_publications
    SET state = 'delivered', delivered_at = now(), last_error = ''
    WHERE id = ANY(%s)
"""

# A failed attempt increments rather than transitioning, and only crosses into `failed` once it has
# exhausted its budget. Written as one statement so a row cannot be counted twice by two workers.
_MARK_FAILED = """
    UPDATE result_publications
    SET attempts = attempts + 1,
        last_error = %s,
        state = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END
    WHERE id = ANY(%s)
"""

_PENDING_COUNT = """
    SELECT sink, count(*) FROM result_publications WHERE state = 'pending' GROUP BY sink
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.postgres_dsn)


async def enqueue(records: list[ResultRecord]) -> int:
    """Queue `records` for every enabled sink. Never raises.

    Returns the number of rows written, which a caller may count but need not check: an enqueue
    that failed has already been logged and metered, and the calculation it belongs to succeeded
    regardless.

    With no sink enabled this costs one list lookup and no database round trip at all — which is
    what keeps the cost of this subsystem at zero for a deployment that has not turned it on.
    """
    if not records or not publishing_enabled():
        return 0
    try:
        sinks = enabled_names()
    except Exception:
        logger.warning("publish: cannot resolve enabled sinks; nothing queued", exc_info=True)
        record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
        return 0

    written = 0
    try:
        async with _connect() as conn:
            for record in records:
                document = Jsonb(record.model_dump(mode="json"))
                for sink in sinks:
                    cursor = await conn.execute(
                        _ENQUEUE, (sink, record.calc_ref, document, record.contract_version)
                    )
                    written += cursor.rowcount if cursor.rowcount > 0 else 0
            await conn.commit()
    except Exception:
        logger.warning(
            "publish: could not queue %d record(s) for %s",
            len(records),
            ", ".join(sinks),
            exc_info=True,
        )
        record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
        return 0
    record_metric(lambda m: m.increment("chemclaw_results_queued_total", written))
    return written


async def enqueue_payload(
    *,
    calc_ref: str,
    calc_type: str,
    payload: dict[str, Any],
    payload_kind: str = "",
    calc_version: str = "",
    input_hash: str = "",
    params_hash: str = "",
    structure_id: str = "",
    compute_seconds: float | None = None,
    computed_at: datetime | None = None,
    depends_on: list[str] | None = None,
    publication: Publication | None = None,
) -> int:
    """Project one stored payload and queue it. Never raises — see the module docstring.

    The single entry point every hook uses, so "what gets published" is decided in one place rather
    than three. A payload this release has no projector for is skipped with a debug line, not an
    error: `calculation_results` is never pruned, so a deployment legitimately holds rows from
    calculators that no longer ship.
    """
    if not publishing_enabled():
        return 0
    # Imported inside the function, deliberately: with no sink configured the projection machinery
    # and RDKit's canonicalization are never imported at all, so the hot cache path pays nothing
    # for a subsystem that is off.
    from chemclaw.publish.project import ProjectionError, project, projector_for

    if projector_for(calc_type, payload_kind) is None:
        logger.debug("publish: no projector for %s; not queued", calc_type)
        return 0
    try:
        record = project(
            calc_ref=calc_ref,
            calc_type=calc_type,
            payload=payload,
            payload_kind=payload_kind,
            calc_version=calc_version,
            input_hash=input_hash,
            params_hash=params_hash,
            structure_id=structure_id,
            compute_seconds=compute_seconds,
            computed_at=computed_at,
            depends_on=depends_on,
        )
    except (ProjectionError, ValueError):
        # A payload the projector cannot read is a code gap, and worth an ERROR — but it is still
        # not worth failing the calculation that produced it.
        logger.exception("publish: could not project %s (%s)", calc_ref, calc_type)
        record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
        return 0
    if publication is not None:
        record = record.model_copy(update={"publications": [publication]})
    return await enqueue([record])


async def claim(sink: str, limit: int) -> list[tuple[int, str, dict[str, Any]]]:
    """Claim up to `limit` pending rows for `sink`, as `(id, calc_ref, document)`.

    **Not a transaction the caller holds.** `FOR UPDATE SKIP LOCKED` inside a committed read gives
    each worker a distinct slice without either blocking; the rows are then marked by id after the
    delivery attempt. That means a worker that dies mid-delivery leaves its rows `pending`, which
    is correct: at-least-once is what the content-addressed upserts on the far end are built for.
    """
    async with _connect() as conn:
        cursor = await conn.execute(_CLAIM, (sink, settings.result_publish_max_attempts, limit))
        rows = await cursor.fetchall()
        await conn.commit()
    return [(int(row[0]), str(row[1]), row[2]) for row in rows]


async def mark_delivered(ids: list[int]) -> None:
    """Record that these rows reached their sink."""
    if not ids:
        return
    async with _connect() as conn:
        await conn.execute(_MARK_DELIVERED, (ids,))
        await conn.commit()
    record_metric(lambda m: m.increment("chemclaw_results_published_total", len(ids)))


async def mark_failed(ids: list[int], reason: str) -> None:
    """Record a failed attempt, retiring a row only once it has spent its attempt budget.

    A retired row is never deleted: it is the record that something was *not* published, and an
    operator re-queues it with the backfill CLI once the cause is fixed. Deleting it would turn an
    outage into a silent gap.
    """
    if not ids:
        return
    async with _connect() as conn:
        await conn.execute(_MARK_FAILED, (reason[:2000], settings.result_publish_max_attempts, ids))
        await conn.commit()
    record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total", len(ids)))


async def pending_counts() -> dict[str, int]:
    """How much is waiting, per sink — the gauge an operator watches for a stuck destination."""
    async with _connect() as conn:
        cursor = await conn.execute(_PENDING_COUNT)
        rows = await cursor.fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


__all__ = [
    "CONTRACT_VERSION",
    "claim",
    "enqueue",
    "enqueue_payload",
    "mark_delivered",
    "mark_failed",
    "pending_counts",
]
