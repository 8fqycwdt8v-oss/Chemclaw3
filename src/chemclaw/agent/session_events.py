"""The job→session push-back channel.

A finished background job cannot reach into the front-door process, so it appends a row to
`session_events` (a durable mailbox) and the front door tails the table, claiming each unconsumed
row and waking the owning session. This module holds the writer (`record_session_event`), the atomic
claim (`claim_unconsumed`) and a tailer (`stream_new_events`) whose polling is injectable for unit
tests. Only the notification's durability lives here; the job's own stays in Temporal.

The claim is one `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING …` statement, so
two tailers can never both deliver a row. Delivery is at-most-once; the tailer restores a row whose
yield never completed (`restore_unconsumed`), narrowing the loss window to the transport, and the
model also reads the mailbox at turn start (`api/runner._with_pushed_job_results`).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from functools import partial
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# Idempotent when the writer supplies a `dedupe_key`: the recording activity runs at-least-once, and
# the partial unique index turns a retried insert into a no-op. A NULL key appends unconditionally.
_INSERT = (
    "INSERT INTO session_events (session_id, kind, payload, dedupe_key) VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING"
)
# Atomically claim and read back a session's unconsumed events. SKIP LOCKED makes a concurrent
# tailer skip claimed rows; the caller re-sorts by id since RETURNING order is unspecified. The
# claim is destructive, so a kind-selective consumer must filter in the claim (`_CLAIM_KINDS`),
# leaving other kinds for their own consumer.
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
# Put a claimed-but-undelivered row back, so a dropped stream does not lose the notification.
_RESTORE = "UPDATE session_events SET consumed_at = NULL WHERE id = %s"


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

    `dedupe_key` is the writer's deterministic identity for this event; with it set, a retried
    insert is a no-op. `None` (non-retrying writers) appends unconditionally.
    """
    async with db.connection(_dsn(dsn)) as conn:
        await conn.execute(_INSERT, (session_id, kind, Jsonb(payload or {}), dedupe_key))
        await conn.commit()


async def claim_unconsumed(
    session_id: str, *, kinds: Sequence[str] | None = None, dsn: str | None = None
) -> list[SessionEvent]:
    """Atomically claim (mark consumed) and return a session's unconsumed events in arrival order.

    `kinds` scopes the claim to those event kinds (None claims everything); since the claim is
    at-most-once, a kind-selective consumer must filter here, never after. Opens its own connection
    per call, so the tailer holds none between polls.
    """
    async with db.connection(_dsn(dsn)) as conn:
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


async def restore_unconsumed(event_id: int, *, dsn: str | None = None) -> None:
    """Un-claim one event, so the next poll — this tailer's or another's — delivers it again.

    Used only for a row whose delivery did not complete. At worst it turns at-most-once into
    at-least-once for that row, and a duplicated "job finished" card is cheaper than a lost one.
    """
    try:
        async with db.connection(_dsn(dsn)) as conn:
            await conn.execute(_RESTORE, (event_id,))
            await conn.commit()
    except Exception:
        # Never raises: it runs as an unawaited teardown task, and a failed restore only returns
        # this row to at-most-once delivery.
        logger.warning("could not restore undelivered session event %d", event_id, exc_info=True)


async def stream_new_events(
    session_id: str,
    *,
    poll_seconds: float | None = None,
    max_polls: int | None = None,
    claim: Callable[[str], Awaitable[list[SessionEvent]]] | None = None,
    kinds: Sequence[str] | None = None,
    collapse: Callable[[list[SessionEvent]], list[SessionEvent]] | None = None,
) -> AsyncIterator[SessionEvent]:
    """Yield a session's push-back events as they arrive, each already claimed atomically.

    The service runs this as a per-session background task. The default path borrows a pooled
    connection per poll rather than holding one per open stream, which would exhaust the pool; a
    connection failure ends the stream and the client reconnects.

    Args:
        session_id: The session to tail.
        poll_seconds: Sleep between polls; defaults to `session_event_poll_seconds`.
        max_polls: Stop after this many polls (None = run forever, the service default).
        claim: Atomically claims and returns unconsumed events; defaults to the Postgres claim. An
        injected claim owns its own kind-filtering — `kinds` applies to the default only.
        kinds: Claim only these event kinds (None = all), so other kinds stay unconsumed for their
        own consumer.
        collapse: Fold one claim's rows before any is yielded, for a consumer to whom several rows
        of a batch are one fact; only this function sees the batch, so a caller folding as it goes
        could keep only the first row. Dropped rows are consumed and not restored. `None` delivers
        the claim unchanged.

    Yields:
        Each surviving `SessionEvent` in arrival order, at most once across tailers.
    """
    interval = poll_seconds if poll_seconds is not None else settings.session_event_poll_seconds
    do_claim: Callable[[], Awaitable[list[SessionEvent]]] = (
        partial(claim, session_id)
        if claim is not None
        else partial(claim_unconsumed, session_id, kinds=kinds)
    )
    polls = 0
    while max_polls is None or polls < max_polls:
        batch = await do_claim()
        for event in collapse(batch) if collapse is not None else batch:
            delivered = False
            try:
                yield event
                delivered = True
            finally:
                if not delivered and event.event_id is not None:
                    # The consumer went away between the claim and the yield completing. Restore on
                    # a separate task, because an `await` in this teardown `finally` re-raises the
                    # cancellation; the strong reference keeps the task alive.
                    task = asyncio.get_running_loop().create_task(
                        restore_unconsumed(event.event_id)
                    )
                    _PENDING_RESTORES.add(task)
                    task.add_done_callback(_PENDING_RESTORES.discard)
        polls += 1
        if max_polls is None or polls < max_polls:
            await asyncio.sleep(interval)


#: Strong references to in-flight restores — the `agent/turn_cost.py` `_PENDING` shape, because a
#: bare `create_task` from a dying generator is garbage-collectable mid-write.
_PENDING_RESTORES: set[asyncio.Task[None]] = set()
