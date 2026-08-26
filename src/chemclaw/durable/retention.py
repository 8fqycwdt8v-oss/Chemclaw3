"""Bounded growth for the durable stores (gap SCH-1).

Nothing in this system ever deleted anything: no `DELETE`, no TTL, no retention window anywhere in
the tree. `session_messages`, `session_events`, `audit_events`, `calculation_results`, `note_index`
and both fingerprint tables grew for the life of the deployment. That is not only a disk-cost
problem: a records story with no disposal story is incomplete, and "keep for N years, then dispose"
is a policy a deployment has to be able to state and act on.

**What this prunes, and what it deliberately refuses to.**

- `session_events` — a consumed push-back mailbox row is spent; it exists to wake one stream once.
- `session_messages` — conversation history. Bounded by age, per the deployment's policy, **but an
  age cutoff alone cannot dispose of a conversation row** (D-145). A `tool_use` and the
  `tool_result` answering it are one indivisible unit: delete either half and the API rejects the
  whole thread on every subsequent turn. Rows of one turn are written together and so share a
  `created_at`, but a cutoff is an instant with no knowledge of turns, and a pair *can* straddle it
  — a call retried across a window boundary, a mid-turn-resume interleaving, a clock that moved.
  Worse, nothing repairs the damage afterwards. A read-time repair used to strip an orphaned
  *call*, which made half of this failure self-healing; it went with the MAF thread that needed it
  (D-2026-08-10 §2), so both directions are now permanent. So this table is pruned per session
  through `droppable_rows`, which refuses any row whose partner is not also expiring — the sweep
  has to be right the first time.

- `tool_result_blobs` — the full text of what a tool returned, kept so a surface can fetch it
  (`api/tool_results.py`, migration 042). This is the table that shows what the three refusals
  below actually turn on, because it is the one that holds no *record*: the answers are in
  `calculation_results` and `job_records`, and a trace blob is a view of a turn that already
  happened, so a swept one costs a chemist a rendering they can ask for again and never a
  recomputation. That is what makes a plain `created_at` cutoff sufficient here — no LRU, no cost
  ordering, because ordering evictions by value only pays when what is being ordered is expensive
  to regenerate, and nothing here is. `tool_result_links.content_hash` is `ON DELETE CASCADE`, so
  the link rows go with the blob and this sweep needs no orphan pass.

  Its window still defaults to 0 like the others, and that is a deliberate uniformity rather than
  a considered policy for this table: `retention_enabled` is off by default, so a number here would
  differ from 0 only for a deployment that switched retention on without stating this window —
  which is exactly the case `test_retention_is_off_until_a_policy_is_stated` refuses. The cost is
  that the highest-volume table in this set is unbounded until an operator says otherwise, and
  `infra/sql/README.md` says so rather than implying a bound that does not exist.

- `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` — the LangGraph turn state (D-2026-08-10
  §3). They belong on this list for the same reason everything above does and were missing for a
  reason worth stating: they are created by `AsyncPostgresSaver.setup()` rather than by a migration
  in `infra/sql`, so they appear in no schema review and in no inventory. Erasure already reached
  them per actor (`agent/leaver.py`); disposal did not, so a deployment that erased nobody kept
  every turn's state for its whole life.

  Pruned by **thread**, not by row. A checkpoint chains to the one before it through
  `parent_checkpoint_id`, so deleting the old rows inside a live thread would leave the survivors
  pointing at nothing; a thread expires whole, when its newest checkpoint does. All three tables go
  in one transaction, against the per-table rule below, because they are one thread's state split
  across three keys with no foreign key to enforce it — `_prune_checkpoints` says what committing
  them separately would cost.

  **No migration can add an index to them, and no migration can `ANALYZE` them either.**
  `infra/sql` is applied by a `pre-install` hook Job that completes before any app container starts,
  so on a fresh install these tables do not exist when it runs; a migration is recorded in
  `schema_migrations` on that first run and never re-executed, so a `CREATE INDEX` guarded on the
  table's existence would be a permanent no-op that reads like a control — the `map_to_hpc_identity`
  shape D-2026-08-15 deleted. Measured, the index nobody can add is worth nothing anyway, and for a
  sharper reason than "it did not help much": the index the query would need **cannot be built at
  all**. `CREATE INDEX ... (thread_id, ((checkpoint->>'ts')::timestamptz))` is rejected with
  *functions in index expression must be marked IMMUTABLE*, because casting text to `timestamptz`
  depends on the session's `TimeZone`. The only buildable form stores the **text**, which
  `max((checkpoint->>'ts')::timestamptz)` never reads — measured on 200 000 threads / 600 000 rows,
  adding `(thread_id, (checkpoint->>'ts'))` moved the thread query from 600 ms to 641 ms, i.e.
  slightly the wrong way, for a 2.7 s build and permanent write amplification on the checkpoint
  path.

  What the missing migration *does* cost is **planner statistics**, and that — not the statement —
  is the whole of the problem this sweep ever had on a large table. `_EXPIRED_THREADS` and
  `_ANALYZE_THREADS` carry the measurements.

- `audit_events` is **refused**, by design, not by omission. The trail is the record of who ran
  what, and for a tool call that changed nothing durable it is the *only* record — so disposing of
  it is not a cache decision, it is deciding to stop being able to answer a question about the past.
  Which rows, how old, and exported where first are questions for whoever owns that record; a
  cleanup job on a clock is the wrong place to answer them. The refusal used to be argued from the
  row hash chain that once sat over this table; the chain is gone and the refusal is not, because
  the chain was never the reason. The job says so out loud rather than silently skipping the table.

- `job_records` is **refused**, and it is the newest reason to be careful here (D-157). The table
  exists precisely because a durable run's result used to expire — with Temporal's own history —
  and take a campaign's entire evaluation record with it. Ageing those rows out on a clock would
  restore the failure this system just removed, one retention window later. Its disposal story is
  the same *archive-then-record* design `audit_events` needs, and it belongs in the same ADR.

- `calculation_results` is **refused** for a different reason: D-011 ("never compute twice") is a
  correctness *and* cost guarantee, and evicting a cached result silently converts a cache hit into
  a recomputation — potentially an HPC run. A cache is bounded by cost policy, not by a retention
  clock, so it needs its own eviction design (LRU by access, or by compute cost) rather than an age
  cutoff. Deliberately not lumped in here.

Every prune is age-based against a per-table window, runs on `background-jobs`, and reports what it
removed so the deletion is itself auditable in the job's own result.
"""

