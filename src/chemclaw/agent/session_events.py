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
that with the concurrent double-delivery this fixes). That window used to span the whole
claim-to-SSE-write gap, and the sentence justifying it — "the durable result already lives in the
graph/session" — had quietly stopped covering the case that matters: for a job finishing while no
turn is open, this row is the *only* thing that tells anyone. So the tailer now restores a row
whose yield never completed (`restore_unconsumed`), shrinking the loss window to the transport
itself, and the model reads the mailbox at turn start (`api/runner._with_pushed_job_results`)
beside the browser's stream.
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
# Put a claimed-but-undelivered row back. The claim is at-most-once by design, and for most of its
# life the whole window was "claim-commit to SSE write" — a drop in it silently destroyed the one
# signal that a chemist's long search had finished. The tailer now restores a row whose yield never
# completed, which shrinks the loss window to the transport itself.
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

    `dedupe_key` is the writer's deterministic identity for this logical event: the Temporal
    activity that records it is retried at-least-once, so a retry after a committed-but-unacked
    insert would deliver the same notification twice. With a key set, the second insert lands on
    the unique index and becomes a no-op; None (non-retrying writers) appends unconditionally.
    """
    async with db.connection(_dsn(dsn)) as conn:
        await conn.execute(_INSERT, (session_id, kind, Jsonb(payload or {}), dedupe_key))
        await conn.commit()


async def claim_unconsumed(
    session_id: str, *, kinds: Sequence[str] | None = None, dsn: str | None = None
) -> list[SessionEvent]:
    """Atomically claim (mark consumed) and return a session's unconsumed events in arrival order.

    One `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING` statement, so a concurrent tailer cannot claim
    the same rows (COR-4). Rows are re-sorted by id since RETURNING order is unspecified. `kinds`
    scopes the claim to those event kinds (None claims everything): the claim is at-most-once, so a
    kind-selective consumer must filter here, never after the claim.

    The tailer calls this once per poll rather than holding a connection of its own, so the whole
    claim — connection included — lives in this one function.
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

    The compensating half of the claim, used only for a row whose delivery did not complete. A
    restore can at worst turn at-most-once into at-least-once for that one row (the yield may have
    reached the transport before the teardown landed), and a duplicated "your job finished" card
    is the cheap side of that trade against a silently lost one.
    """
    try:
        async with db.connection(_dsn(dsn)) as conn:
            await conn.execute(_RESTORE, (event_id,))
            await conn.commit()
    except Exception:
        # Never raises: it runs as an unawaited teardown task, where an escaping error surfaces
        # only as an unattributed "Task exception was never retrieved" — and losing the restore
        # merely returns this one row to the at-most-once behaviour the claim always had.
        logger.warning("could not restore undelivered session event %d", event_id, exc_info=True)


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

    The default (database) path **borrows a connection per poll** rather than holding one for the
    stream's lifetime. Holding one was right while every connection was a fresh handshake — a
    2-second poll loop would otherwise have churned one connect per stream per interval. With the
    front door pooling (`chemclaw.core.db.pooling`) the borrow is free and holding is the expensive
    choice: `service_max_event_streams_per_user` is 5, so 50 chemists is 250 streams, and 250
    connections pinned for the lifetime of open browser tabs would exhaust the pool for the turns
    that actually need it. A connection failure ends the stream (the client reconnects), exactly
    as before.

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
    do_claim: Callable[[], Awaitable[list[SessionEvent]]] = (
        partial(claim, session_id)
        if claim is not None
        else partial(claim_unconsumed, session_id, kinds=kinds)
    )
    polls = 0
    while max_polls is None or polls < max_polls:
        for event in await do_claim():
            delivered = False
            try:
                yield event
                delivered = True
            finally:
                if not delivered and event.event_id is not None:
                    # The consumer went away between the claim and the yield completing — a
                    # dropped SSE stream, a cancelled task. Restored on a task of its own because
                    # this `finally` runs inside the teardown, where an `await` re-raises the
                    # cancellation; the strong reference keeps the write alive until it lands.
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
