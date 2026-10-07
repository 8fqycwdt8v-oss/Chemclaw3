"""Postgres backing for the wait: `infra/sql/076_pending_requests.sql`.

The workflow in `durable/awaiting.py` is the authority on whether a wait is open; this table is
its projection, written by its activities and read by the front door and the agent. Separate so
workers that run no wait never import psycopg.

Every write is an upsert keyed on `request_id` (activities are at-least-once), and every state
transition is guarded in SQL (`WHERE state = 'waiting'`), so an expiry cannot overwrite an
answer across processes.
"""

import json
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow, class_row
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.db import IsoStamp


class PendingRequest(BaseModel):
    """One open or settled wait, as a surface reads it.

    Read back by `class_row`, so every field name here is a column name in `_COLUMNS` and in that
    order: the two are one declaration rather than two that agreed by inspection.
    """

    #: `extra="forbid"` so a SELECT column with no matching field is an error rather than silently
    #: ignored.
    model_config = ConfigDict(extra="forbid")

    request_id: str
    kind: str
    subject: str
    rationale: str = ""
    asked_of: str = ""
    requested_by: str = ""
    session_id: str = ""
    state: str = "waiting"
    due_at: IsoStamp = ""
    reminders: int = 0
    answered_at: IsoStamp = ""
    answered_by: str = ""
    answer: dict[str, Any] = Field(default_factory=dict)
    created_at: IsoStamp = ""
    #: The knowledge notes the question rests on, so the answer route can check they still hold
    #: (`kg/premise.py`). Read from the row: the premise validated at ask time is the one that
    #: counts.
    premise_note_ids: list[str] = Field(default_factory=list)


#: The most rows one `open_requests` call serves, however much is asked for; a structural bound,
#: not a deployment setting. Reported as `limit_applied` rather than applied silently.
_MAX_PAGE = 200


class OpenRequests(BaseModel):
    """One page of the inbox, **and how much of the inbox it is**.

    The bare `list[PendingRequest]` this replaced was the silence this table exists to prevent, one
    level up. Measured against a real database: 35 rows waiting, a page of 20 returned, and nothing
    in the value, in a log or in a counter said the other 15 were there. Both readers then said
    something stronger than they knew — `check_pending_requests` documents itself as "everything
    still waiting", and `GET /pending` is the inbox a raised question has to appear in or it ages
    out unanswered.

    `total_waiting` is counted over the *same* predicate in the *same* transaction as the page, so
    "20 of 35" is one consistent statement rather than two reads of a moving table.
    """

    requests: list[PendingRequest] = Field(default_factory=list)
    # Everything matching, before the page bound — the population this page is a page of.
    total_waiting: int = Field(default=0, ge=0)
    # The bound actually used, which is not the bound asked for once `_MAX_PAGE` bites.
    limit_applied: int = Field(default=_MAX_PAGE, ge=1)

    @property
    def truncated(self) -> bool:
        """Whether waiting requests exist that this page does not carry."""
        return self.total_waiting > len(self.requests)


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


# Three cases, distinguished by `run_id`. A retry of the opening activity (same run) updates in
# place without disturbing a state the workflow may already have settled. A re-ask under a new run
# (after a lapsed deadline; `ALLOW_DUPLICATE`) reopens the row so the new wait is visible and
# answerable. An answered row's answer is first moved to the archive by `_ARCHIVE_ANSWER`, so a
# reopen never destroys attribution.
_OPEN = """
    INSERT INTO pending_requests
        (request_id, kind, subject, rationale, asked_of, requested_by, session_id,
         correlation_id, premise_note_ids, due_at, run_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        -- Refreshed for the same reason, and with a sharper edge: a re-ask is a *new* question that
        -- was validated against today's corpus, so keeping the previous cycle's premise would check
        -- an answer against notes this question never rested on — and, where the old cycle cited a
        -- note that has since been retired, would refuse every answer to a question whose own
        -- premise is whole.
        premise_note_ids = EXCLUDED.premise_note_ids,
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
            AND pending_requests.state IN ('expired', 'cancelled', 'answered')
          )
"""

# Archive the answer before a reopen, in the same transaction, so attribution is either moved
# aside or the reopen does not happen. `run_id <> %s` keeps a retry by the owning run from
# archiving its own answer. `ON CONFLICT DO NOTHING`: the archive is keyed on the answering run,
# and the application holds no UPDATE on that table.
_ARCHIVE_ANSWER = """
    INSERT INTO pending_request_answers
        (request_id, run_id, kind, subject, asked_of, requested_by, session_id,
         answered_at, answered_by, answer)
    SELECT request_id, run_id, kind, subject, asked_of, requested_by, session_id,
           answered_at, answered_by, answer
      FROM pending_requests
     WHERE request_id = %s
       AND state = 'answered'
       AND answered_at IS NOT NULL
       AND run_id <> %s
    ON CONFLICT (request_id, run_id) DO NOTHING
"""

# With answers archived, every terminal state is reopenable by a different run, so `open_request`
# returns no verdict. `tests/test_pending_store.py` asserts what each case does to the row and the
# archive.

# `answered_at` only where somebody answered, so an `expired` or `cancelled` row never claims an
# answer time.
_SETTLE = """
    UPDATE pending_requests
    SET state = %s,
        answered_at = CASE WHEN %s = 'answered' THEN now() ELSE NULL END,
        answered_by = %s,
        answer = %s
    WHERE request_id = %s AND state = 'waiting'
"""

# A running total, not an increment, because an activity is at-least-once: `GREATEST` makes a
# redelivered attempt a no-op and keeps this equal to the workflow's own replay-stable count.
_REMIND = """
    UPDATE pending_requests
    SET reminders = GREATEST(reminders, %s), reminded_at = now()
    WHERE request_id = %s AND state = 'waiting'
"""