import logging
from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from psycopg import AsyncConnection
    from psycopg.rows import TupleRow

    from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
    from chemclaw.agent.message_pairing import droppable_rows, stored_call_ids, unreadable_rows
    from chemclaw.core.config import settings
    from chemclaw.core.db import connection, existing_tables
    from chemclaw.durable.registry import durable_activity, durable_workflow

from chemclaw.durable.publish import BAD_DATA_RETRY

logger = logging.getLogger(__name__)

# Tables this job is allowed to prune, with the timestamp column that dates a row and the extra
# predicate that decides whether a row of that table is disposable at all. Explicit and closed: a
# new table is a deliberate addition here, never something a wildcard sweeps up.
#
# `session_events` carries `consumed_at IS NOT NULL` because the module docstring's justification
# for pruning it is that "a **consumed** push-back mailbox row is spent". Age alone was the whole
# predicate, so an undelivered `job_completed` older than the window was destroyed: a durable job
# that outran the retention window — a QM/HPC run, exactly what this channel exists for — lost its
# completion, the session waited on it forever, and the harness "awaiting job" todo never flipped.
# It also destroyed the `system-audit-integrity` and `system-eval-drift` alerts, which by
# construction are never consumed, so retention silently deleted the evidence. (The first channel
# retired with the audit chain's verifier; the argument stands on the second.)
#
# `tool_result_blobs` carries the bare `TRUE` because there is nothing to qualify: every row is a
# trace blob and every trace blob past its window may go. Its link rows are not listed separately
# and must not be — they cascade from the blob (042), so listing them would be a second, racing
# definition of the same disposal.
#
# `checkpoints` is in the register and, like `session_messages`, is not pruned by the plain cutoff
# the pair describes — `_prune_checkpoints` handles it and the pair records only that the table is
# in scope and what dates a row. It has no timestamp column at all: the checkpoint payload carries
# its own `ts`, which is what the expression names.
_PRUNABLE: dict[str, tuple[str, str]] = {
    "session_events": ("created_at", "consumed_at IS NOT NULL"),
    "session_messages": ("created_at", "TRUE"),
    "tool_result_blobs": ("created_at", "TRUE"),
    # **Delivered rows only**, and the predicate is the whole point rather than an optimization. A
    # delivered publication is a receipt for something that now lives in two places, so keeping
    # every one forever would be a third copy of every result this deployment has computed. A
    # `pending` or `failed` row is the only record that something has *not* been published, and
    # sweeping it on a clock would turn a results-store outage into a silent gap — which is the
    # exact failure the outbox exists to prevent. Dated by `delivered_at`, not `enqueued_at`: a row
    # that waited three weeks for a destination to come back should be kept for its full window
    # after it finally arrived, not deleted on arrival.
    "result_publications": ("delivered_at", "state = 'delivered'"),
    "checkpoints": ("(checkpoint->>'ts')::timestamptz", "TRUE"),
}

