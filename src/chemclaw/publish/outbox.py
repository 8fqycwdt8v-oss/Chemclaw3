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
import time
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

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

# `ON CONFLICT DO NOTHING` on the identity index is what makes every enqueue path idempotent: the
# three call sites need no coordination, a retried Temporal activity cannot double-queue, and the
# backfill CLI can be run twice with no effect the second time.
_ENQUEUE = """
    INSERT INTO result_publications (sink, calc_ref, document, schema_version)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (sink, calc_ref, schema_version) DO NOTHING
"""

# "Nobody is working on this row": it has never been claimed, or it was released by
# `mark_delivered`/`mark_failed`, or the claimer that took it is past the ceiling Temporal itself
# gives the drain activity (`result_publish_lease_seconds`) and is therefore gone. One fragment
# rather than two spellings, because `_CLAIM` and `_REAP_EXHAUSTED` have to agree exactly on it:
# if the reaper's idea of an abandoned claim were wider than the claim's, it would dead-letter a
# row another drain is delivering at that moment, and the real failure reason would be lost behind
# the reaper's generic one.
_UNLEASED = "(claimed_at IS NULL OR claimed_at < now() - make_interval(secs => %s))"

# **A claim is a lease, and the lease is what makes two overlapping drains safe.**
#
# Claiming spends the attempt in the same statement that selects the row, and commits before the
# delivery is attempted — a delivery can take the better part of a minute and must not hold a row
# lock across it. That is why `FOR UPDATE SKIP LOCKED` alone was never enough here, which this
# comment used to claim it was: `SKIP LOCKED` excludes only *overlapping transactions*, and this
# one lasts milliseconds, while the case it was written for — a scheduled drain plus an operator's
# manual one — overlaps over the **delivery**, which lasts seconds to a minute. The second run's
# claim therefore happened after the first's commit, saw a row that was still `pending` with
# `attempts < max`, and claimed it again. Measured with a 1.0 s sink and a second drain started
# 0.3 s in: both drains delivered the same row and it came to rest at `attempts=2` for one
# delivery. Duplicate *delivery* is safe — every key on the far side is a content hash, and the
# shipped SQL sink was driven three times over one record and converged exactly — so the harm was
# the accounting: an attempt budget of 8 that empties after 4 real attempts against one
# destination's outage, retiring rows a recovering destination would have accepted.
#
# `claimed_at` closes it by *predicate* rather than by lock duration: a row this pass has claimed
# is not claimable again until its lease expires, so the second run steps over it whether or not
# the first is still inside a transaction.
#
# **A lease is a timestamp, not a fourth state** (D-2026-09-07). The obvious spelling,
# `state='in_flight'`, takes this table from three states to four and every reader of the column
# has to learn it — `_PENDING` and `_ORPHANED` would have to add it back to keep counting an
# undelivered row as backlog, `_MARK_FAILED`'s `state = 'pending'` guard would have to move, and an
# abandoned claim would need a *second* reaper to return it to `pending`. A leased row is still
# `pending`, because that is the truth: it has not been delivered. So every existing reader stays
# correct and `_REAP_EXHAUSTED` below keeps working unchanged.
#
# Oldest first, so a backlog drains in the order it accumulated and a burst of fresh results cannot
# starve what was already waiting.
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
    RETURNING id, calc_ref, document
