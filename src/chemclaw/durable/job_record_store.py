"""Postgres backing for the durable job record (`infra/sql/023_job_records.sql`, D-157).

Separate from `chemclaw.durable.job_record` so processes without Postgres never import psycopg.

Writes are an upsert on `job_id` (the deterministic idempotency key), so retries and re-runs of
one id keep exactly one row describing the latest run. Two upserts: a record carrying a result
replaces the row entire; a record that leaves every result column empty (a failure after a
completed write) refreshes only the rest and never clears a stored result. See
`_says_nothing_about_a_result`.
"""

from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable.job_record import JobRecord, JobRecordSearch, JobRecordSummary

_COLUMNS = (
    "job_id, connector, job, rationale, requested_by, session_id, correlation_id, "
    "plan_step, plan_hash, payload, summary, result, note_id, calc_refs, runtime_seconds, "
    "payload_kind, state, failure_reason"
)

# Every column a second write for the same job id may refresh, including the attribution, so the
# row describes the latest run whole rather than mixing two runs' reasons and requesters.
_MUTABLE = (
    "rationale",
    "requested_by",
    "session_id",
    "correlation_id",
    "plan_step",
    "plan_hash",
    "payload",
    "summary",
    "result",
    "note_id",
    "calc_refs",
    "runtime_seconds",
    "payload_kind",
    "state",
    "failure_reason",
)

# The five columns that say what a run produced. Named once and subtracted, so a new result column
# is protected by adding it here.
_RESULT_COLUMNS = ("summary", "result", "note_id", "calc_refs", "payload_kind")


def _says_nothing_about_a_result(record: JobRecord) -> bool:
    """Whether this record leaves every result column empty, and so must not clear one.

    A property of the record, not of its state: a failed template run legitimately carries the
    results of the steps that completed, and must replace the previous run's results rather than
    sit beside them.
    """
    return not (
        record.summary or record.result or record.note_id or record.calc_refs or record.payload_kind
    )


def _upsert(columns: tuple[str, ...]) -> str:
    """The insert-or-update statement that refreshes exactly `columns` on a conflicting job id."""
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns)
    placeholders = ", ".join("%s" for _ in _COLUMNS.split(", "))
    return f"""
    INSERT INTO job_records ({_COLUMNS})
    VALUES ({placeholders})
    ON CONFLICT (job_id) DO UPDATE SET
        {assignments},
        completed_at = now()
"""


_UPSERT = _upsert(_MUTABLE)
# Named for what it does: keep the stored result columns.
_KEEP_RESULT_UPSERT = _upsert(tuple(c for c in _MUTABLE if c not in _RESULT_COLUMNS))

_SELECT_ONE = f"SELECT {_COLUMNS}, completed_at FROM job_records WHERE job_id = %s"

# The listing projection: everything needed to recognise a run, none of the result blob. Both
# filters self-disable through the `%s = ''` arm; with `plan_cache_mode=force_custom_plan` the
# planner folds it away.
#
# `ILIKE` with a leading wildcard is served by the `gin_trgm_ops` indexes from migration `081`,
# which keep substring semantics (a `tsvector` would change what matches). Without them a
# non-matching term scans the whole never-pruned table.
#
# Ordered by `(completed_at, job_id)`, a total order, so pages neither repeat nor skip rows. The
# keyset anchor is a `job_id` whose position the subquery reads; safe because `job_records` is
# never pruned.
_SEARCH = """
    SELECT job_id, connector, job, rationale, summary, note_id, plan_step, state, completed_at
    FROM job_records
    WHERE (%s = '' OR connector = %s)
      AND (%s = '' OR rationale ILIKE %s OR summary ILIKE %s OR job ILIKE %s)
      AND (
        %s = ''
        OR (completed_at, job_id)
           < (SELECT completed_at, job_id FROM job_records WHERE job_id = %s)
      )
    ORDER BY completed_at DESC, job_id DESC
    LIMIT %s
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.postgres_dsn)


class PostgresJobRecordSink:
    """Writes each finished job's record to `job_records`, one connection per record."""

    async def record(self, record: JobRecord) -> None:
        """Insert the record, refreshing what this particular record is entitled to refresh.

        A record carrying a result replaces the row entire; one carrying none sets how the run ended
        and leaves the result columns alone (see `_says_nothing_about_a_result`).
        """
        async with _connect() as conn:
            await conn.execute(
                _KEEP_RESULT_UPSERT if _says_nothing_about_a_result(record) else _UPSERT,
                (
                    record.job_id,
                    record.connector,
                    record.job,
                    record.rationale,
                    record.requested_by,
                    record.session_id,
                    record.correlation_id,
                    record.plan_step,
                    record.plan_hash,
                    # psycopg adapts a mapping to `jsonb` only through its `Jsonb` wrapper — a bare
                    # dict is rejected by the adapter, not silently stringified.
                    Jsonb(record.payload),
                    record.summary,
                    Jsonb(record.result),
                    record.note_id,
                    record.calc_refs,
                    record.runtime_seconds,
                    record.payload_kind,
                    record.state,
                    record.failure_reason,
                ),
            )
            await conn.commit()


async def read_job_record(job_id: str) -> JobRecord | None:
    """The full record for one job, or None when the table has no row for it.

    Built by column name via `class_row`, so the SELECT list and the model are one declaration.

    Raises:
        pydantic.ValidationError: The SELECT and the model no longer describe the same row
            (`JobRecord` is `extra="forbid"`). Deliberately not caught.
    """
    async with _connect() as conn:
        async with conn.cursor(row_factory=class_row(JobRecord)) as cursor:
            await cursor.execute(_SELECT_ONE, (job_id,))
            return await cursor.fetchone()


async def read_job_record_summaries(
    text: str, connector: str, limit: int, after: str = ""
) -> JobRecordSearch:
    """Past runs matching the (optional) text and connector filters, newest first.

    One row beyond `limit` is fetched and dropped so `hits_truncated` is exact: a false "an older
    run may exist" could cost a duplicate expensive run.

    Args:
        text: Substring to look for in the reason, the summary or the job name; empty matches all.
        connector: Restrict to one bundle; empty searches all.
        limit: The page size.
        after: The `job_id` of the previous page's last row — the keyset anchor. Empty starts at
            the newest run.

    Returns:
        The page, and whether more matched than it holds.
    """
    pattern = f"%{text}%"
    async with _connect() as conn:
        async with conn.cursor(row_factory=class_row(JobRecordSummary)) as cursor:
            await cursor.execute(
                _SEARCH,
                (connector, connector, text, pattern, pattern, pattern, after, after, limit + 1),
            )
            rows = await cursor.fetchall()
    # The extra row is dropped here rather than in SQL — see the docstring for why it is fetched.
    return JobRecordSearch(hits=rows[:limit], hits_truncated=len(rows) > limit)
