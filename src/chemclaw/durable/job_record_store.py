"""Postgres backing for the durable job record (`infra/sql/023_job_records.sql`, D-157).

Kept separate from `chemclaw.durable.job_record` for the reason `chemclaw.agent.audit_store` is
kept separate from `chemclaw.agent.audit`: the module the workflow and the agent tools import
carries no database dependency, so a deployment (or a test, or a connector worker) that runs
without Postgres never pulls psycopg for a store it will not use.

Writes are an **upsert on `job_id`**, not an append. The id is the deterministic idempotency key
(`connectors/jobs.py::job_workflow_id`), a Temporal activity is at-least-once, and a re-run of a
failed job legitimately produces a second, better result for the same id — so "the record of this
run" must have exactly one row in all three cases.

**Two upserts, because a record with nothing to say about a result must not say it loudly enough
to erase one.** A completed record refreshes every mutable column; so does a failed one that
carries a result of its own (a template run keeps the steps that did complete). A failed record
that leaves all five result columns empty refreshes only the rest, and never clears them.
Measured on 2026-08-28 against a live database: writing a failure record over the completed row
for one job id turned
`{'summary': 'dG = -12.3 kJ/mol', 'result': {'dg': -12.3}, 'note_id': 'note-1',
'calc_refs': ['k1', 'k2'], 'state': 'completed'}` into
`{'summary': '', 'result': {}, 'note_id': '', 'calc_refs': [], 'state': 'failed'}` — the scientific
result of a finished run destroyed by the bookkeeping of the step that failed after it. That is
reachable whenever the record activity commits and then overruns its own timeout (`record_job`'s
docstring names exactly that case), because the workflow then believes no row was written.

The asymmetry is the point rather than an omission: a record that is the whole account of a run
replaces the row entire, which is what lets a re-run of a failed job supersede it. A record that
has nothing to say about a result says nothing, instead of saying nothing loudly enough to erase
one. Which of the two a failure is depends on the failure — see `_says_nothing_about_a_result`.
"""

from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable.job_record import JobRecord, JobRecordSummary

_COLUMNS = (
    "job_id, connector, job, rationale, requested_by, session_id, correlation_id, "
    "plan_step, plan_hash, payload, summary, result, note_id, calc_refs, runtime_seconds, "
    "payload_kind, state, failure_reason"
)

# Every column a second write for the same job id may refresh, **including the attribution**.
# Updating the reason and the result while keeping the first run's `requested_by` was a row that
# contradicted itself: a second execution under this id is a different person asking a differently-
# worded question (the id is reused when Temporal has expired the first execution, which is exactly
# the horizon this table exists for), and half-updating left run 2's reason beside run 1's name —
# the worst possible answer for the field an audit joins on. The row describes the latest run,
# whole.
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

# The five columns that say what a run *produced*. Named once and subtracted, so a new result
# column is protected by being added here instead of by remembering to omit it from a second SQL
# literal.
_RESULT_COLUMNS = ("summary", "result", "note_id", "calc_refs", "payload_kind")


