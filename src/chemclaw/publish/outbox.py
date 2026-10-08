"""The durable queue between a finished calculation and the results database.

An outbox rather than an inline POST: an unavailable results store must neither fail a finished
calculation nor lose it. The record is written locally in the same act that produces it, and a
Temporal job drains it with retries.

Projection happens at enqueue (`project_payload`), so a shape this release cannot read fails
beside the calculation that produced it, where it can be diagnosed. Enqueue never raises into
its caller: failures are counted (`chemclaw_result_publish_failures_total`, or
`chemclaw_result_projection_failures_total` for an unreadable payload) and logged.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, NamedTuple

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.jsonb import json_column
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import degraded, record_metric
from chemclaw.publish.record import CONTRACT_VERSION, Publication, ResultRecord
from chemclaw.publish.registry import enabled_names, publishing_enabled

logger = logging.getLogger(__name__)


class Lease(NamedTuple):
    """One claimed row's fence: which row, and the attempt number the claim spent on it.

    `claimed_at` says a row is leased but not by whom. `attempts` is the fencing token because
    `_CLAIM` increments it in the same statement that takes the lease: it is monotonic per row, so
    a superseded pass's mark cannot touch a later claim. Passed from `claim` to `mark_*` as one
    value so no call site can forget it.
    """

    row_id: int
    #: The value of `attempts` *after* the claim that handed this row out.
    attempt: int


class ClaimedRow(NamedTuple):
    """A leased row: what to deliver, and the fence that says this pass still owns it."""

    lease: Lease
    calc_ref: str
    document: dict[str, Any]


def _lease_columns(leases: Sequence[Lease]) -> tuple[list[int], list[int]]:
    """Split leases into the two parallel arrays `unnest(bigint[], integer[])` takes.

    Two arrays because psycopg adapts `list[int]` without a registered composite type.
    """
    return [lease.row_id for lease in leases], [lease.attempt for lease in leases]


# `ON CONFLICT DO NOTHING` on the identity index makes every enqueue path idempotent: call sites
# need no coordination, a retried activity cannot double-queue, and the backfill can run twice.
_ENQUEUE = """
    INSERT INTO result_publications (sink, calc_ref, document, schema_version)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (sink, calc_ref, schema_version) DO NOTHING
