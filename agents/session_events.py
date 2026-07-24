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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from functools import partial
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from chemclaw import db
from chemclaw.config import settings

# The insert is idempotent when the writer supplies a `dedupe_key`: the recording activity runs
# at-least-once, so a retry after a committed-but-unacked insert would otherwise duplicate the
# notification. The partial unique index on `dedupe_key` turns that retry into a no-op; a NULL key
# (writers with no retry semantics) keeps the plain append.
_INSERT = (
    "INSERT INTO session_events (session_id, kind, payload, dedupe_key) VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING"
)
# Atomically claim (mark consumed) and read back a session's unconsumed events in one statement.
# The inner SELECT locks the rows with SKIP LOCKED, so a concurrent tailer skips already-claimed
# rows instead of re-reading them (COR-4). RETURNING order is unspecified, so the caller re-sorts
# by id to preserve arrival order. The claim is *destructive* (at-most-once), so a consumer that
# only wants certain kinds must filter in the claim itself — claiming everything and dropping the
# rest client-side would silently destroy other consumers' events; the `_CLAIM_KINDS` variant
# scopes the claim so unmatched kinds stay unconsumed for whoever they are meant for.
_CLAIM = (
    "UPDATE session_events SET consumed_at = now() WHERE id IN ("
    "SELECT id FROM session_events WHERE session_id = %s AND consumed_at IS NULL "
    "ORDER BY id FOR UPDATE SKIP LOCKED"
    ") RETURNING id, session_id, kind, payload"
)
_CLAIM_KINDS = (
    "UPDATE session_events SET consumed_at = now() WHERE id IN ("
    "SELECT id FROM session_events WHERE session_id = %s AND consumed_at IS NULL "
    "AND kind = ANY(%s) ORDER BY id FOR UPDATE SKIP LOCKED"
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
    session_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    dsn: str | None = None,
) -> None:
    """Append a push-back event for `session_id` (called from the job side).

    `dedupe_key` is the writer's deterministic identity for this logical event: the Temporal
    activity that records it is retried at-least-once, so a retry after a committed-but-unacked
    insert would deliver the same notification twice. With a key set, the second insert lands on
    the unique index and becomes a no-op; None (non-retrying writers) appends unconditionally.
    """
    async with await db.connect(
        _dsn(dsn), statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        await conn.execute(_INSERT, (session_id, kind, Jsonb(payload or {}), dedupe_key))
        await conn.commit()


async def _claim_on(
    conn: AsyncConnection[TupleRow], session_id: str, kinds: Sequence[str] | None
) -> list[SessionEvent]:
    """Run the atomic claim on an existing connection (shared by one-shot claim and the tailer).

    With `kinds` set, only rows of those kinds are claimed — the claim is destructive, so the
    filter must live in the SQL: rows of other kinds stay unconsumed for their own consumer instead
    of being marked consumed and dropped.
    """
    if kinds is None:
        cursor = await conn.execute(_CLAIM, (session_id,))
    else:
        cursor = await conn.execute(_CLAIM_KINDS, (session_id, list(kinds)))
    rows = await cursor.fetchall()
    await conn.commit()
    return [
        SessionEvent(event_id=row[0], session_id=row[1], kind=row[2], payload=row[3] or {})
        for row in sorted(rows, key=lambda r: r[0])
    ]


async def claim_unconsumed(
    session_id: str, *, kinds: Sequence[str] | None = None, dsn: str | None = None
) -> list[SessionEvent]:
    """Atomically claim (mark consumed) and return a session's unconsumed events in arrival order.

    One `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING` statement, so a concurrent tailer cannot claim
    the same rows (COR-4). Rows are re-sorted by id since RETURNING order is unspecified. `kinds`
    scopes the claim to those event kinds (None claims everything): the claim is at-most-once, so a
    kind-selective consumer must filter here, never after the claim.
    """
    async with await db.connect(
        _dsn(dsn), statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        return await _claim_on(conn, session_id, kinds)


async def stream_new_events(
    session_id: str,
    *,
    poll_seconds: float | None = None,
    max_polls: int | None = None,
    claim: Callable[[str], Awaitable[list[SessionEvent]]] | None = None,
    kinds: Sequence[str] | None = None,
) -> AsyncIterator[SessionEvent]:
    """Yield a session's push-back events as they arrive, each already claimed atomically.

    The service runs this as a per-session background task (unbounded, `max_polls=None`). `claim`/
    `poll_seconds` default to the Postgres channel + configured interval but are injectable, so the
    loop is unit-testable with fakes and no database. `max_polls` bounds the loop for tests.

    The default (database) path opens **one** connection for the stream's whole lifetime: the loop
    polls every couple of seconds forever, so connect-per-poll would churn a fresh Postgres
    connection per stream per interval — multiplied by concurrent streams, a real exhaustion risk
    for the shared session-store database. A connection failure ends the stream (the client
    reconnects), exactly as it would have failed a per-poll connect.

    Args:
        session_id: The session to tail.
        poll_seconds: Sleep between polls; defaults to `session_event_poll_seconds`.
        max_polls: Stop after this many polls (None = run forever, the service default).
        claim: Atomically claims and returns unconsumed events; defaults to the Postgres claim.
            An injected claim owns its own kind-filtering — `kinds` applies to the default only.
        kinds: Claim only these event kinds (None = all). The claim is destructive (at-most-once),
            so a kind-selective consumer must scope the claim itself: other kinds then stay
            unconsumed for their own consumer instead of being silently destroyed.

    Yields:
        Each `SessionEvent` in arrival order, at most once across tailers (a claimed row is never
        re-delivered — the atomic claim is the concurrency guard, COR-4).
    """
    interval = poll_seconds if poll_seconds is not None else settings.session_event_poll_seconds
    async with AsyncExitStack() as stack:
        if claim is not None:
            do_claim: Callable[[], Awaitable[list[SessionEvent]]] = partial(claim, session_id)
        else:
            conn = await stack.enter_async_context(
                await db.connect(
                    _dsn(None), statement_timeout_seconds=settings.pg_statement_timeout_seconds
                )
            )
            do_claim = partial(_claim_on, conn, session_id, kinds)
        polls = 0
        while max_polls is None or polls < max_polls:
            for event in await do_claim():
                yield event
            polls += 1
            if max_polls is None or polls < max_polls:
                await asyncio.sleep(interval)