def _says_nothing_about_a_result(record: JobRecord) -> bool:
    """Whether this record leaves every result column empty, and so must not clear one.

    **The property is the record's, not its state's, and reading it as the state's reopened the
    self-contradicting row `_MUTABLE` above was fixed to close.** `connector_job.failed_job_record`
    fills none of these — a failure has no envelope to take one from — which is where the
    protection below comes from. `template_job.failed_template_record` fills `result` and
    `payload_kind`, deliberately and with its own docstring saying why: "a five-step procedure that
    died at step four ran four real steps, and discarding them would lose the work." Choosing the
    narrow upsert on `state == "failed"` refused to write those, and `TemplateWorkflow` launches
    under `ALLOW_DUPLICATE_FAILED_ONLY`, so a re-run of a failed template is the ordinary case.
    Measured against the live database: a second failed run kept the *first* run's step results
    beside the second run's payload, actor and failing step — a row saying bob failed at step `a`
    while carrying results for steps `a` and `b` that his run never produced, read back by
    `find_past_jobs`, `operations.job_activity` and `evidence_pack.assemble` alike.
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
# Named for what it does rather than for who calls it: it was `_FAIL_UPSERT`, and reading
# "the failure statement" as "the statement every failure takes" is what put a template
# run's own steps behind it.
_KEEP_RESULT_UPSERT = _upsert(tuple(c for c in _MUTABLE if c not in _RESULT_COLUMNS))

_SELECT_ONE = f"SELECT {_COLUMNS}, completed_at FROM job_records WHERE job_id = %s"

# The listing projection: everything a chemist needs to recognise a run, and none of the result
# blob (see `JobRecordSummary`). Both filters are self-disabling — an empty term matches every row
# through the `%s = ''` arm — so "any connector, any text" needs no second statement, and the
# self-disabling arm costs nothing: `core/db.py` connects with `plan_cache_mode=force_custom_plan`,
# so the planner sees the bound value, folds `%s = ''` away and is free to use an index on the
# `ILIKE` that survives. Measured, that is exactly what it does.
#
# **ILIKE, still — but the sentence that used to follow it was a claim about a commit, and it was
# false.** It read: "this table holds one row per durable run (thousands, not millions) … a search
# index would be machinery to maintain for a scan the database does in milliseconds." At 200
# chemists doing ~5 durable jobs a day that is ~365k rows a year, and a leading wildcard is
# unindexable by a btree, so the scan the sentence called milliseconds is the **whole table** —
# measured at 500 000 rows, 1 036 ms and 19 920 buffers for a term that matches nothing, holding
# one of `pg_pool_max_size` connections for the duration, from a tool the *model* calls
# (`search_job_records` in `src/chemclaw/agent/durable_tools.py`). A term that matches is fast
# for a reason that
# hides this: the `completed_at` index lets the scan stop at the first page of hits, so every test,
# demo and eyeball sees 0.2 ms. A miss is what an agent searching for a phrase it invented
# produces.
#
# Migration `081` adds `gin_trgm_ops` indexes on the three searched columns, which is why the
# statement below is unchanged: trigrams accelerate this *same* predicate rather than replacing it,
# so the rows returned are identical (1 036 ms -> 1.09 ms on the miss, 950x). A `tsvector` — the
# other thing that removes the scan — would have changed what the tool matches, from the substring
# search its docstring promises to stems and boolean widening, and `core/fulltext.py` exists to
# keep *that* rule identical across the two hybrid indexes rather than to be a second answer here.
_SEARCH = """
    SELECT job_id, connector, job, rationale, summary, note_id, plan_step, state, completed_at
    FROM job_records
    WHERE (%s = '' OR connector = %s)
      AND (%s = '' OR rationale ILIKE %s OR summary ILIKE %s OR job ILIKE %s)
    ORDER BY completed_at DESC
    LIMIT %s
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.postgres_dsn)


class PostgresJobRecordSink:
    """Writes each finished job's record to `job_records`, one connection per record."""

    async def record(self, record: JobRecord) -> None:
        """Insert the record, refreshing what this particular record is entitled to refresh.

        A record carrying a result replaces the row entire; a failure that carries none sets how
        the run ended and leaves the result columns alone. See the module docstring for the
        measurement behind the split, and `_says_nothing_about_a_result` for why the test is the
        record rather than its state.
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
                    _json(record.payload),
                    record.summary,
                    _json(record.result),
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
    """The full record for one job, or None when the table has no row for it."""
    async with _connect() as conn:
        cursor = await conn.execute(_SELECT_ONE, (job_id,))
        row = await cursor.fetchone()
    if row is None:
        return None
    return JobRecord(
        job_id=row[0],
        connector=row[1],
        job=row[2],
        rationale=row[3],
        requested_by=row[4],
        session_id=row[5],
        correlation_id=row[6],
        plan_step=row[7],
        plan_hash=row[8],
        payload=row[9],
        summary=row[10],
        result=row[11],
        note_id=row[12],
        calc_refs=list(row[13] or []),
        runtime_seconds=row[14],
        payload_kind=row[15],
        state=row[16],
        failure_reason=row[17],
        completed_at=row[18],
    )


async def read_job_record_summaries(
    text: str, connector: str, limit: int
) -> list[JobRecordSummary]:
    """Past runs matching the (optional) text and connector filters, newest first."""
    pattern = f"%{text}%"
    async with _connect() as conn:
        cursor = await conn.execute(
            _SEARCH, (connector, connector, text, pattern, pattern, pattern, limit)
        )
        rows = await cursor.fetchall()
    return [
        JobRecordSummary(
            job_id=row[0],
            connector=row[1],
            job=row[2],
            rationale=row[3],
            summary=row[4],
            note_id=row[5],
            plan_step=row[6],
            state=row[7],
            completed_at=row[8],
        )
        for row in rows
    ]


def _json(value: dict[str, Any]) -> Jsonb:
    """Wrap a mapping for a `jsonb` column (psycopg needs the explicit adapter)."""
    return Jsonb(value)
