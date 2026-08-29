"""Postgres backing for the wait: `infra/sql/076_pending_requests.sql`.

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
from collections.abc import Sequence
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


# **Three cases, and telling them apart is the whole point of `run_id`.** A retry of the opening
# activity carries the *same* Temporal run and must update in place without disturbing a state the
# workflow may already have settled. A re-ask after a **lapsed** deadline carries a different run —
# `request_id_for` is deterministic and `ALLOW_DUPLICATE` is set precisely so a lapsed question can
# be asked again — and must reopen the row, so the new wait is visible and answerable.
#
# The third case is the one the first version of this fix got wrong. Guarding on
# `run_id <> EXCLUDED.run_id` alone admitted an **answered** row, and the reopen NULLs
# `answered_at`/`answered_by`/`answer` — so re-asking a question somebody had already answered
# destroyed their attribution and their payload. This table is in `retention._NOT_PRUNED`, justified
# there as "the attribution for an answer that released a durable workflow", and the answer route
# writes no audit event: the row is the only record there is. A row that can never be deleted must
# not be silently overwritten either.
#
# So a reopen is scoped to the terminal states in which **nobody answered**. A genuinely new ask of
# an already-answered question differs in its `subject`, which is what `request_id_for` keys on, and
# therefore gets its own row rather than overwriting somebody's answer.
#
# Guarding on `state = 'waiting'` alone — the version before either fix — did the retry case and
# silently dropped the re-ask: the row kept the old cycle's `expired` state and deadline, so the new
# wait appeared in no inbox and the answer route refused it with 409 forever while the workflow ran.
_OPEN = """
    INSERT INTO pending_requests
        (request_id, kind, subject, rationale, asked_of, requested_by, session_id,
         correlation_id, due_at, run_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (request_id) DO UPDATE SET
        kind = EXCLUDED.kind,
        subject = EXCLUDED.subject,
        rationale = EXCLUDED.rationale,
        asked_of = EXCLUDED.asked_of,
        due_at = EXCLUDED.due_at,
        run_id = EXCLUDED.run_id,
        -- **Refreshed, because they legitimately differ between cycles and a gate reads one of
        -- them.** `request_id_for` keys on (kind, subject, asked_of) and deliberately *not* on the
        -- requester, so a re-ask is routinely a different person in a different session. Leaving
        -- these stale meant `_may_answer`'s separation-of-duties check read the *previous* cycle's
        -- requester: bob re-launches alice's irreversible job, the approval row reopens still
        -- naming alice, and bob passes a check whose entire purpose is to refuse him.
        requested_by = EXCLUDED.requested_by,
        session_id = EXCLUDED.session_id,
        correlation_id = EXCLUDED.correlation_id,
        state = 'waiting',
        answered_at = NULL,
        answered_by = '',
        answer = '{}'::jsonb,
        reminders = CASE
            WHEN pending_requests.run_id = EXCLUDED.run_id THEN pending_requests.reminders ELSE 0
        END,
        reminded_at = CASE
            WHEN pending_requests.run_id = EXCLUDED.run_id THEN pending_requests.reminded_at
            ELSE NULL
        END
    WHERE pending_requests.state = 'waiting'
       OR (
            pending_requests.run_id <> EXCLUDED.run_id
            AND pending_requests.state IN ('expired', 'cancelled')
          )
"""

# `answered_at` only where somebody answered. It was stamped unconditionally, so an `expired` or
# `cancelled` row carried a timestamp with an empty `answered_by` — a column saying "somebody
# answered at some point" about a question nobody answered, surfaced to the agent and the front door
# that way. `076`'s `pending_requests_answer_is_attributed` constraint exists to stop exactly that
# claim and only fires on `state = 'answered'`; this walked around it from the other side.
_SETTLE = """
    UPDATE pending_requests
    SET state = %s,
        answered_at = CASE WHEN %s = 'answered' THEN now() ELSE NULL END,
        answered_by = %s,
        answer = %s
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
    run_id: str = "",
) -> None:
    """Record a wait as open.

    Idempotent within one Temporal run, and **reopening across runs** — see `_OPEN` for why those
    are different cases and what it cost to treat them as one. `run_id` defaults to empty so a
    caller with no run to name (a test, a backfill) keeps the old within-run behaviour.
    """
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
                run_id,
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
        cursor = await conn.execute(
            _SETTLE, (state, state, answered_by, json.dumps(answer), request_id)
        )
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


async def open_requests(
    *, asked_of: str = "", identities: Sequence[str] = (), limit: int = 50
) -> list[PendingRequest]:
    """Everything still waiting, soonest deadline first.

    `asked_of` narrows to what is routed to one actor **or to nobody in particular**: an unrouted
    request is waiting on whoever is entitled, so hiding it from a named query would make the
    common case invisible. Routing is advisory either way — the answer route is the control.

    `identities` is the rest of the caller's routing surface — their user principal name and the
    entitlements they hold — because **routing to a team is the case this was built for and was the
    one it could not answer.** `request_external_input` documents `asked_of` as "an actor id or a
    team entitlement", and `_may_answer` honours both, but this read matched the object id alone: a
    request routed to `qc-team` was answerable by the QC team and appeared in **nobody's** inbox, so
    it sat invisible until it expired. Passing only `asked_of` keeps the old behaviour for callers
    that have no role set to offer.
    """
    sql = f"SELECT {_COLUMNS} FROM pending_requests WHERE state = 'waiting'"
    params: list[Any] = []
    routes = [route for route in (asked_of, *identities) if route]
    if routes:
        sql += " AND (asked_of = ANY(%s) OR asked_of = '')"
        params.append(routes)
    sql += " ORDER BY due_at LIMIT %s"
    params.append(max(1, min(limit, 200)))
    async with _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            return [_row(tuple(row)) for row in await cur.fetchall()]
