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
are counted (`chemclaw_result_publish_failures_total`, or
`chemclaw_result_projection_failures_total` when the payload is what could not be read) and logged
at warning, which is the same
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
from chemclaw.core.logging import log_event
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

# **Claiming spends the attempt, in the same statement that selects the row.** A plain
# `SELECT ... FOR UPDATE SKIP LOCKED` is not enough here: the lock lasts only as long as the
# transaction, and this one has to commit before the delivery is attempted — a delivery can take
# the better part of a minute and must not hold a row lock across it. So two runs overlapping (a
# scheduled drain and an operator's manual one) would both see the same pending rows, deliver them
# twice and each record a failure, double-counting the attempt budget against one destination's
# outage.
#
# Incrementing inside the claim closes that: the `UPDATE` takes a row lock the other run's
# `SKIP LOCKED` respects, and by the time the lock is released the row's attempt is already spent.
# Duplicate *delivery* would still be safe — every key on the far side is a content hash — but the
# accounting would not be, and an attempt budget that empties twice as fast retires rows a working
# destination would have accepted.
#
# Oldest first, so a backlog drains in the order it accumulated and a burst of fresh results cannot
# starve what was already waiting.
_CLAIM = """
    UPDATE result_publications
    SET attempts = attempts + 1
    WHERE id IN (
        SELECT id
        FROM result_publications
        WHERE sink = %s AND state = 'pending' AND attempts < %s
        ORDER BY enqueued_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, calc_ref, document
"""

_MARK_DELIVERED = """
    UPDATE result_publications
    SET state = 'delivered', delivered_at = now(), last_error = ''
    WHERE id = ANY(%s)
"""

# Records why an attempt failed, and retires the row once its budget is gone. **It does not
# increment** — `_CLAIM` already did, which is what makes the count correct under two concurrent
# runs. A retired row is kept, never deleted: it is the record that something was not published,
# and the backfill CLI's `--requeue` is how it comes back.
#
# `RETURNING state` is what makes the dead-letter count exact rather than inferred. Retirement
# happens inside the `CASE`, so from outside the statement a row that spent its last attempt and a
# row with attempts left are one `UPDATE` — which is why nothing counted dead letters at all, and
# why the queued-minus-published difference could never be a backlog: a retired row leaves the
# queue and increments nothing on the published side, forever.
_MARK_FAILED = """
    UPDATE result_publications
    SET last_error = %s,
        state = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END
    WHERE id = ANY(%s)
    RETURNING state
"""

# The backlog, per sink, in the two numbers that are actually a backlog. `count(*)` and
# `min(enqueued_at)` are both served by the partial index `result_publications_pending`
# (`(sink, enqueued_at) WHERE state = 'pending'`), so this is an index-only scan and the age is one
# read of its leading edge per sink.
_PENDING = """
    SELECT sink, count(*), EXTRACT(EPOCH FROM (now() - min(enqueued_at)))
    FROM result_publications WHERE state = 'pending' GROUP BY sink
"""

# Dead letters, per sink. Deliberately a second statement: there is no partial index on `failed`,
# so folding it into the query above with a `FILTER` would take the pending read off its index too.
# Read on the drain pass (every `result_publish_schedule_minutes`), never on a scrape.
_DEAD_LETTERED = """
    SELECT sink, count(*) FROM result_publications WHERE state = 'failed' GROUP BY sink
"""

# The last backlog reading, per sink, for the three gauge families below. Refreshed by the drain,
# read by every scrape — which is the whole point: a gauge that queried per scrape is the objection
# that argued against having one at all.
_PENDING_GAUGE: dict[str, float] = {}
_OLDEST_GAUGE: dict[str, float] = {}
_DEAD_GAUGE: dict[str, float] = {}