"""

# "Nobody is working on this row": never claimed, released by a mark, or claimed by a drain past
# `result_publish_lease_seconds` (the activity's own ceiling) and therefore gone. One fragment so
# `_CLAIM` and `_REAP_EXHAUSTED` agree exactly; a wider reaper would dead-letter a live delivery.
_UNLEASED = "(claimed_at IS NULL OR claimed_at < now() - make_interval(secs => %s))"

# A claim is a lease, which is what makes two overlapping drains safe.
#
# The claim spends the attempt and commits before delivery (which can take a minute and must not
# hold a row lock), so `SKIP LOCKED` alone cannot exclude a second drain; `claimed_at` excludes
# it by predicate until the lease expires. Duplicate delivery would be safe (content-hash keys) but
# would burn the attempt budget.
#
# The lease is a timestamp, not a fourth state: a leased row is still `pending` (it has not been
# delivered), so every reader of `state` stays correct. Oldest first, so a backlog drains in
# order. `attempts` is returned with the row as the fence (see `Lease`).
_CLAIM = f"""
    UPDATE result_publications
    SET attempts = attempts + 1, claimed_at = now()
    WHERE id IN (
        SELECT id
        FROM result_publications
        WHERE sink = %s AND state = 'pending' AND attempts < %s AND {_UNLEASED}
        ORDER BY enqueued_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, calc_ref, document, attempts
"""

# Retire rows whose budget is spent but which no mark ever recorded.
#
# A pass that dies between claim and mark leaves the row `pending` with an attempt spent; once
# the budget is gone `_CLAIM` never takes it again, yet it is neither `failed` (so not counted,
# requeued or alerted as a dead letter) nor ever pruned. A spent, unleased, still-`pending` row
# has by construction no outcome recorded, so retiring it is not a guess. Runs at the head of every
# claim for the sink.
#
# `last_error` is written only when empty, so a real recorded failure outranks this generic one.
# Bounded by `_UNLEASED` so a row whose final attempt is being delivered right now is untouched.
_REAP_EXHAUSTED = f"""
    UPDATE result_publications
    SET state = 'failed',
        last_error = CASE WHEN last_error = '' THEN %s ELSE last_error END
    WHERE sink = %s AND state = 'pending' AND attempts >= %s AND {_UNLEASED}
    RETURNING id
"""

# Records delivery and releases the lease (`claimed_at = NULL`).
#
# Matched on the lease, not the id, so a superseded pass cannot touch a later claim; guarded on
# `pending` so a dead-lettered row is never walked to `delivered`. `RETURNING id` makes
# `chemclaw_results_published_total` count transitions, not call arguments.
_MARK_DELIVERED = """
    UPDATE result_publications AS p
    SET state = 'delivered', delivered_at = now(), last_error = '', claimed_at = NULL
    FROM unnest(%s::bigint[], %s::integer[]) AS lease(id, attempt)
    WHERE p.id = lease.id AND p.attempts = lease.attempt AND p.state = 'pending'
    RETURNING p.id
"""

# Records why an attempt failed, and retires the row once its budget is gone. Does not increment:
# `_CLAIM` already did. A retired row is kept, never deleted (`--requeue` brings it back).
#
# `RETURNING state` makes the dead-letter count exact, and `AND state = 'pending'` makes it a
# count of transitions (RETURNING reports every matched row) and stops a `delivered` row being
# walked backwards. `claimed_at = NULL` returns the row to the queue now rather than after a lease
# period. Matched on the lease, for `_CLAIM`'s reason.
_MARK_FAILED = """
    UPDATE result_publications AS p
    SET last_error = %s,
        claimed_at = NULL,
        state = CASE WHEN p.attempts >= %s THEN 'failed' ELSE 'pending' END
    FROM unnest(%s::bigint[], %s::integer[]) AS lease(id, attempt)
    WHERE p.id = lease.id AND p.attempts = lease.attempt AND p.state = 'pending'
    RETURNING p.state
"""

# The backlog per sink: count and oldest enqueue time, from the partial index
# `result_publications_pending`. Both aggregate over the whole pending set, so the cost scales with
# the backlog (cheap because the backlog is normally small).
#
# `min(enqueued_at)` is returned as an absolute epoch; the gauge computes the age at scrape time,
# so a stopped drain shows a backlog that keeps ageing instead of one frozen at its last reading.
#
# Scoped to the enabled sinks, matching what enqueue and the drain work on. Rows stranded by a
# disabled sink are reported through `_ORPHANED`, not here, so they never page.
_PENDING = """
    SELECT sink, count(*), EXTRACT(EPOCH FROM min(enqueued_at))
    FROM result_publications WHERE state = 'pending' AND sink = ANY(%s) GROUP BY sink
"""

# Rows queued for a sink no longer enabled: a count for the log line and degradation counter,
# never a gauge, because a deliberately disabled destination must not page.
_ORPHANED = """
    SELECT sink, count(*) FROM result_publications
    WHERE state = 'pending' AND NOT (sink = ANY(%s)) GROUP BY sink
"""

# Dead letters, per sink. A separate statement: there is no index on `failed`, and a `FILTER` in
# the query above would take the pending read off its index.
#
# This is a sequential scan over a table whose `failed` rows are never pruned, read once per drain
# pass. If the table reaches millions of rows, the fix is a partial index on
# `(sink) WHERE state = 'failed'` in `infra/sql/`.
_DEAD_LETTERED = """
    SELECT sink, count(*) FROM result_publications
    WHERE state = 'failed' AND sink = ANY(%s) GROUP BY sink
"""

# The last backlog reading per sink: refreshed by the drain, read by every scrape, so a scrape
# never queries.
_PENDING_GAUGE: dict[str, float] = {}
_OLDEST_ENQUEUED: dict[str, float] = {}
_DEAD_GAUGE: dict[str, float] = {}


def _connect(operation: str) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout.

    `operation` labels `chemclaw_db_query_duration_seconds` so enqueue, claim and marks have
    separable latency distributions.
    """
    return db.connection(settings.postgres_dsn, operation=operation)


async def enqueue(records: list[ResultRecord]) -> int:
    """Queue `records` for every enabled sink. Never raises.

    Returns the number of rows written; a failed enqueue is already logged and metered. With no sink
    enabled this costs no database round trip.

    One record's failure costs one record: each runs in a savepoint inside one outer transaction,
    because a server-side refusal aborts the transaction (and would take the good rows with it),
    while a client-side refusal leaves it healthy; a savepoint contains both.
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
        # The explicit outer transaction makes the inner ones savepoints rather than one commit per
        # record.
        async with _connect("outbox_enqueue") as conn, conn.transaction():
            for record in records:
                written += await _enqueue_one(conn, record, sinks)
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


async def _enqueue_one(
    conn: psycopg.AsyncConnection[TupleRow], record: ResultRecord, sinks: list[str]
) -> int:
    """Queue one record for every sink, or none of them; return the rows it wrote.

    A refused document is logged and counted by `calc_ref` and costs only itself. `json_column`
    names a non-finite float in this process instead of letting Postgres reject an anonymous token.
    """
    rows = 0
    try:
        async with conn.transaction():
            document = json_column(record.model_dump(mode="json"))
            for sink in sinks:
                cursor = await conn.execute(
                    _ENQUEUE, (sink, record.calc_ref, document, record.contract_version)
                )
                rows += cursor.rowcount if cursor.rowcount > 0 else 0
    except Exception:
        logger.warning(
            "publish[enqueue:write]: %s could not be queued for %s; the rest of its batch is "
            "unaffected",
            record.calc_ref,
            ", ".join(sinks),
            exc_info=True,
        )
        record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
        return 0
    return rows


def project_payload(
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
) -> list[ResultRecord] | None:
    """The records one stored payload becomes, or **None** when the projector raised.

    Never raises. Three states: a list is "queue these", `[]` is "nothing to queue", and `None` is
    "a projector exists and could not read it", which `backfill` needs to count failed rows.
    `enqueue_payload` is this plus the write, for callers that only need a count.
    """
    # Imported lazily: with no sink configured the projection machinery and RDKit are never loaded.
    from chemclaw.publish.project import records_for

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
        # Every exception, not a named set: projectors can raise `KeyError` on rows written by an
        # older calculator, and a backfill walk must not abort on one. Nothing a best-effort publish
        # raises is worth failing a persisted calculation. Counted apart from publish failures,
        # because a projector that raises will raise on every payload of that shape until code
        # changes.
        logger.exception("publish: could not project %s (%s)", calc_ref, calc_type)
        record_metric(lambda m: m.increment("chemclaw_result_projection_failures_total"))
        return None
    if publication is not None:
        records = [record.model_copy(update={"publications": [publication]}) for record in records]
    return records


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

    Never raises. Returns how many rows were written, not always one: a decomposing shape queues the
    aggregate and its parts. The single entry point every hook uses. A payload with no projector is
    skipped with a debug line (the cache holds rows from retired calculators).

    `payload_kind` routes a composite, whose `calc_type` is a `<connector>.<job>` route; empty
    falls back to prefix inference for cached primitives. A zero cannot say why; callers that care
    use `project_payload` and `enqueue`.
    """
    if not publishing_enabled():
        return 0
    # Imported inside the function for the reason `project_payload` states.
    from chemclaw.publish.project import projector_for

    if projector_for(calc_type, payload_kind) is None:
        logger.debug("publish: no projector for %s; not queued", calc_type)
        return 0
    records = project_payload(
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
        publication=publication,
    )
    if records is None:
        return 0
    return await enqueue(records)


async def claim(sink: str, limit: int) -> list[ClaimedRow]:
    """Claim up to `limit` pending rows for `sink`, each with the lease that fences its mark.

    Claiming spends the attempt and commits before delivery (see `_CLAIM`); `claimed_at` is the
    exclusion and lasts `result_publish_lease_seconds`. A worker that dies mid-delivery leaves its
    rows leased, and the next claim after expiry retakes them (at-least-once, which the far side's
    content-addressed upserts absorb). Rows whose budget ran out that way are retired first by
    `_REAP_EXHAUSTED`, in the same transaction.

    Does not refresh the backlog gauges: a claimed row is still `pending`, so the reading would be
    the pre-drain depth. The drain refreshes once per pass after marking.
    """
    budget = settings.result_publish_max_attempts
    lease = settings.result_publish_lease_seconds
    async with _connect("outbox_claim") as conn:
        # Reaped in the same transaction as the claim: the `attempts >= budget` reap and the
        # `attempts < budget` claim partition the pending set, both bounded by the same lease.
        cursor = await conn.execute(
            _REAP_EXHAUSTED,
            (
                f"spent all {budget} attempts without an outcome being recorded; a pass died "
                "between claiming this row and marking it (worker eviction, activity timeout, or "
                "the per-sink delivery ceiling). Requeue with `backfill_publications --requeue` "
                "once the destination is reachable.",
                sink,
                budget,
                lease,
            ),
        )
        reaped = len(await cursor.fetchall())
        cursor = await conn.execute(_CLAIM, (sink, budget, lease, limit))
        rows = await cursor.fetchall()
        await conn.commit()
    if reaped:
        # The same counter `mark_failed` books, since it is the same transition, so the dead-letter
        # rate covers this cause too.
        record_metric(lambda m: m.increment("chemclaw_results_dead_lettered_total", reaped))
        log_event(
            logger,
            "publish.reaped_exhausted",
            "publish[delivery]: %d row(s) for sink %r had spent their attempts with no outcome "
            "recorded and were retired to dead-letter",
            reaped,
            sink,
            level=logging.WARNING,
            stage="delivery",
            sink=sink,
            dead_lettered=reaped,
        )
    return [ClaimedRow(Lease(int(row[0]), int(row[3])), str(row[1]), row[2]) for row in rows]


async def mark_delivered(leases: Sequence[Lease]) -> None:
    """Record that these rows reached their sink, for the leases this pass still holds.

    A row whose lease has moved on is not marked; the pass now holding it will deliver and mark it
    (at-least-once). `chemclaw_results_published_total` counts rows that changed state, so a re-run
    books nothing.
    """
    if not leases:
        return
    async with _connect("outbox_mark_delivered") as conn:
        cursor = await conn.execute(_MARK_DELIVERED, _lease_columns(leases))
        marked = len(await cursor.fetchall())
        await conn.commit()
    if marked:
        record_metric(lambda m: m.increment("chemclaw_results_published_total", marked))
    _log_fenced_off("delivered", len(leases) - marked)


async def mark_failed(leases: Sequence[Lease], reason: str) -> None:
    """Record a failed attempt, retiring a row only once it has spent its attempt budget.

    A retired row is never deleted: it is the record that something was not published, re-queued
    with the backfill CLI once fixed. Fenced on the lease (see `Lease`).
    """
    if not leases:
        return
    async with _connect("outbox_mark_failed") as conn:
        cursor = await conn.execute(
            _MARK_FAILED,
            (reason[:2000], settings.result_publish_max_attempts, *_lease_columns(leases)),
        )
        states = [str(row[0]) for row in await cursor.fetchall()]
        await conn.commit()
    retired = sum(1 for state in states if state == "failed")
    _log_fenced_off("failed", len(leases) - len(states))
    # `chemclaw_result_publish_failures_total` also carries sink-resolution, queue-write and enqueue
    # failures; without a stage label, each site names its stage in the log line (`stage=delivery`).
    record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total", len(states)))
    if retired:
        record_metric(lambda m: m.increment("chemclaw_results_dead_lettered_total", retired))
    log_event(
        logger,
        "publish.attempt_failed",
        "publish[delivery]: %d row(s) failed an attempt, %d retired to dead-letter: %s",
        len(states),
        retired,
        reason[:200],
        level=logging.WARNING if retired else logging.INFO,
        stage="delivery",
        rows=len(states),
        dead_lettered=retired,
    )


def _log_fenced_off(outcome: str, fenced: int) -> None:
    """Say when a mark reached no row, because silence there is indistinguishable from success.

    A fence miss means this pass outran `result_publish_lease_seconds` and its rows were re-claimed.
    Not an error: the new holder will deliver and mark them.
    """
    if fenced <= 0:
        return
    log_event(
        logger,
        "publish.mark_fenced_off",
        "publish[delivery]: %d row(s) could not be marked %s — this pass no longer holds their "
        "lease, so another drain re-claimed them after %.0fs and owns their outcome",
        fenced,
        outcome,
        settings.result_publish_lease_seconds,
        level=logging.WARNING,
        stage="delivery",
        rows=fenced,
        outcome=outcome,
    )


async def refresh_backlog(dsn: str | None = None) -> None:
    """Re-read the backlog and republish the three gauges. Called on the drain pass. Never raises.

    Read from the table, because counter arithmetic (queued minus published) cannot be a backlog:
    retired rows never publish, and the counters live in different processes and reset on restart.
    A count and an *age*, because a fast-turning queue and a stuck one can share a count.

    Sinks that fall to zero are kept at zero rather than dropped, so a rule on "pending > 0" keeps
    evaluating. Between a pod's start and its first pass the families are absent (seeding a reading
    nobody took would be worse); alert on that with `absent_over_time` over a window longer than
    `result_publish_schedule_minutes`.
    """
    target = dsn if dsn is not None else settings.postgres_dsn
    try:
        sinks = enabled_names()
        async with db.connection(target, operation="outbox_backlog") as conn:
            cursor = await conn.execute(_PENDING, (sinks,))
            pending = await cursor.fetchall()
            cursor = await conn.execute(_DEAD_LETTERED, (sinks,))
            dead = await cursor.fetchall()
            cursor = await conn.execute(_ORPHANED, (sinks,))
            orphaned = await cursor.fetchall()
    except Exception:
        logger.warning("publish: could not read the outbox backlog; gauges keep their last value")
        return
    if orphaned:
        degraded(
            logger,
            "result_outbox_orphaned",
            "publish: %s row(s) are queued for sink(s) no longer enabled (%s); nothing drains, "
            "prunes or requeues them. Re-enable the sink to deliver them, or discard them "
            "deliberately",
            sum(int(row[1]) for row in orphaned),
            ", ".join(f"{row[0]}={row[1]}" for row in orphaned),
            level=logging.WARNING,
            exc_info=False,
        )
    _replace(_PENDING_GAUGE, {str(row[0]): float(row[1]) for row in pending})
    # The enqueue epoch of each sink's oldest pending row; the gauge turns it into an age. An empty
    # sink is zeroed by `_replace`.
    _replace(
        _OLDEST_ENQUEUED, {str(row[0]): float(row[2]) for row in pending if row[2] is not None}
    )
    _replace(_DEAD_GAUGE, {str(row[0]): float(row[1]) for row in dead})


async def poll_backlog(stop: asyncio.Event) -> None:
    """Re-read the backlog every `jobs_in_flight_refresh_seconds` until `stop` is set.

    The gauges are this process's last reading, aged at scrape time, so a worker that does not drain
    keeps the age of whatever it last saw growing after a peer has delivered it. Every worker runs
    this on a timer, which makes each one report the table's truth within one interval instead of
    only the one that drained last; a drain that stops still shows the age growing.
    """
    await refresh_backlog()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), settings.jobs_in_flight_refresh_seconds)
        except TimeoutError:
            await refresh_backlog()