# The expired threads. The rule is the only correct one and has never changed: **a thread is expired
# exactly when its newest checkpoint is older than the cutoff.** The unit of disposal is a thread —
# `parent_checkpoint_id` chains a thread's checkpoints, so removing the old ones from a thread still
# in use would leave the survivors pointing at rows that are gone.
#
# **This statement was once replaced by a `WITH RECURSIVE` loose index scan and the replacement was
# reverted, because the premise it rested on was measured false.** That premise was: "an aggregate
# has to build every group before the `LIMIT` above it can discard one, so this plans
# `Seq Scan -> HashAggregate -> Sort -> Limit` and its cost tracks the table rather than the cap."
# It is true only of a table with **no statistics**. With statistics, `GROUP BY thread_id ORDER BY
# thread_id` matches `checkpoints_pkey`'s leading column, so the planner streams the index and the
# `LIMIT` stops it — no sort, no hash, no whole-table aggregate:
#
#     Limit
#       -> GroupAggregate  (Group Key: thread_id, Filter: max(...) < cutoff)
#            -> Index Scan using checkpoints_pkey on checkpoints
#
# Measured on 200 000 threads x 3 checkpoints, all expired, cap 501: this reads **1 504 rows of
# 600 000** and runs in **2.5 ms**, against **21.3 ms** for the walk. On 1 000 000 threads x 3 it is
# **2.5 ms** against the walk's **23.2 ms** — the "first pass against a deployment that never
# pruned" case the walk was written for, where the walk is 9x slower.
#
# **The steady state is what decides it.** Retention runs daily, so every pass after the first faces
# a backlog that is *sparse*: nearly every thread is live and the few expired ones may be anywhere
# in `thread_id` order. No statement can be bounded by the cap there — finding the expired minority
# means visiting every thread, and the only question is what one visit costs. This statement pays
# **one streaming index pass**: on 200 000 live threads / 600 000 rows it reads every row exactly
# once in **593 ms**. The walk pays a random index probe *plus* a correlated `max()` per thread:
# **8 147 ms** for the same answer, 13.7x worse, and it read 2.6x the table (26 003 scan rows on a
# 10 000-row table). Under a 2 s `statement_timeout` the walk is **cancelled** where this completes
# in 618 ms — the walk reaches "cancelled, retried, deletes nothing, forever" *sooner* than the
# statement it replaced, which is the failure it was written to prevent.
#
# Bounding the walk itself (`thread.visited < n` in the recursive term) was measured too and is
# dominated: at 200 000 live threads a visit cap of 501 costs 24.6 ms but looks at 0.25% of the
# table and returns nothing — a livelock, since the next pass starts from the same first 501 live
# threads. Raising it to 20 000 costs 847 ms, already *slower* than this statement's full pass while
# still covering 10%. The bounded walk is faster than this only in proportion to how much of the
# table it refuses to look at; at equal coverage it loses by an order of magnitude, and buying
# coverage back needs a durable resume watermark this job has nowhere to keep.
#
# So the fix for the no-statistics case is statistics, not a different statement — see
# `_ANALYZE_THREADS`. `ORDER BY thread_id` is load-bearing rather than cosmetic: it is what makes
# the primary key usable and the plan streamable, and it also makes a capped pass deterministic.
#
# One over the cap is asked for, for the reason `_EXPIRED_SESSIONS` does: to learn whether a tail
# exists at all. It is a probe, not a count — `RetentionOutcome` says so.
_EXPIRED_THREADS = (
    "SELECT thread_id FROM checkpoints "
    "GROUP BY thread_id "
    "HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s) "
    "ORDER BY thread_id LIMIT %s"
)