"""

# **The fourth state the three-state contract does not name, and how a row leaves it.** `_CLAIM`
# spends the attempt and commits *before* the delivery, so every way a pass dies between the claim
# and the mark — a pod eviction, an activity `start_to_close` expiry, the drain's own per-sink
# ceiling being reached after the claim — leaves the row `pending` with an attempt spent and
# `last_error` untouched. Repeat that `result_publish_max_attempts` times and the row is:
# excluded from `_CLAIM` by `attempts < %s` so it is never delivered again; not `'failed'`, so
# `_DEAD_LETTERED` never counts it and `chemclaw_outbox_dead_lettered` reads zero for it forever;
# still `'pending'`, so it is counted forever in `chemclaw_outbox_pending` and ages forever in
# `chemclaw_outbox_oldest_pending_seconds`, which is what `ChemclawResultOutboxStuck` pages on;
# unmatched by `backfill.requeue_failed` (`WHERE state = 'failed'`), so the documented remedy
# resets nothing; and unmatched by retention (`state = 'delivered'`), so it is never pruned.
# Measured with a hanging sink and eight interrupted passes: `('alpha','h1','pending',8,'')`,
# `claim()` returned `[]`, `dead_lettered` was empty, and `requeue_failed()` reset 0 rows.
#
# This statement is the transition that state was missing. A row whose budget is spent and whose
# state is still `pending` has, by construction, no outcome recorded — a claimed row is `pending`
# only until `mark_delivered`/`mark_failed` runs — so retiring it is not a guess about what
# happened, it is the definition of what did not. It runs at the head of every claim for that sink,
# which is the moment the exclusion would otherwise bite silently.
#
# `last_error` is written only when empty, so the last *real* failure a pass did record outranks
# this generic one — the reason an operator needs is the destination's, not the reaper's.
#
# **`_UNLEASED` is what keeps "no outcome recorded" true.** Since a claim is a lease, a row can be
# `pending` with its budget spent *and* be in somebody's hands right now — the drain that spent the
# last attempt is delivering it. Retiring that row would dead-letter a delivery in progress and,
# because `_MARK_FAILED` guards on `state = 'pending'`, the destination's own account of the
# failure would then be dropped in favour of the generic sentence below. Bounded by the same
# predicate the claim uses, this statement can only reach a row nobody is working on.
_REAP_EXHAUSTED = f"""
    UPDATE result_publications
    SET state = 'failed',
        last_error = CASE WHEN last_error = '' THEN %s ELSE last_error END
    WHERE sink = %s AND state = 'pending' AND attempts >= %s AND {_UNLEASED}
    RETURNING id
"""

# Releases the lease as well as recording the outcome: `claimed_at = NULL` is what "nobody is
# working on this row" means, and a delivered row is nobody's.
_MARK_DELIVERED = """
    UPDATE result_publications
    SET state = 'delivered', delivered_at = now(), last_error = '', claimed_at = NULL
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
#
# **`AND state = 'pending'` is what makes that claim true.** `RETURNING state` returns the *new*
# value for every matched row, not only for the rows that changed, so without a guard the count was
# per call rather than per transition: measured, `mark_failed(ids)` on the same three ids twice
# booked `chemclaw_results_dead_lettered_total` 0 → 3 → 6 and logged "3 retired to dead-letter"
# both times, for three retirements. The guard is the transition's own precondition — a claimed row
# is `pending` by construction (`_CLAIM` spends the attempt and leaves the state alone) — so it is
# narrower than `state <> 'failed'` for free, and a `delivered` row can no longer be walked
# backwards into `failed` by a mis-partitioned id list either.
# **`claimed_at = NULL` is the other half of retrying at all.** A row whose attempt failed goes
# back into the queue, and a row that is still leased is not in the queue — so without the release
# a destination's outage would cost one retry per *lease period* rather than one per drain pass,
# which at the shipped numbers is the difference between the next pass and two minutes of nothing.
_MARK_FAILED = """
    UPDATE result_publications
    SET last_error = %s,
        claimed_at = NULL,
        state = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END
    WHERE id = ANY(%s) AND state = 'pending'
    RETURNING state
"""

