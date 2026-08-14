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
    "checkpoints": ("(checkpoint->>'ts')::timestamptz", "TRUE"),
}

# The expired threads, newest-checkpoint-first by age. Grouped rather than filtered row by row
# because the unit of disposal is a thread: `parent_checkpoint_id` chains a thread's checkpoints,
# so removing the old ones from a thread still in use would leave the survivors pointing at rows
# that are gone. `HAVING max(...)` is what makes "this conversation is finished with" the question
# being asked, rather than "this checkpoint is old".
#
# `LIMIT` for the reason `_EXPIRED_SESSIONS` has one: a first pass against a deployment that has
# never pruned faces every thread it has ever had under a 30 s `statement_timeout`, and a pass that
# times out is retried, times out again and deletes nothing. The caller asks for one *over* its cap
# for the other reason `_EXPIRED_SESSIONS` does — to learn whether there is a tail to report.
_EXPIRED_THREADS = (
    "SELECT thread_id FROM checkpoints GROUP BY thread_id "
    "HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s) "
    "ORDER BY thread_id LIMIT %s"
)

# The three statements the per-session conversation prune needs. Only sessions that actually have an
# expired row are visited, so a deployment whose sessions are all recent pays one indexed scan.
#
# `LIMIT` because one activity must not attempt unbounded work. The first pass against a deployment
# that has never pruned faces every session it has ever had, under a 30 s `statement_timeout` per
# statement — and a pass that times out is retried by Temporal, times out again, and exhausts
# `activity_max_attempts` having deleted nothing. A bounded batch makes progress on every pass and
# the schedule drains the tail; the count of what was left is reported rather than dropped.
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

    `sessions_deferred` is how many expired sessions the pass did not reach, because a cap that is
    not reported reads as "there was nothing more": a table still growing would look bounded in
    every result this job returns. Non-zero simply means the next scheduled pass has work.

    `threads_deferred` is the same number for the checkpoint tables, and a separate field rather
    than a sum: the two caps bound different units (a conversation, a checkpoint thread) and an
    operator deciding whether to raise `retention_max_sessions_per_pass` needs to know which one is
    hitting it.
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
    """
    outcome = RetentionOutcome(deleted={}, skipped=[])
    async with connection(settings.postgres_dsn) as conn:
        for table, (column, disposable) in _PRUNABLE.items():
            days = _window_days(table)
            if days <= 0:
                outcome.skipped.append(f"{table} (retention disabled)")
                continue
            if table == "session_messages":
                # Not a single sweeping DELETE: a conversation row's disposability depends on rows
                # that may not be expiring (see the module docstring). Per session, through the
                # pairing closure — and committing per session, which is why no `commit()` follows
                # this call.
                deleted, deferred = await _prune_session_messages(conn, days)
                outcome.deleted[table] = deleted
                outcome.sessions_deferred = deferred
                continue
            if table == "checkpoints":
                # Three tables, one thread, one transaction — see `_prune_checkpoints`. It reports
                # each table separately because that is what an operator can go and look at, and it
                # commits itself, which is why no `commit()` follows this call either.
                counts, skipped, deferred = await _prune_checkpoints(conn, days)
                outcome.deleted.update(counts)
                outcome.skipped.extend(skipped)
                outcome.threads_deferred = deferred
                continue
            async with conn.cursor() as cur:
                # Table and column come from the closed `_PRUNABLE` map above, never from a caller,
                # so the interpolation cannot carry untrusted input; the *value* is bound.
                await cur.execute(
                    f"DELETE FROM {table} "  # noqa: S608
                    f"WHERE {disposable} AND {column} < now() - make_interval(days => %s)",
                    (days,),
                )
                outcome.deleted[table] = cur.rowcount
            await conn.commit()
    return outcome


async def _prune_session_messages(conn: AsyncConnection[TupleRow], days: int) -> tuple[int, int]:
    """Delete expired conversation rows, never splitting a tool-call pairing.

    Returns `(rows deleted, sessions this pass did not reach)`.

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

    The batch is capped and the remainder returned. A first pass against a deployment that has
    never pruned would otherwise take an unbounded number of round trips inside one activity, and
    exceeding `retention_timeout_seconds` costs an attempt having committed only what it reached —
    with the cap it commits a bounded amount and says how much is left.
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

    Returns `(rows deleted per table, tables skipped with the reason, threads not reached)`.

    **The cap is reported, for the reason `_prune_session_messages` reports its own.** One over the
    cap is selected purely to learn whether there is a tail and is never worked; without it, a first
    pass against a deployment with fifty thousand expired threads returns the cap as its deleted
    count and an empty `skipped`, which reads as a drained backlog rather than as one pass of many.

    **One transaction across all three tables, against this module's own per-table rule.** That rule
    exists so one table's failure cannot roll back another's, and it holds because those tables are
    independent. These three are not: they are one thread's state split across three keys with no
    foreign key to enforce it. Committing them separately gives a crash between two commits a choice
    of two bad outcomes — surviving `checkpoints` rows referring to blobs that are gone (a thread
    that now raises when read) or orphaned blobs no later pass can find (because the `HAVING` is
    over `checkpoints`, and that thread no longer has any). One transaction has neither, and it is
    bounded by the batch cap rather than by the backlog.

    **A malformed `ts` fails this pass loudly, and that is the answer rather than an oversight.**
    The thread query casts `checkpoint->>'ts'` to `timestamptz`, and Postgres has no `TRY_CAST` — a
    checkpoint payload whose `ts` is missing or unparseable raises, the activity fails, and Temporal
    surfaces it. Two things make that the right failure. `checkpoints` is last in `_PRUNABLE` and
    every earlier table commits in its own statement, so the pass keeps the disposal it already did.
    And swallowing the error would turn a data-disposal job that *cannot run* into one that reports
    success while a table grows — the exact reading `sessions_deferred` and `threads_deferred` exist
    to prevent. No guard is written for it because none has been needed: `ts` is a field of
    LangGraph's own `Checkpoint`, written by `create_checkpoint` on every write, and a release that
    changed it would break `AsyncPostgresSaver` before it reached this sweep.

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
                f"DELETE FROM {table} WHERE thread_id = ANY(%s)",  # noqa: S608
                (threads,),
            )
            deleted[table] = max(cur.rowcount, 0)
    await conn.commit()
    logger.info(
        "pruned %d expired checkpoint thread(s); %d left for the next pass",
        len(threads),
        deferred,
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
