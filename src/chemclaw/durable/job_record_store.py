"""Postgres backing for the durable job record (`infra/sql/023_job_records.sql`, D-157).

Kept separate from `chemclaw.durable.job_record` for the reason `chemclaw.agent.audit_store` is
kept separate from `chemclaw.agent.audit`: the module the workflow and the agent tools import
carries no database dependency, so a deployment (or a test, or a connector worker) that runs
without Postgres never pulls psycopg for a store it will not use.

Writes are an **upsert on `job_id`**, not an append. The id is the deterministic idempotency key
(`connectors/jobs.py::job_workflow_id`), a Temporal activity is at-least-once, and a re-run of a
failed job legitimately produces a second, better result for the same id — so "the record of this
run" must have exactly one row in all three cases.
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
    "payload, summary, result, note_id, calc_refs, runtime_seconds"
)

# Every mutable column is refreshed, **including the attribution**. Updating the reason and the
# result while keeping the first run's `requested_by` was a row that contradicted itself: a second
# execution under this id is a different person asking a differently-worded question (the id is
# reused when Temporal has expired the first execution, which is exactly the horizon this table
# exists for), and half-updating left run 2's reason beside run 1's name — the worst possible
# answer for the field an audit joins on. The row describes the latest run, whole.
_UPSERT = f"""
    INSERT INTO job_records ({_COLUMNS})
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (job_id) DO UPDATE SET
        rationale = EXCLUDED.rationale,
        requested_by = EXCLUDED.requested_by,
        session_id = EXCLUDED.session_id,
        correlation_id = EXCLUDED.correlation_id,
        payload = EXCLUDED.payload,
        summary = EXCLUDED.summary,
        result = EXCLUDED.result,
        note_id = EXCLUDED.note_id,
        calc_refs = EXCLUDED.calc_refs,
        runtime_seconds = EXCLUDED.runtime_seconds,
        completed_at = now()
"""

_SELECT_ONE = f"SELECT {_COLUMNS}, completed_at FROM job_records WHERE job_id = %s"

# The listing projection: everything a chemist needs to recognise a run, and none of the result
# blob (see `JobRecordSummary`). Both filters are self-disabling — an empty term matches every row
# through the `%s = ''` arm — so "any connector, any text" needs no second statement. ILIKE rather
# than a `tsvector`: this table holds one row per durable run (thousands, not millions), the
# reason is a sentence rather than a document, and a search index would be machinery to maintain
# for a scan the database does in milliseconds.
_SEARCH = """
    SELECT job_id, connector, job, rationale, summary, note_id, completed_at
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
        """Insert the record, replacing any existing row for the same job id."""
        async with _connect() as conn:
            await conn.execute(
                _UPSERT,
                (
                    record.job_id,
                    record.connector,
                    record.job,
                    record.rationale,
                    record.requested_by,
                    record.session_id,
                    record.correlation_id,
                    # psycopg adapts a mapping to `jsonb` only through its `Jsonb` wrapper — a bare
                    # dict is rejected by the adapter, not silently stringified.
                    _json(record.payload),
                    record.summary,
                    _json(record.result),
                    record.note_id,
                    record.calc_refs,
                    record.runtime_seconds,
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
        payload=row[7],
        summary=row[8],
        result=row[9],
        note_id=row[10],
        calc_refs=list(row[11] or []),
        runtime_seconds=row[12],
        completed_at=row[13],
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
            completed_at=row[6],
        )
        for row in rows
    ]


def _json(value: dict[str, Any]) -> Jsonb:
    """Wrap a mapping for a `jsonb` column (psycopg needs the explicit adapter)."""
    return Jsonb(value)