def _connect(operation: str) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY).

    `operation` labels `chemclaw_db_query_duration_seconds`, so the enqueue, the claim and the two
    retirement writes are separable in the latency distribution — they have very different shapes
    (an enqueue is per record, a claim is one indexed update) and pooling them would hide both.
    """
    return db.connection(settings.postgres_dsn, operation=operation)


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
        logger.warning(
            "publish[enqueue:sinks]: cannot resolve enabled sinks; nothing queued", exc_info=True
        )
        record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
        return 0

    written = 0
    try:
        async with _connect("outbox_enqueue") as conn:
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
            "publish[enqueue:write]: could not queue %d record(s) for %s",
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
    """Project one stored payload and queue what it becomes.

    Never raises — see the module docstring. Returns how many rows were written, which is **not
    always one**: a shape that decomposes queues the aggregate and its parts (`records_for`), so a
    solvent screen is three rows rather than one.

    The single entry point every hook uses, so "what gets published" is decided in one place rather
    than three. A payload this release has no projector for is skipped with a debug line, not an
    error: `calculation_results` is never pruned, so a deployment legitimately holds rows from
    calculators that no longer ship.

    `payload_kind` is the model's own name and is what routes a *composite*: its `calc_type` is
    `<connector>.<job>`, a route, and no projector prefix matches one. Empty falls back to the
    prefix inference, which is right for a cached primitive whose `calc_type` is its calculator.
    """
    if not publishing_enabled():
        return 0
    # Imported inside the function, deliberately: with no sink configured the projection machinery
    # and RDKit's canonicalization are never imported at all, so the hot cache path pays nothing
    # for a subsystem that is off.
    from chemclaw.publish.project import projector_for, records_for

    if projector_for(calc_type, payload_kind) is None:
        logger.debug("publish: no projector for %s; not queued", calc_type)
        return 0
    try:
        records = records_for(
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
    except Exception:
        # **Every** exception, not a named tuple of them. The tuple was `(ProjectionError,
        # ValueError)`, which is what a projector raises *deliberately* — and measured by mutating
        # each of the 15 fixture shapes, four projectors raise a bare `KeyError` when a field is
        # missing from a list element (`modes[].wavenumber_cm`, `atom_charges[].charge`,
        # `sites[].index`, `points[].energy_hartree`). Those escaped.
        #
        # A live calculation never hit it — pydantic had just produced the payload — but
        # `backfill_cached` walks rows a *different calculator version* wrote, and one of them
        # aborted the whole walk. `backfill.py`'s own docstring promises the opposite ("a walk that
        # aborted on the first one would never reach the rest"), so the narrow tuple was breaking
        # the property the module was built around.
        #
        # The comment below has always stated the right policy; the tuple was narrower than the
        # argument. A publish is best-effort by construction: nothing it can raise is worth failing
        # a calculation that already succeeded and is already persisted.
        # **Counted apart from a publish failure**, because it is a different question with a
        # different answer: a queue write or a delivery that failed may succeed on the next pass,
        # while a projector that raises will raise on every payload of that shape until code
        # changes. One series carrying both made the second look like the first — see the counter's
        # own declaration for the case that proved it.
        logger.exception("publish: could not project %s (%s)", calc_ref, calc_type)
        record_metric(lambda m: m.increment("chemclaw_result_projection_failures_total"))
        return 0
    if publication is not None:
        records = [record.model_copy(update={"publications": [publication]}) for record in records]
    return await enqueue(records)


async def claim(sink: str, limit: int) -> list[tuple[int, str, dict[str, Any]]]:
    """Claim up to `limit` pending rows for `sink`, as `(id, calc_ref, document)`.

    **Claiming spends the attempt** — see `_CLAIM` for why that has to happen in the same statement
    rather than after the delivery.

    **Not a transaction the caller holds.** The claim commits before anything is delivered, because
    a delivery can take the better part of a minute and must not hold row locks across it. So a
    worker that dies mid-delivery leaves its rows `pending` with one attempt spent, and the next
    run picks them up — at-least-once, which is exactly what the content-addressed upserts on the
    far end are built for.
    """
    async with _connect("outbox_claim") as conn:
        cursor = await conn.execute(_CLAIM, (sink, settings.result_publish_max_attempts, limit))
        rows = await cursor.fetchall()
        await conn.commit()
    # The drain pass *is* the refresh point, and this is where a pass reaches the outbox — one
    # extra indexed read per sink per pass, against a `COUNT(*)` on every 15-second scrape. Taken
    # after the claim rather than before it so the reading excludes the rows this pass is about to
    # deliver, which is what makes a falling backlog visible from one pass to the next.
    await refresh_backlog()
    return [(int(row[0]), str(row[1]), row[2]) for row in rows]


async def mark_delivered(ids: list[int]) -> None:
    """Record that these rows reached their sink."""
    if not ids:
        return
    async with _connect("outbox_mark_delivered") as conn:
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
    async with _connect("outbox_mark_failed") as conn:
        cursor = await conn.execute(
            _MARK_FAILED, (reason[:2000], settings.result_publish_max_attempts, ids)
        )
        states = [str(row[0]) for row in await cursor.fetchall()]
        await conn.commit()
    retired = sum(1 for state in states if state == "failed")
    # **This is one delivery attempt per row, and it is not the only thing on this counter.**
    # `chemclaw_result_publish_failures_total` also carries a sink-resolution failure, a local
    # queue-write failure and the enqueue activity's own failure — four unrelated events on one
    # series, which is exactly the argument this module makes for keeping projection failures
    # apart. The counter cannot be split without a label it does not declare, so every site says
    # which stage it is in its log line instead; `stage=delivery` is this one.
    record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total", len(ids)))
    if retired:
        record_metric(lambda m: m.increment("chemclaw_results_dead_lettered_total", retired))
    log_event(
        logger,
        "publish.attempt_failed",
        "publish[delivery]: %d row(s) failed an attempt, %d retired to dead-letter: %s",
        len(ids),
        retired,
        reason[:200],
        level=logging.WARNING if retired else logging.INFO,
        stage="delivery",
        rows=len(ids),
        dead_lettered=retired,
    )


async def refresh_backlog(dsn: str | None = None) -> None:
    """Re-read the backlog and republish the three gauges. Called on the drain pass. Never raises.

    **The documented backlog formula was wrong three ways, and this is what replaces it.**
    `durable/publish_results.py` said the backlog is
    `chemclaw_results_queued_total - chemclaw_results_published_total`, "already exact". Executed:
    queued=10, published=0, failures=50, and the true pending row count was **0**.

    - A row retired to `failed` never increments `published`, so the difference reads 10 forever;
      `failures_total` cannot correct it because `mark_failed` adds `len(ids)` *per attempt*
      (10 rows x 5 attempts = 50).
    - **The two counters live in different processes.** `queued` is incremented in the connector
      worker that finished the calculation and `published` in the `background-jobs` worker that
      drains, while `METRICS` is an in-memory per-process singleton — so restarting either pod
      resets one side of the subtraction, and restarting the calc worker makes it *negative*.
    - `publish/backfill.py` increments `queued` from a short-lived CLI nothing ever scrapes.

    A count and an *age*, because they answer different questions: five rows that turn over every
    second and five rows that have not moved since Tuesday are the same count and a different
    incident. The age is what the "a gauge would need a `COUNT(*)` on every scrape" objection never
    applied to — `min(enqueued_at)` over the pending partial index is a read of its leading edge.

    Sinks that have gone to zero are *kept* at zero rather than dropped from the gauges, because a
    disappearing series and a series reading zero are not the same thing to an alert: a rule on
    "pending > 0 for 30m" silently stops evaluating when the label vanishes.

    Never raises: this is telemetry running inside a drain whose real work is delivery, and a
    backlog read that failed must not fail the pass that would reduce the backlog.
    """
    target = dsn if dsn is not None else settings.postgres_dsn
    try:
        async with db.connection(target, operation="outbox_backlog") as conn:
            cursor = await conn.execute(_PENDING)
            pending = await cursor.fetchall()
            cursor = await conn.execute(_DEAD_LETTERED)
            dead = await cursor.fetchall()
    except Exception:
        logger.warning("publish: could not read the outbox backlog; gauges keep their last value")
        return
    _replace(_PENDING_GAUGE, {str(row[0]): float(row[1]) for row in pending})
    _replace(_OLDEST_GAUGE, {str(row[0]): float(row[2] or 0.0) for row in pending})
    _replace(_DEAD_GAUGE, {str(row[0]): float(row[1]) for row in dead})


def _replace(gauge: dict[str, float], reading: dict[str, float]) -> None:
    """Update a gauge family in place, keeping known sinks that have fallen to zero."""
    gauge.update(dict.fromkeys(gauge, 0.0))
    gauge.update(reading)


def bind_backlog_gauges() -> None:
    """Publish the three backlog gauge families off the last reading (no query on a scrape).

    Called at import, like the ingest cursor lag's: the readings live in this module, so any
    process that drains the outbox is exactly the process that should report its depth, and there
    is no startup hook that could be forgotten.
    """
    record_metric(lambda m: m.bind_gauge_family("chemclaw_outbox_pending", lambda: _PENDING_GAUGE))
    record_metric(
        lambda m: m.bind_gauge_family(
            "chemclaw_outbox_oldest_pending_seconds", lambda: _OLDEST_GAUGE
        )
    )
    record_metric(
        lambda m: m.bind_gauge_family("chemclaw_outbox_dead_lettered", lambda: _DEAD_GAUGE)
    )


bind_backlog_gauges()


__all__ = [
    "CONTRACT_VERSION",
    "claim",
    "enqueue",
    "enqueue_payload",
    "mark_delivered",
    "mark_failed",
    "refresh_backlog",
]