# What makes `_EXPIRED_THREADS` plan as the streaming index scan above rather than as a whole-table
# `Seq Scan -> HashAggregate -> Sort`.
#
# `checkpoints` is created by `AsyncPostgresSaver.setup()`, outside `infra/sql`, so no migration
# analyzes it — and until autovacuum first does, the planner has no idea `thread_id` holds hundreds
# of thousands of distinct values and reaches for a parallel hash aggregate. Measured on 200 000
# threads x 3 checkpoints with no statistics, cap 501: `Parallel Seq Scan -> Partial HashAggregate
# -> Sort (external merge, 5.8 MB to disk) -> Finalize GroupAggregate`, **1 526 ms** — against
# **2.5 ms** for the identical statement once analyzed. That window is real and it is exactly the
# first pass on a fresh deployment; it closes at the first autovacuum analyze.
#
# So the sweep analyzes the table itself, every pass, immediately before asking the question. It is
# cheap because `ANALYZE` samples rather than scans: **242 ms** on 600 000 rows, **424 ms** on
# 3 000 000 — a fixed sub-second cost on a job that runs once a day, and it also refreshes the
# statistics this sweep's own deletions invalidate. Measured, the new statistics take effect for
# the planner **inside the sweep's own uncommitted transaction**, which is why this can sit one
# statement ahead of the query it fixes rather than needing a connection of its own.
#
# Unconditional rather than "only when the table has never been analyzed": the conditional needs
# the `reltuples = -1` sentinel (a Postgres internal, version-dependent) to distinguish "never
# analyzed" from "analyzed and empty", and it would still miss the stale-statistics case. A quarter
# of a second a day does not buy that complexity. A role that does not own the table makes
# `ANALYZE` a warning and a no-op rather than an error, so no privilege guard is needed either.
_ANALYZE_THREADS = "ANALYZE checkpoints"

# The three statements the per-session conversation prune needs. Only sessions that actually have an
# expired row are visited, so a deployment whose sessions are all recent pays one indexed scan.
#
# `LIMIT` because one activity must not attempt unbounded work. The first pass against a deployment
# that has never pruned faces every session it has ever had, under a 30 s `statement_timeout` per
# statement — and a pass that times out is retried by Temporal, times out again, and exhausts
# `activity_max_attempts` having deleted nothing. A bounded batch makes progress on every pass and
# the schedule drains the tail; that a tail exists at all is reported rather than dropped.
_EXPIRED_SESSIONS = (
    "SELECT DISTINCT session_id FROM session_messages "
    "WHERE created_at < now() - make_interval(days => %s) "
    "ORDER BY session_id LIMIT %s"
)
# The whole session, in id order: the pairing closure needs the partners, which are frequently the
# rows that are *not* expiring.
# `message_shape` is selected because the pairing rule reads it: the sweep and the transcript
# reader must decide "which serialization is this" the same way, and this is the destructive one.
_SESSION_ROWS = (
    "SELECT id, message, message_shape FROM session_messages WHERE session_id = %s ORDER BY id"
)
_EXPIRED_IDS = (
    "SELECT id FROM session_messages "
    "WHERE session_id = %s AND created_at < now() - make_interval(days => %s)"
)
_DELETE_IDS = "DELETE FROM session_messages WHERE session_id = %s AND id = ANY(%s)"