# The backlog, per sink, in the two numbers that are actually a backlog. `count(*)` and
# `min(enqueued_at)` are both served by the partial index `result_publications_pending`
# (`(sink, enqueued_at) WHERE state = 'pending'`).
#
# **What that costs, from `EXPLAIN (ANALYZE, BUFFERS)` rather than from reasoning about it.** On
# 200k rows with 10k pending across 3 sinks (PostgreSQL 16, everything in shared buffers):
#
#   - freshly `VACUUM`ed: `GroupAggregate ← Index Only Scan` — `Heap Fetches: 0`, 70 buffers,
#     1.8 ms. Still `rows=10000`: it walks **every pending index entry**.
#   - as the table actually is between vacuums: `HashAggregate ← Bitmap Heap Scan ← Bitmap Index
#     Scan`, 4,940 heap blocks, 5,076 buffers, 9.4 ms. Pending rows are the churning ones — every
#     insert and every `_CLAIM` dirties their pages — so their pages are the least likely in the
#     table to be all-visible, which is exactly when an index-only scan degrades.
#
# So the two claims this comment used to make were both wrong: "an index-only scan" is the
# best-case plan and not the steady-state one, and "the age is one read of its leading edge" is
# wrong in every plan — `min(enqueued_at)` is a full aggregate over the pending set, because
# `count(*)` in the same statement has to read all of it anyway. The cost is a function of the
# backlog, which is the honest way to state it: it is cheap because the backlog is normally small,
# not because the plan reads one row.
#
# `min(enqueued_at)` as an **absolute epoch**, not an age. The age is computed in the gauge
# callable against the clock at scrape time, so a drain that has stopped shows a backlog that keeps
# ageing instead of one frozen at its last healthy reading. Computing `now() - min(...)` here put
# the subtraction at refresh time, which meant the number stood still exactly when the drain did —
# and a stopped drain is the outage `ChemclawResultOutboxStuck` exists to catch, so the metric was
# blind to its own headline case. `ingest/eln/cursor.py` had already made this choice and written
# down why; this is that argument applied to the sibling that got it wrong.
#
# **Scoped to the enabled sinks, because the gauge must describe what the drain actually works on.**
# `enqueue` writes one row per *currently enabled* sink and the drain iterates *currently enabled*
# manifests, while this read took every row regardless. So removing a sink from
# `CHEMCLAW_RESULT_SINKS` left its pending rows drained by nobody, pruned by nobody (retention
# sweeps `delivered` only) and requeued by nobody — and counted here forever. Measured:
# `chemclaw_outbox_pending{sink="beta"} 1.0` with `dead_lettered` empty and `requeue_failed()`
# resetting 0 rows, so `ChemclawResultOutboxStuck` ("the drain is not keeping up or has stopped")
# fires permanently for a destination the operator deliberately turned off, with no way to silence
# it but editing the table. Those rows are not lost — they are reported once per pass through
# `degraded()` instead, which is a different fact wanting a different, non-paging rule.
_PENDING = """
    SELECT sink, count(*), EXTRACT(EPOCH FROM min(enqueued_at))
    FROM result_publications WHERE state = 'pending' AND sink = ANY(%s) GROUP BY sink
"""

# Rows queued for a sink no longer enabled. A count per sink, for the log line and the degradation
# counter — never a gauge, because it must not page: an operator who disabled a destination has
# already decided, and what they need is a line saying how much is stranded, not an alert.
_ORPHANED = """
    SELECT sink, count(*) FROM result_publications
    WHERE state = 'pending' AND NOT (sink = ANY(%s)) GROUP BY sink
"""

# Dead letters, per sink. Deliberately a second statement: there is no partial index on `failed`,
# so folding it into the query above with a `FILTER` would take the pending read off its index too.
#
# **With no index on `failed` this is a sequential scan of the whole table, and that is the
# subsystem's most expensive read.** Measured on the same 200k-row population: `Parallel Seq Scan`
# over 200,000 rows to find 5,136 failed ones, 2,478 buffers, ~20 ms — vacuumed or not, since
# nothing here is indexable. It does not shrink over time the way the pending read does: retention
# prunes `delivered` rows only, and a `failed` row is "kept, never deleted" by design, so the scan
# grows with everything this deployment has ever published *and* with everything it has ever
# retired.
#
# Read once per drain pass — every `result_publish_schedule_minutes`, and once for all sinks rather
# than once per sink (see `durable/publish_results._drain_result_publications`, which is where the
# refresh moved to and why). At that cadence 20 ms on 200k rows is not a cost worth an index. It
# becomes one if the table reaches millions of rows, and the fix then is a partial index on
# `(sink) WHERE state = 'failed'` in `infra/sql/`, not a change here.
_DEAD_LETTERED = """
    SELECT sink, count(*) FROM result_publications
    WHERE state = 'failed' AND sink = ANY(%s) GROUP BY sink
"""

