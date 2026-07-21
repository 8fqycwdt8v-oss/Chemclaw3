"""The job→session push-back channel (plan Phase F3-T2).

A finished background job (a Temporal workflow) cannot reach into the front-door process to update a
live conversation, and making the user poll is the very thing this closes. Instead the job appends a
row to `session_events` (the durable mailbox), and the front-door service *tails* the table: for
each unconsumed row it wakes the owning session and marks the row consumed. This module is that —
the writer (`record_session_event`), the reader (`fetch_unconsumed`/`mark_consumed`), and a tailer
(`stream_new_events`) whose polling is dependency-injected so its loop is unit-testable without a
database. The payload is opaque JSON; only durability of the *notification* lives here — the job's
own durability stays in Temporal (D-002).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from chemclaw import db
from chemclaw.config import settings

_INSERT = "INSERT INTO session_events (session_id, kind, payload) VALUES (%s, %s, %s)"
_SELECT_UNCONSUMED = (
    "SELECT id, session_id, kind, payload FROM session_events "
    "WHERE session_id = %s AND consumed_at IS NULL ORDER BY id"
)
_MARK_CONSUMED = "UPDATE session_events SET consumed_at = now() WHERE id = ANY(%s)"


class SessionEvent(BaseModel):
    """One push-back notification for a session (e.g. a completed job's result)."""

    session_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    event_id: int | None = None  # set when read back; absent when first recorded


def _dsn(dsn: str | None) -> str:
    """The session-store DSN (shared with the history store), overridable per call for tests."""
    return dsn or settings.session_store_dsn or settings.postgres_dsn


async def record_session_event(
    session_id: str, kind: str, payload: dict[str, Any] | None = None, *, dsn: str | None = None
) -> None:
    """Append a push-back event for `session_id` (called from the job side)."""
    async with await db.connect(
        _dsn(dsn), statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        await conn.execute(_INSERT, (session_id, kind, Jsonb(payload or {})))
        await conn.commit()


async def fetch_unconsumed(session_id: str, *, dsn: str | None = None) -> list[SessionEvent]:
    """Return a session's not-yet-consumed events in arrival order."""
    async with await db.connect(
        _dsn(dsn), statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        cursor = await conn.execute(_SELECT_UNCONSUMED, (session_id,))
        rows = await cursor.fetchall()
    return [
        SessionEvent(event_id=row[0], session_id=row[1], kind=row[2], payload=row[3] or {})
        for row in rows
    ]


async def mark_consumed(event_ids: Sequence[int], *, dsn: str | None = None) -> None:
    """Mark delivered events consumed so a restarted tailer neither replays nor drops them."""
    if not event_ids:
        return
    async with await db.connect(
        _dsn(dsn), statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        await conn.execute(_MARK_CONSUMED, (list(event_ids),))
        await conn.commit()


async def stream_new_events(
    session_id: str,
    *,
    poll_seconds: float | None = None,
    max_polls: int | None = None,
    fetch: Callable[[str], Awaitable[list[SessionEvent]]] | None = None,
    mark: Callable[[Sequence[int]], Awaitable[None]] | None = None,
) -> AsyncIterator[SessionEvent]:
    """Yield a session's push-back events as they arrive, marking each consumed once yielded.

    The service runs this as a per-session background task (unbounded, `max_polls=None`). `fetch`/
    `mark`/`poll_seconds` default to the Postgres channel + configured interval but are injectable,
    so the loop is unit-testable with fakes and no database. `max_polls` bounds the loop for tests.

    Args:
        session_id: The session to tail.
        poll_seconds: Sleep between polls; defaults to `session_event_poll_seconds`.
        max_polls: Stop after this many polls (None = run forever, the service default).
        fetch: Reads unconsumed events; defaults to `fetch_unconsumed`.
        mark: Marks events consumed; defaults to `mark_consumed`.

    Yields:
        Each `SessionEvent` in arrival order, once across restarts (consumed rows are skipped).
    """
    interval = poll_seconds if poll_seconds is not None else settings.session_event_poll_seconds
    do_fetch = fetch or (lambda sid: fetch_unconsumed(sid))
    do_mark = mark or (lambda ids: mark_consumed(ids))
    polls = 0
    while max_polls is None or polls < max_polls:
        events = await do_fetch(session_id)
        for event in events:
            yield event
        if events:
            await do_mark([e.event_id for e in events if e.event_id is not None])
        polls += 1
        if max_polls is None or polls < max_polls:
            await asyncio.sleep(interval)
