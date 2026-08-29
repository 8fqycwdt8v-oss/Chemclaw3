"""Postgres backing for the wait: `infra/sql/073_pending_requests.sql`.

The workflow in `durable/awaiting.py` is the authority on whether a wait is still open; this table
is its projection, written by that workflow's own activities and read by the front door and the
agent. Kept separate from the workflow module for the reason `audit_store` is kept separate from
`audit`: a workflow module is imported by every worker, and a worker that runs no wait should not
pull psycopg for a store it will not use.

**Every write here is an upsert keyed on `request_id`, and every state transition is guarded.**
An activity runs at-least-once, so `open` must be replayable, and `close` must not be able to
overwrite an answer with an expiry — a reminder that fires while a person is clicking would
otherwise decide the outcome by whichever transaction commits second. The guard is in the SQL
(`WHERE state = 'waiting'`), so it holds across processes rather than in whichever worker asks.
"""

import json
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings


class PendingRequest(BaseModel):
    """One open or settled wait, as a surface reads it."""

    request_id: str
    kind: str
    subject: str
    rationale: str = ""
    asked_of: str = ""
    requested_by: str = ""
    session_id: str = ""
    state: str = "waiting"
    due_at: str = ""
    reminders: int = 0
    answered_at: str = ""
    answered_by: str = ""
    answer: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


_OPEN = """
    INSERT INTO pending_requests
        (request_id, kind, subject, rationale, asked_of, requested_by, session_id,
         correlation_id, due_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (request_id) DO UPDATE SET
        kind = EXCLUDED.kind,
        subject = EXCLUDED.subject,
        rationale = EXCLUDED.rationale,
        asked_of = EXCLUDED.asked_of,
        due_at = EXCLUDED.due_at
    WHERE pending_requests.state = 'waiting'
"""

_SETTLE = """
    UPDATE pending_requests
    SET state = %s, answered_at = now(), answered_by = %s, answer = %s
    WHERE request_id = %s AND state = 'waiting'
"""

_REMIND = """
    UPDATE pending_requests
    SET reminders = reminders + 1, reminded_at = now()
    WHERE request_id = %s AND state = 'waiting'
"""

_COLUMNS = (
    "request_id, kind, subject, rationale, asked_of, requested_by, session_id, state, "
    "due_at, reminders, answered_at, answered_by, answer, created_at"
)


async def open_request(
    *,
    request_id: str,
    kind: str,
    subject: str,
    rationale: str,
    asked_of: str,
    requested_by: str,
    session_id: str,
    correlation_id: str,
    due_at: datetime,
) -> None:
    """Record a wait as open. Idempotent, and never reopens one that has settled."""
    async with _connect() as conn:
        await conn.execute(
            _OPEN,
            (
                request_id,
                kind,
                subject,
                rationale,
                asked_of,
                requested_by,
                session_id,
                correlation_id,
                due_at,
            ),
        )


async def settle_request(
    request_id: str, *, state: str, answered_by: str, answer: dict[str, Any]
) -> bool:
    """Move a waiting request to `answered`, `expired` or `cancelled`.

    Returns whether this call was the one that settled it. `False` means somebody else got there
    first, which is not an error: an expiry racing a person's click is the ordinary case, and the
    guard is what makes the first writer win rather than the last.
    """
    async with _connect() as conn:
        cursor = await conn.execute(_SETTLE, (state, answered_by, json.dumps(answer), request_id))
        return cursor.rowcount == 1


async def record_reminder(request_id: str) -> None:
    """Count one escalation against a still-open request."""
    async with _connect() as conn:
        await conn.execute(_REMIND, (request_id,))


def _row(values: tuple[Any, ...]) -> PendingRequest:
    """One database row as the model a surface reads."""
    return PendingRequest(
        request_id=str(values[0]),
        kind=str(values[1]),
        subject=str(values[2]),
        rationale=str(values[3]),
        asked_of=str(values[4]),
        requested_by=str(values[5]),
        session_id=str(values[6]),
        state=str(values[7]),
        due_at=values[8].isoformat() if values[8] else "",
        reminders=int(values[9]),
        answered_at=values[10].isoformat() if values[10] else "",
        answered_by=str(values[11]),
        answer=dict(values[12] or {}),
        created_at=values[13].isoformat() if values[13] else "",
    )


async def get_request(request_id: str) -> PendingRequest | None:
    """One request by id, whatever state it is in."""
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM pending_requests WHERE request_id = %s", (request_id,)
            )
            row = await cur.fetchone()
    return _row(tuple(row)) if row else None


async def open_requests(*, asked_of: str = "", limit: int = 50) -> list[PendingRequest]:
    """Everything still waiting, soonest deadline first.

    `asked_of` narrows to what is routed to one actor **or to nobody in particular**: an unrouted
    request is waiting on whoever is entitled, so hiding it from a named query would make the
    common case invisible. Routing is advisory either way — the answer route is the control.
    """
    sql = f"SELECT {_COLUMNS} FROM pending_requests WHERE state = 'waiting'"
    params: list[Any] = []
    if asked_of:
        sql += " AND (asked_of = %s OR asked_of = '')"
        params.append(asked_of)
    sql += " ORDER BY due_at LIMIT %s"
    params.append(max(1, min(limit, 200)))
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            return [_row(tuple(row)) for row in await cur.fetchall()]