# The last backlog reading, per sink, for the three gauge families below. Refreshed by the drain,
# read by every scrape — which is the whole point: a gauge that queried per scrape is the objection
# that argued against having one at all.
_PENDING_GAUGE: dict[str, float] = {}
_OLDEST_ENQUEUED: dict[str, float] = {}
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

    **One record's failure costs one record.** The loop used to run inside a single transaction
    with one `except Exception` around the whole of it, so a document the column refused rolled
    back every good document beside it — and `records_for` decomposes one payload into several, so
    those siblings are one calculation's own facts, not an unrelated grouping. All the log line
    could then say was "could not queue 3 record(s)", which is silent about how many of the three
    were fine.

    **A savepoint per record, not a bare `try`**, because the failures are on both sides of the
    wire and only one of them is survivable without one: psycopg refuses a NUL in its own dumper
    and leaves the transaction healthy, while Postgres refusing a value aborts the transaction, so
    every later `INSERT` fails with `InFailedSqlTransaction` and the final `COMMIT` takes the good
    rows with it anyway. `conn.transaction()` nested inside the outer one is a `SAVEPOINT`, which
    contains both.
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
        # The outer transaction is explicit so that the inner ones are savepoints rather than
        # transactions of their own: without it the first `conn.transaction()` would open — and
        # commit — a transaction per record, turning one batch into N commits.
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

    A refused document is logged and counted **by `calc_ref`**, and costs only itself: that is the
    number an operator needs and the batch-wide line could not give.

    `json_column` rather than a bare `Jsonb` for the reason `chemclaw.core.jsonb` states — a
    non-finite float is not JSON, and letting it travel turns a value this process could have named
    into an `InvalidTextRepresentation` naming a *token*. `publish.record`'s models refuse one at
    projection now, where it is counted as the permanent shape problem it is; this is the boundary
    behind that, for a document those models do not own end to end.
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

    **Not a transaction the caller holds; a lease.** The claim commits before anything is
    delivered, because a delivery can take the better part of a minute and must not hold row locks
    across it — so the exclusion that stops a second drain re-delivering the same row cannot be the
    lock. `claimed_at` is that exclusion, and `result_publish_lease_seconds` is how long it lasts:
    the drain activity's own `start_to_close` ceiling, past which Temporal has already given up on
    the claimer.

    **So a worker that dies mid-delivery leaves its rows leased, and they come back by predicate.**
    The next claim for that sink after the lease expires takes them, with one attempt spent for the
    pass that died — at-least-once, which is exactly what the content-addressed upserts on the far
    end are built for. There is no second timer and no sweeper to schedule: the recovery happens in
    the statement that would otherwise skip the row.

    **And when the budget runs out that way, this retires the row rather than stranding it.** The
    sentence above was true for every attempt but the last: a row that spent its eighth attempt
    without ever being marked stayed `pending` forever, unclaimable, uncounted as a dead letter,
    ageing in the gauge the stuck-outbox alert reads, and untouched by the documented `--requeue`.
    `_REAP_EXHAUSTED` runs first, in the same transaction, and moves exactly those rows to
    `'failed'` — see that statement for the measurement.

    **This no longer refreshes the backlog gauges, and that is the fix rather than an omission.**
    It used to, with a comment saying the reading was taken after the claim "so the reading
    excludes the rows this pass is about to deliver". `_CLAIM` only increments `attempts`; it
    leaves `state = 'pending'` — the whole point of not holding a transaction across the delivery —
    so a claimed row is still pending to `_PENDING`. Measured: three rows, one `claim()`, and
    `chemclaw_outbox_pending{sink="probe"}` read **3.0** with all three still undelivered. The
    gauge published the *pre*-drain depth and held it for a full pass, which is the one reading it
    must never give. The refresh now happens once per pass after every row has been marked — see
    `durable/publish_results._drain_result_publications`.
    """
    budget = settings.result_publish_max_attempts
    lease = settings.result_publish_lease_seconds
    async with _connect("outbox_claim") as conn:
        # Reaped in the same transaction as the claim, so a row can never be both retired here and
        # handed out below: the `attempts >= budget` reap and the `attempts < budget` claim
        # partition the pending set, and one commit publishes both halves. Both halves are also
        # bounded by the *same* lease, so neither can reach a row another drain is delivering.
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
        # The same counter `mark_failed` books, because it is the same transition: a row has left
        # the queue without being published. Booked here rather than left to the gauge so that the
        # dead-letter *rate* an operator alerts on covers this cause too — it was the one that
        # produced nothing at all.
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

    **That holds within a process's lifetime and not across a restart, which is a limit rather than
    a hole.** `_PENDING_GAUGE`, `_OLDEST_ENQUEUED` and `_DEAD_GAUGE` start empty, so between a pod
    starting and its first drain pass the three families are *absent* from `/metrics` — the exact
    state the paragraph above argues against, arriving by a route that paragraph does not cover.
    It cannot be closed by seeding: a fabricated `pending = 0` for every configured sink would be a
    reading nobody has taken, and reading zero for a queue that is actually deep is strictly worse
    than reading nothing. `ingest/eln/cursor.py`'s `_OBSERVED` has the identical shape for the same
    reason, and seeding it with the epoch would page for every source at every restart.

    What closes it is on the alerting side, where absence is expressible:
    `absent_over_time(chemclaw_outbox_pending[1h])` (and the same over
    `chemclaw_outbox_dead_lettered` and `chemclaw_ingest_cursor_lag_seconds`) fires when nothing has
    reported for longer than a drain interval, which is the same fault as a stuck drain and wants
    the same operator. The window has to exceed `result_publish_schedule_minutes`, or an ordinary
    restart pages.

    Never raises: this is telemetry running inside a drain whose real work is delivery, and a
    backlog read that failed must not fail the pass that would reduce the backlog.
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
    # The enqueue epoch of each sink's oldest pending row; the gauge turns it into an age. A sink
    # with nothing pending has no oldest row, and `_replace` zeroes it — which reads as "0 seconds
    # behind", the honest answer for an empty queue.
    _replace(
        _OLDEST_ENQUEUED, {str(row[0]): float(row[2]) for row in pending if row[2] is not None}
    )
    _replace(_DEAD_GAUGE, {str(row[0]): float(row[1]) for row in dead})


def _replace(gauge: dict[str, float], reading: dict[str, float]) -> None:
    """Update a gauge family in place, keeping known sinks that have fallen to zero."""
    gauge.update(dict.fromkeys(gauge, 0.0))
    gauge.update(reading)


def _oldest_pending_seconds() -> dict[str, float]:
    """How long each sink's oldest undelivered row has been waiting, as of *now*.

    Read at scrape time against the stored enqueue epoch rather than stored as an age, and the
    difference is the whole value of the metric: a drain that stops refreshing leaves the count and
    the dead-letter families frozen — which is honest, they are counts of a state nobody has
    re-read — but an *age* that stands still is a lie about the passage of time. Frozen at "120 s
    behind", `ChemclawResultOutboxStuck` would never cross its threshold no matter how long the
    drain stayed down, so the rule whose own description says "the drain is not keeping up or has
    stopped" was blind to the second half.

    Clamped at zero: a row enqueued by a pod whose clock runs ahead of this one would otherwise
    read as a negative age, which is a nonsense an alert cannot interpret.
    """
    now = time.time()
    # **A stored epoch of zero means "nothing pending", not "enqueued in 1970".** `_replace` keeps a
    # sink that has fallen to zero in the family rather than dropping it — the right call, because
    # a disappearing series silently stops an alert evaluating — and this function used to subtract
    # that placeholder from the clock. Measured on a sink whose queue had just drained:
    # `chemclaw_outbox_oldest_pending_seconds{sink="alpha"} = 1788721651`, about 56 years, which
    # fires `ChemclawResultOutboxStuck` at its worst reading at the exact moment the drain is
    # healthiest. The docstring above already stated the intent — *"a sink with nothing pending has
    # no oldest row, and `_replace` zeroes it, which reads as '0 seconds behind'"* — and the
    # arithmetic said the opposite; this is the line that makes the sentence true.
    return {
        sink: 0.0 if enqueued <= 0.0 else max(0.0, now - enqueued)
        for sink, enqueued in _OLDEST_ENQUEUED.items()
    }


def bind_backlog_gauges() -> None:
    """Publish the three backlog gauge families off the last reading (no query on a scrape).

    Called at import, like the ingest cursor lag's: the readings live in this module, so any
    process that drains the outbox is exactly the process that should report its depth, and there
    is no startup hook that could be forgotten.
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
    "refresh_backlog",
]