def _replace(gauge: dict[str, float], reading: dict[str, float]) -> None:
    """Update a gauge family in place, keeping known sinks that have fallen to zero."""
    gauge.update(dict.fromkeys(gauge, 0.0))
    gauge.update(reading)


def _oldest_pending_seconds() -> dict[str, float]:
    """How long each sink's oldest undelivered row has been waiting, as of *now*.

    Computed at scrape time from the stored epoch, so a drain that stops refreshing still shows the
    age growing and `ChemclawResultOutboxStuck` can fire. Clamped at zero against clock skew.
    """
    now = time.time()
    # A stored epoch of zero means "nothing pending" and reads as 0 seconds, not as an age since
    # 1970.
    return {
        sink: 0.0 if enqueued <= 0.0 else max(0.0, now - enqueued)
        for sink, enqueued in _OLDEST_ENQUEUED.items()
    }


def bind_backlog_gauges() -> None:
    """Publish the three backlog gauge families off the last reading (no query on a scrape).

    Called at import, so any process that drains the outbox reports its depth with no startup hook
    to forget. Every background worker also refreshes on a timer (`poll_backlog`).
    """
    record_metric(lambda m: m.bind_gauge_family("chemclaw_outbox_pending", lambda: _PENDING_GAUGE))
    record_metric(
        lambda m: m.bind_gauge_family(
            "chemclaw_outbox_oldest_pending_seconds", _oldest_pending_seconds
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
    "poll_backlog",
    "refresh_backlog",
]
