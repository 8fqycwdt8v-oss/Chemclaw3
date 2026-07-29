"""Bounded growth for the durable stores (gap SCH-1).

Nothing in this system ever deleted anything: no `DELETE`, no TTL, no retention window anywhere in
the tree. `session_messages`, `session_events`, `audit_events`, `calculation_results`, `note_index`
and both fingerprint tables grew for the life of the deployment. For a GxP system that is not only
a disk-cost problem — retention is a *requirement* ("keep for N years, then dispose, provably"), and
a records story with no disposal story is incomplete.

**What this prunes, and what it deliberately refuses to.**

- `session_events` — a consumed push-back mailbox row is spent; it exists to wake one stream once.
- `session_messages` — conversation history. Bounded by age, per the deployment's policy, **but an
  age cutoff alone cannot dispose of a conversation row** (D-145). A `tool_use` and the
  `tool_result` answering it are one indivisible unit: delete either half and the API rejects the
  whole thread on every subsequent turn. Rows of one turn are written together and so share a
  `created_at`, but a cutoff is an instant with no knowledge of turns, and a pair *can* straddle it
  — a call retried across a window boundary, a mid-turn-resume interleaving, a clock that moved.
  Worse, the asymmetry in `agent.message_pairing` means only one of the two failures self-heals:
  an orphaned *call* is stripped on the next read, while an orphaned *result* is invisible to the
  repair and bricks the session permanently. So this table is pruned per session through
  `droppable_rows`, which refuses any row whose partner is not also expiring.

- `audit_events` is **refused**, by design, not by omission. The table is hash-chained
  (`infra/sql/011`), so deleting its oldest rows leaves the surviving head pointing at a `prev_hash`
  that no longer exists — indistinguishable from tampering, which is precisely what the chain is
  built to detect. Safe disposal needs archive-then-reseal (export the pruned prefix, verify it,
  record an out-of-band genesis anchor the verifier accepts), which is a design decision with GxP
  consequences and belongs in an ADR with QA sign-off — not in a cleanup job. The job says so out
  loud rather than silently skipping the table.

- `calculation_results` is **refused** for a different reason: D-011 ("never compute twice") is a
  correctness *and* cost guarantee, and evicting a cached result silently converts a cache hit into
  a recomputation — potentially an HPC run. A cache is bounded by cost policy, not by a retention
  clock, so it needs its own eviction design (LRU by access, or by compute cost) rather than an age
  cutoff. Deliberately not lumped in here.

Every prune is age-based against a per-table window, runs on `background-jobs`, and reports what it
removed so the deletion is itself auditable in the job's own result.
"""

from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from agent_framework import Message
    from psycopg import AsyncConnection
    from psycopg.rows import TupleRow

    from chemclaw.agent.message_pairing import droppable_rows
    from chemclaw.core.config import settings
    from chemclaw.core.db import connection
    from chemclaw.durable.registry import durable_activity, durable_workflow

from chemclaw.durable.publish import BAD_DATA_RETRY

# Tables this job is allowed to prune, with the timestamp column that dates a row. Explicit and
# closed: a new table is a deliberate addition here, never something a wildcard sweeps up.
_PRUNABLE: dict[str, str] = {
    "session_events": "created_at",
    "session_messages": "created_at",
}

# The three statements the per-session conversation prune needs. Only sessions that actually have an
# expired row are visited, so a deployment whose sessions are all recent pays one indexed scan.
_EXPIRED_SESSIONS = (
    "SELECT DISTINCT session_id FROM session_messages "
    "WHERE created_at < now() - make_interval(days => %s)"
)
# The whole session, in id order: the pairing closure needs the partners, which are frequently the
# rows that are *not* expiring.
_SESSION_ROWS = "SELECT id, message FROM session_messages WHERE session_id = %s ORDER BY id"
_EXPIRED_IDS = (
    "SELECT id FROM session_messages "
    "WHERE session_id = %s AND created_at < now() - make_interval(days => %s)"
)
_DELETE_IDS = "DELETE FROM session_messages WHERE session_id = %s AND id = ANY(%s)"


class RetentionOutcome(BaseModel):
    """What one retention pass removed, per table — the job's own audit record."""

    deleted: dict[str, int] = {}
    skipped: list[str] = []


def _window_days(table: str) -> int:
    """The configured retention window for `table`, in days. 0 disables pruning for that table."""
    return {
        "session_events": settings.retention_session_events_days,
        "session_messages": settings.retention_session_messages_days,
    }[table]


@durable_activity("background")
@activity.defn
async def prune_expired_rows() -> RetentionOutcome:
    """Delete rows past their table's retention window; return the per-table counts.

    Each table is pruned in its own statement so one failure cannot roll back the others, and the
    cutoff is computed in SQL (`now() - interval`) so the app clock and the database clock cannot
    disagree about what "expired" means.
    """
    outcome = RetentionOutcome(deleted={}, skipped=[])
    async with connection(
        settings.postgres_dsn,
        statement_timeout_seconds=settings.pg_statement_timeout_seconds,
    ) as conn:
        for table, column in _PRUNABLE.items():
            days = _window_days(table)
            if days <= 0:
                outcome.skipped.append(f"{table} (retention disabled)")
                continue
            if table == "session_messages":
                # Not a single sweeping DELETE: a conversation row's disposability depends on rows
                # that may not be expiring (see the module docstring). Per session, through the
                # pairing closure.
                outcome.deleted[table] = await _prune_session_messages(conn, days)
                continue
            async with conn.cursor() as cur:
                # Table and column come from the closed `_PRUNABLE` map above, never from a caller,
                # so the interpolation cannot carry untrusted input; the *value* is bound.
                await cur.execute(
                    f"DELETE FROM {table} WHERE {column} < now() - make_interval(days => %s)",  # noqa: S608
                    (days,),
                )
                outcome.deleted[table] = cur.rowcount
        await conn.commit()
    return outcome


async def _prune_session_messages(conn: AsyncConnection[TupleRow], days: int) -> int:
    """Delete expired conversation rows, never splitting a tool-call pairing; return the count.

    Three statements per session rather than one across the table, because the decision is not
    expressible in SQL: whether an expired row may go depends on whether the rows *paired with it*
    are also going, and those may be newer than the cutoff.

    Reads the session's **whole** history, not just its expired rows. That is the point — a
    candidate's partner being non-expired is exactly the case worth catching, and a partial view
    would report the split component as safe. Sessions are handled one at a time so the memory cost
    is one conversation, not the whole expired backlog.
    """
    deleted = 0
    async with conn.cursor() as cur:
        await cur.execute(_EXPIRED_SESSIONS, (days,))
        session_ids = [row[0] for row in await cur.fetchall()]
    for session_id in session_ids:
        async with conn.cursor() as cur:
            await cur.execute(_SESSION_ROWS, (session_id,))
            rows = [(int(row[0]), Message.from_dict(row[1])) for row in await cur.fetchall()]
            await cur.execute(_EXPIRED_IDS, (session_id, days))
            expired = {int(row[0]) for row in await cur.fetchall()}
            disposable = droppable_rows(rows, expired)
            if not disposable:
                continue
            await cur.execute(_DELETE_IDS, (session_id, sorted(disposable)))
            deleted += max(cur.rowcount, 0)
    return deleted


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