_COLUMNS = (
    "request_id, kind, subject, rationale, asked_of, requested_by, session_id, state, "
    "due_at, reminders, answered_at, answered_by, answer, created_at, premise_note_ids"
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
    premise_note_ids: list[str] | None = None,
    run_id: str = "",
) -> None:
    """Record a wait as open — archiving the previous cycle's answer when there is one.

    Idempotent within one Temporal run, reopening across runs (see `_OPEN`). `run_id` defaults to
    empty for callers with no run to name. `_ARCHIVE_ANSWER` and `_OPEN` run in one transaction, in
    that order, so an answer is never blanked; the archive is a no-op in every other case.
    """
    async with _connect() as conn:
        await conn.execute(_ARCHIVE_ANSWER, (request_id, run_id))
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
                list(premise_note_ids or []),
                due_at,
                run_id,
            ),
        )


async def settle_request(
    request_id: str, *, state: str, answered_by: str, answer: dict[str, Any]
) -> bool:
    """Move a waiting request to `answered`, `expired` or `cancelled`.

    Returns whether this call settled it. `False` means somebody else got there first (an expiry
    racing a click), which is not an error: the first writer wins.
    """
    async with _connect() as conn:
        cursor = await conn.execute(
            _SETTLE, (state, state, answered_by, json.dumps(answer), request_id)
        )
        return cursor.rowcount == 1


# Guarded on the run as well as the state, so a sweep that examined an old run never settles a
# question reopened under a new one.
_SETTLE_ORPHAN = """
    UPDATE pending_requests
    SET state = 'cancelled', answered_at = NULL, answered_by = '', answer = %s
    WHERE request_id = %s AND run_id = %s AND state = 'waiting'
"""

#: One keyset page of waiting rows past the grace window, oldest first. A cursor so live waits at
#: the front cannot hide an orphan behind them; `request_id` breaks `created_at` ties so the order
#: is total.
_ORPHAN_CANDIDATES = """
    SELECT request_id, run_id, created_at FROM pending_requests
    WHERE state = 'waiting' AND created_at < now() - make_interval(secs => %s)
      AND (%s::timestamptz IS NULL OR (created_at, request_id) > (%s::timestamptz, %s))
    ORDER BY created_at, request_id
    LIMIT %s
"""


class WaitingRow(BaseModel):
    """One candidate for the orphan sweep, and the keyset position after it."""

    request_id: str
    run_id: str = ""
    created_at: datetime


async def waiting_rows(
    *, older_than_seconds: float, limit: int, after: WaitingRow | None = None
) -> list[WaitingRow]:
    """One page of waiting rows past the grace window, continuing after `after`.

    See `_ORPHAN_CANDIDATES` for why the sweep pages.
    """
    at = after.created_at if after else None
    key = after.request_id if after else ""
    async with _connect() as conn:
        cursor = await conn.execute(_ORPHAN_CANDIDATES, (older_than_seconds, at, at, key, limit))
        return [
            WaitingRow(request_id=str(row[0]), run_id=str(row[1] or ""), created_at=row[2])
            for row in await cursor.fetchall()
        ]


async def settle_orphan(request_id: str, run_id: str, reason: str) -> bool:
    """Cancel a waiting row whose run is gone, if that run still owns it (`_SETTLE_ORPHAN`)."""
    async with _connect() as conn:
        cursor = await conn.execute(
            _SETTLE_ORPHAN, (json.dumps({"reason": reason}), request_id, run_id)
        )
        return cursor.rowcount == 1


async def record_reminder(request_id: str, count: int) -> None:
    """Record how many escalations a still-open request has had — see `_REMIND` for why a total.

    Args:
        request_id: The wait being chased.
        count: The asking workflow's own running escalation count, which is replay-stable.
    """
    async with _connect() as conn:
        await conn.execute(_REMIND, (count, request_id))


async def get_request(request_id: str) -> PendingRequest | None:
    """One request by id, whatever state it is in.

    Raises:
        pydantic.ValidationError: `_COLUMNS` and `PendingRequest` no longer describe the same row.
    """
    async with _connect() as conn:
        async with conn.cursor(row_factory=class_row(PendingRequest)) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM pending_requests WHERE request_id = %s", (request_id,)
            )
            return await cur.fetchone()


async def open_requests(
    *, asked_of: str = "", identities: Sequence[str] = (), limit: int = 50
) -> OpenRequests:
    """One page of what is still waiting, soonest deadline first, **and how many there are**.

    `asked_of` narrows to what is routed to one actor or to nobody in particular (an unrouted
    request waits on whoever is entitled). `identities` adds the caller's other routing names (UPN
    and entitlements), so a request routed to a team appears in that team's inboxes. Routing is
    advisory; the answer route is the control. The count is read in the same transaction as the
    page, so the two cannot disagree.
    """
    where = "WHERE state = 'waiting'"
    params: list[Any] = []
    routes = [route for route in (asked_of, *identities) if route]
    if routes:
        where += " AND (asked_of = ANY(%s) OR asked_of = '')"
        params.append(routes)
    page = max(1, min(limit, _MAX_PAGE))
    async with _connect() as conn:
        # Two cursors on one connection, still one transaction; the row factory is per cursor.
        async with conn.cursor(row_factory=class_row(PendingRequest)) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM pending_requests {where} ORDER BY due_at LIMIT %s",
                (*params, page),
            )
            rows = await cur.fetchall()
        async with conn.cursor() as counter:
            await counter.execute(f"SELECT count(*) FROM pending_requests {where}", tuple(params))
            counted = await counter.fetchone()
    return OpenRequests(
        requests=rows,
        total_waiting=int(counted[0]) if counted else len(rows),
        limit_applied=page,
    )