class RetentionOutcome(BaseModel):
    """What one retention pass removed, per table — the job's own audit record.

    **Both `*_deferred` fields are probes, not counts, and read as "is there a tail" rather than
    "how long is it".** Each is `0` or `1`, because the statement behind it asks for exactly one row
    over the cap and never more: `1` means the backlog outran this pass, `0` means it drained. That
    is deliberate and it is the honest reading — a true remainder needs a second whole-table
    aggregate, measured at 3 444 ms on 3 000 000 rows against the capped query's own 2.5 ms, which
    would make the *report* cost three orders of magnitude more than the work it describes. What the
    fields exist to prevent is the opposite misreading: a cap that is not reported at all makes a
    still-growing table look bounded in every result this job returns.

    `sessions_deferred` and `threads_deferred` are separate fields rather than one flag: the two
    caps bound different units (a conversation, a checkpoint thread) and an operator deciding
    whether to raise `retention_max_sessions_per_pass` needs to know which one is hitting it.
    """

    deleted: dict[str, int] = {}
    skipped: list[str] = []
    sessions_deferred: int = 0
    threads_deferred: int = 0


def _window_days(table: str) -> int:
    """The configured retention window for `table`, in days. 0 disables pruning for that table."""
    return {
        "session_events": settings.retention_session_events_days,
        "session_messages": settings.retention_session_messages_days,
        "tool_result_blobs": settings.retention_tool_results_days,
        "result_publications": settings.retention_result_publications_days,
        "checkpoints": settings.retention_checkpoints_days,
    }[table]


