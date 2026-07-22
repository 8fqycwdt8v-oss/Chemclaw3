"""The job→session push-back channel (plan Phase F3-T2).

A finished background job (a Temporal workflow) cannot reach into the front-door process to update a
live conversation, and making the user poll is the very thing this closes. Instead the job appends a
row to `session_events` (the durable mailbox), and the front-door service *tails* the table: it
*claims* each unconsumed row and wakes the owning session. This module is that — the writer
(`record_session_event`), the atomic claim (`claim_unconsumed`), and a tailer (`stream_new_events`)
whose polling is dependency-injected so its loop is unit-testable without a database. The payload is
opaque JSON; only durability of the *notification* lives here — the job's own durability stays in
Temporal (D-002).

The claim is a single `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING …`
statement (COR-4): marking a row consumed and reading it back are one atomic step, so two tailers
racing on the same session can never both deliver a row — the second's `SKIP LOCKED` select simply
skips the rows the first already claimed. The tradeoff is at-most-once on a crash in the tiny window
between claim-commit and the event reaching the client (versus the old at-least-once, which paid for
that with the concurrent double-delivery this fixes); for a "wake the chat" notification whose
durable result already lives in the graph/session, that is the right side of the trade.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from chemclaw import db
from chemclaw.config import settings

_INSERT = "INSERT INTO session_events (session_id, kind, payload) VALUES (%s, %s, %s)"
# Atomically claim (mark consumed) and read back a session's unconsumed events in one statement.
# The inner SELECT locks the rows with SKIP LOCKED, so a concurrent tailer skips already-claimed
# rows instead of re-reading them (COR-4). RETURNING order is unspecified, so the caller re-sorts
# by id to preserve arrival order.
_CLAIM = (
    "UPDATE session_events SET consumed_at = now() WHERE id IN ("
    "SELECT id FROM session_events WHERE session_id = %s AND consumed_at IS NULL "
    "ORDER BY id FOR UPDATE SKIP LOCKED"
    ") RETURNING id, session_id, kind, payload"
)


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


async def claim_unconsumed(session_id: str, *, dsn: str | None = None) -> list[SessionEvent]:
    """Atomically claim (mark consumed) and return a session's unconsumed events in arrival order.

    One `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING` statement, so a concurrent tailer cannot claim
    the same rows (COR-4). Rows are re-sorted by id since RETURNING order is unspecified.
    """
    async with await db.connect(
        _dsn(dsn), statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        cursor = await conn.execute(_CLAIM, (session_id,))
        rows = await cursor.fetchall()
        await conn.commit()
    return [
        SessionEvent(event_id=row[0], session_id=row[1], kind=row[2], payload=row[3] or {})
        for row in sorted(rows, key=lambda r: r[0])
    ]


async def stream_new_events(
    session_id: str,
    *,
    poll_seconds: float | None = None,
    max_polls: int | None = None,
    claim: Callable[[str], Awaitable[list[SessionEvent]]] | None = None,
) -> AsyncIterator[SessionEvent]:
    """Yield a session's push-back events as they arrive, each already claimed atomically.

    The service runs this as a per-session background task (unbounded, `max_polls=None`). `claim`/
    `poll_seconds` default to the Postgres channel + configured interval but are injectable, so the
    loop is unit-testable with fakes and no database. `max_polls` bounds the loop for tests.

    Args:
        session_id: The session to tail.
        poll_seconds: Sleep between polls; defaults to `session_event_poll_seconds`.
        max_polls: Stop after this many polls (None = run forever, the service default).
        claim: Atomically claims and returns unconsumed events; defaults to `claim_unconsumed`.

    Yields:
        Each `SessionEvent` in arrival order, at most once across tailers (a claimed row is never
        re-delivered — the atomic claim is the concurrency guard, COR-4).
    """
    interval = poll_seconds if poll_seconds is not None else settings.session_event_poll_seconds
    do_claim = claim or (lambda sid: claim_unconsumed(sid))
    polls = 0
    while max_polls is None or polls < max_polls:
        for event in await do_claim(session_id):
            yield event
        polls += 1
        if max_polls is None or polls < max_polls:
            await asyncio.sleep(interval)