@durable_activity("background")
@activity.defn
async def prune_expired_rows() -> RetentionOutcome:
    """Delete rows past their table's retention window; return the per-table counts.

    Each table is pruned **and committed** in its own statement, so one failure cannot roll back
    the others — with one deliberate exception, the three checkpoint tables, which are one thread's
    state and go together (`_prune_checkpoints` says why). That was the docstring's claim before it
    was true: there was a single `commit()`
    after the loop, so a timeout on the second table discarded the first table's deletions and the
    run reported them as done — a sweep that says it removed rows it then rolled back is worse than
    one that fails outright, because the growth it was meant to bound continues while the log says
    otherwise. Committing per table also bounds each transaction to one table's locks.

    The same argument then applied one level down and was not made there: `session_messages` is
    pruned per *session*, and every session's deletions sat in one transaction that committed after
    the loop. A failure on the four thousandth session discarded the first three thousand nine
    hundred and ninety-nine, and the transaction held its row locks across the whole sweep on the
    single-replica background worker. So the fix is the same fix: commit each session
    (D-2026-08-05-a-sweep-that-commits-once).

    The cutoff is computed in SQL (`now() - interval`) so the app clock and the database clock
    cannot disagree about what "expired" means.

    **One table's failure does not stop the sweep from reaching the others.** The tables in
    `_PRUNABLE` are independent — nothing here reads from more than one of them — so a
    `statement_timeout` or a bad row confined to `session_messages` used to end the whole pass
    before `tool_result_blobs` or `checkpoints` were even attempted: the loop had no `try/except`,
    so an exception from one table's block propagated straight out of this function. Against a
    deployment where that one table has a persistent problem (an oversized session, a malformed
    row), every *other* table would never be pruned again until the first was fixed, and nothing in
    the job's own result said so — only Temporal's activity-failure log, which is not where an
    operator reading a retention report looks. Each table's block is now caught, logged and rolled
    back on its own, so its neighbours still get their turn in the same pass; the first exception is
    re-raised once every table has been attempted, so the activity still fails and Temporal still
    retries — the same outcome as before for the table that actually failed, with the isolation as
    the only change. The rollback matters beyond tidiness: an uncaught error leaves the connection
    in Postgres's aborted-transaction state, where every later statement on it fails too, so without
    it the "still attempt the rest" half of this fix would not work at all.
    """
    outcome = RetentionOutcome(deleted={}, skipped=[])
    first_error: BaseException | None = None
    async with connection(settings.postgres_dsn) as conn:
        for table, (column, disposable) in _PRUNABLE.items():
            days = _window_days(table)
            if days <= 0:
                outcome.skipped.append(f"{table} (retention disabled)")
                continue
            try:
                if table == "session_messages":
                    # Not a single sweeping DELETE: a conversation row's disposability depends on
                    # rows that may not be expiring (see the module docstring). Per session, through
                    # the pairing closure — and committing per session, which is why no `commit()`
                    # follows this call.
                    deleted, deferred = await _prune_session_messages(conn, days)
                    outcome.deleted[table] = deleted
                    outcome.sessions_deferred = deferred
                    continue
                if table == "checkpoints":
                    # Three tables, one thread, one transaction — see `_prune_checkpoints`. It
                    # reports each table separately because that is what an operator can go and look
                    # at, and it commits itself, which is why no `commit()` follows this call
                    # either.
                    counts, skipped, deferred = await _prune_checkpoints(conn, days)
                    outcome.deleted.update(counts)
                    outcome.skipped.extend(skipped)
                    outcome.threads_deferred = deferred
                    continue
                async with conn.cursor() as cur:
                    # Table and column come from the closed `_PRUNABLE` map above, never from a
                    # caller, so the interpolation cannot carry untrusted input; the *value* is
                    # bound.
                    await cur.execute(
                        f"DELETE FROM {table} "
                        f"WHERE {disposable} AND {column} < now() - make_interval(days => %s)",
                        (days,),
                    )
                    outcome.deleted[table] = cur.rowcount
                await conn.commit()
            except Exception as exc:  # isolated per table; re-raised once every table is tried
                await conn.rollback()
                logger.exception(
                    "retention sweep failed for table %s; the other tables are still attempted",
                    table,
                )
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error
    return outcome


async def _prune_session_messages(conn: AsyncConnection[TupleRow], days: int) -> tuple[int, int]:
    """Delete expired conversation rows, never splitting a tool-call pairing.

    Returns `(rows deleted, 1 if expired sessions remain beyond this pass's cap else 0)`.

    Three statements per session rather than one across the table, because the decision is not
    expressible in SQL: whether an expired row may go depends on whether the rows *paired with it*
    are also going, and those may be newer than the cutoff.

    Reads the session's **whole** history, not just its expired rows. That is the point — a
    candidate's partner being non-expired is exactly the case worth catching, and a partial view
    would report the split component as safe. Sessions are handled one at a time so the memory cost
    is one conversation, not the whole expired backlog.

    **One transaction per session.** Each session's deletion is committed before the next is read,
    so a failure part way through keeps everything already removed rather than discarding the whole
    pass — the identical argument `prune_expired_rows` makes for committing per table, which had
    not been made here. It also bounds how long this holds row locks: one session's worth, not the
    entire backlog's, which matters because the sweep shares the single-replica background worker
    with every other scheduled activity.

    The batch is capped and the existence of a tail returned. A first pass against a deployment
    that has never pruned would otherwise take an unbounded number of round trips inside one
    activity, and
    exceeding `retention_timeout_seconds` costs an attempt having committed only what it reached —
    with the cap it commits a bounded amount and says whether anything is left.
    """
    deleted = 0
    cap = settings.retention_max_sessions_per_pass
    async with conn.cursor() as cur:
        await cur.execute(_EXPIRED_SESSIONS, (days, cap + 1))
        session_ids = [row[0] for row in await cur.fetchall()]
    # One over the cap was requested purely to learn whether there is a tail; it is not worked.
    deferred = max(len(session_ids) - cap, 0)
    for session_id in session_ids[:cap]:
        async with conn.cursor() as cur:
            await cur.execute(_SESSION_ROWS, (session_id,))
            # Call ids, not deserialised messages. The rows of one session may be in *either*
            # stored shape — the M6 conversion pass is resumable — and the previous version read
            # them all with MAF's `Message.from_dict`, which raises `TypeError` on a LangChain
            # payload. So the sweep crashed on any session that had taken a turn since the
            # conversion, Temporal retried it to exhaustion, and retention silently stopped for
            # exactly the sessions still in use.
            rows = [(int(row[0]), stored_call_ids(row[1], row[2])) for row in await cur.fetchall()]
            if unreadable := unreadable_rows(rows):
                # Refuse the whole session rather than the row: an unreadable row links to nothing,
                # so pruning around it could strand a pairing it would have protected.
                logger.warning(
                    "skipping retention for session %s: %d row(s) in an unrecognised stored "
                    "shape (ids: %s)",
                    session_id,
                    len(unreadable),
                    ", ".join(str(row_id) for row_id in unreadable[:10]),
                )
                continue
            await cur.execute(_EXPIRED_IDS, (session_id, days))
            expired = {int(row[0]) for row in await cur.fetchall()}
            disposable = droppable_rows(rows, expired)
            if not disposable:
                continue
            await cur.execute(_DELETE_IDS, (session_id, sorted(disposable)))
            deleted += max(cur.rowcount, 0)
        await conn.commit()
    return deleted, deferred


async def _prune_checkpoints(
    conn: AsyncConnection[TupleRow], days: int
) -> tuple[dict[str, int], list[str], int]:
    """Delete every trace of threads whose newest checkpoint has expired.

    Returns `(rows deleted per table, tables skipped with the reason, 1 if a tail remains else 0)`.

    **The pass analyzes `checkpoints` before it queries it, and that one statement is what bounds
    the work.** `_ANALYZE_THREADS` carries the measurement; the short version is that this table is
    created outside `infra/sql`, so nothing ever gives the planner statistics for it, and without
    them `_EXPIRED_THREADS` plans as a whole-table parallel hash aggregate that spills to disk
    (1 526 ms on 600 000 rows) instead of as a `LIMIT`-terminated scan of `checkpoints_pkey`
    (2.5 ms). Analyzing costs 242 ms there and 424 ms on 3 000 000 rows, once a day.

    On a *drained* backlog — every pass after the first, since this job runs daily — no statement
    can be bounded by the cap at all: the few expired threads may be anywhere in `thread_id` order,
    so finding them means visiting every thread. What the cap still buys is a bounded amount of
    *deletion*, and what the analyzed plan buys is that the visit is one streaming index pass
    (593 ms over 200 000 live threads) rather than a random probe per thread (8 147 ms, and
    cancelled under a 2 s statement timeout).

    **The cap is reported, for the reason `_prune_session_messages` reports its own** — and as a
    probe rather than a remainder (`RetentionOutcome` says why). One over the cap is selected
    purely to learn whether a tail exists and is never worked; without it, a first pass against a
    deployment with fifty thousand expired threads returns the cap as its deleted count and an
    empty `skipped`, which reads as a drained backlog rather than as one pass of many.

    **One transaction across all three tables, against this module's own per-table rule.** That rule
    exists so one table's failure cannot roll back another's, and it holds because those tables are
    independent. These three are not: they are one thread's state split across three keys with no
    foreign key to enforce it. Committing them separately gives a crash between two commits a choice
    of two bad outcomes — surviving `checkpoints` rows referring to blobs that are gone (a thread
    that now raises when read) or orphaned blobs no later pass can find (because the thread query
    runs over `checkpoints`, and that thread no longer has any). One transaction has neither, and it
    is bounded by the batch cap rather than by the backlog.

    **A malformed `ts` fails this pass loudly, and that is the answer rather than an oversight.**
    The thread query casts `checkpoint->>'ts'` to `timestamptz`, and Postgres has no `TRY_CAST` — a
    checkpoint payload whose `ts` is missing or unparseable raises, the activity fails, and Temporal
    surfaces it. Two things make that the right failure. `checkpoints` is last in `_PRUNABLE` and
    every earlier table commits in its own statement, so the pass keeps the disposal it already did.
    And swallowing the error would turn a data-disposal job that *cannot run* into one that reports
    success while a table grows — the exact reading `sessions_deferred` and `threads_deferred` exist
    to prevent. No guard is written for it because none has been needed: `ts` is a field of
    LangGraph's own `Checkpoint`, written by `create_checkpoint` on every write, and a release that
    changed it would break `AsyncPostgresSaver` before it reached this sweep. The cast runs over
    every row the grouping scan reaches, so one malformed `ts` anywhere ahead of the cap fails the
    whole checkpoint pass rather than only the pass that would have deleted its thread — earlier
    and louder, which for a job that must not silently stop disposing is the right direction. A
    *missing* `ts` is not that case and needs no guard: `checkpoint->>'ts'` is then SQL `NULL`,
    `max()` ignores it, and a thread with no timestamp at all is simply never expired.

    **Skipped, not failed, when the tables are absent.** They are created by
    `AsyncPostgresSaver.setup()` rather than by a migration, so a deployment that has never run the
    graph engine does not have them — and a sweep that raised there would stop pruning the three
    tables it had already handled on every subsequent pass, which is the opposite of what a
    retention job is for. `core.db.existing_tables` is asked once, because the check cannot live
    inside the `DELETE` (Postgres resolves the relation at parse time).
    """
    async with conn.cursor() as cur:
        present = await existing_tables(cur, CHECKPOINT_TABLES)
        missing = sorted(set(CHECKPOINT_TABLES) - present)
        if missing:
            # All or nothing: the tables are created together by one `setup()`, so a partial set is
            # a schema nobody has, and guessing which half to prune would be inventing a case.
            return {}, [f"{', '.join(missing)} (no checkpointer in this schema)"], 0
        # Before the question, not after: `_EXPIRED_THREADS` only plans as a `LIMIT`-terminated
        # index scan when the planner has statistics for a table no migration can give them to.
        await cur.execute(_ANALYZE_THREADS)
        cap = settings.retention_max_sessions_per_pass
        await cur.execute(_EXPIRED_THREADS, (days, cap + 1))
        found = [str(row[0]) for row in await cur.fetchall()]
        deferred = max(len(found) - cap, 0)
        threads = found[:cap]
        if not threads:
            return dict.fromkeys(CHECKPOINT_TABLES, 0), [], 0
        deleted: dict[str, int] = {}
        for table in CHECKPOINT_TABLES:
            # `CHECKPOINT_TABLES` is a module constant of the checkpointer's own, never a caller's,
            # so the interpolation cannot carry untrusted input; the thread ids are bound.
            await cur.execute(
                f"DELETE FROM {table} WHERE thread_id = ANY(%s)",
                (threads,),
            )
            deleted[table] = max(cur.rowcount, 0)
    await conn.commit()
    logger.info(
        "pruned %d expired checkpoint thread(s); %s",
        len(threads),
        "more remain for the next pass" if deferred else "the backlog is drained",
    )
    return deleted, [], deferred


@durable_workflow("background")
@workflow.defn
class RetentionWorkflow:
    """Enforce the deployment's retention windows on a cadence (gap SCH-1)."""

    @workflow.run
    async def run(self) -> RetentionOutcome:
        """Run one retention pass and return what it removed."""
        return await workflow.execute_activity(
            prune_expired_rows,
            start_to_close_timeout=timedelta(seconds=settings.retention_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )
