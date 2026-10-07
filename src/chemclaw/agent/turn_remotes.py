"""Requests to the process holding a session's turn, from a replica that does not hold it.

A running turn's pump, readers and cancel live in the memory of the process that started it
(`api/detach.DetachableTurn`), so a reattach or Stop arriving at another replica must be relayed.
A non-holding replica writes a request naming the turn's claim (`session_turns.holder`) — follow
or stop — and polls for the answer. The holder polls for requests addressed to its claims
(`api/turn_relay.TurnRelay`), acts with the same calls the local routes use, writes its answer, and
for a follow relays frames into `session_turn_frames`, which the asker consumes by deleting them.
Every statement borrows a pooled connection briefly; no LISTEN.

A request is a lease the asker refreshes as it polls; a dead asker's request lapses after one
lease and is swept. Naming the `holder` means only the addressed turn can answer it. Both tables
cascade from `session_owners`. Postgres store only: in memory a session is one process.
"""

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, NamedTuple

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.core.jsonb import json_column

#: What a request asks of the holder. `unload_stop` is a stop sent by a page being discarded, which
#: the holder defers exactly as the local route does.
Kind = Literal["watch", "stop", "unload_stop"]

#: The holder's answer, written onto the request. `asked` until the holder has read it; then
#: `watching` (frames follow), `refused` (the turn already has as many watchers as it accepts),
#: `gone` (the turn ended before it could be served), `deferred` (an unload stop is waiting out its
#: grace), `stopping` (the cancel is in flight) and `stopped` (the turn's teardown has finished).
State = Literal["asked", "watching", "refused", "gone", "deferred", "stopping", "stopped"]


class Holding(NamedTuple):
    """Who holds a session's live turn claim, and who sent the turn it covers."""

    holder: str
    #: `None` on a claim the previous image took, which recorded no sender.
    actor: str | None


class Request(NamedTuple):
    """One live request addressed to a turn this process holds."""

    id: str
    session_id: str
    holder: str
    kind: Kind
    actor: str
    state: State


_HOLDING = "SELECT holder, actor FROM session_turns WHERE session_id = %s AND expires_at > now()"
_ASK = (
    "INSERT INTO session_turn_remotes (id, session_id, holder, kind, actor, state, lease_until) "
    "VALUES (%s, %s, %s, %s, %s, 'asked', now() + make_interval(secs => %s))"
)
# The asker's poll: refresh its lease and read the holder's answer in one statement. No row means
# the request is gone — swept by the holder after a lapse, or cascaded away with its session.
_REFRESH = (
    "UPDATE session_turn_remotes SET lease_until = now() + make_interval(secs => %s) "
    "WHERE id = %s RETURNING state, correlation_id"
)
# Consumed by deletion, so a frame is delivered once and a view leaves nothing behind it as it
# reads. `RETURNING` order is unspecified; the caller sorts by `seq`.
_TAKE = "DELETE FROM session_turn_frames WHERE remote_id = %s RETURNING seq, frame"
_WITHDRAW = "DELETE FROM session_turn_remotes WHERE id = %s"
# The holder's poll. `unnest` pairs the two arrays element-wise, so one statement reads every
# request for any turn this process holds; lapsed requests are excluded (`_SWEEP`).
_PENDING = (
    "SELECT r.id, r.session_id, r.holder, r.kind, r.actor, r.state "
    "FROM session_turn_remotes r "
    "JOIN unnest(%s::text[], %s::text[]) AS t(session_id, holder) "
    "  ON r.session_id = t.session_id AND r.holder = t.holder "
    "WHERE r.lease_until > now()"
)
_SWEEP = (
    "DELETE FROM session_turn_remotes r "
    "USING unnest(%s::text[], %s::text[]) AS t(session_id, holder) "
    "WHERE r.session_id = t.session_id AND r.holder = t.holder AND r.lease_until <= now()"
)
_ANSWER = "UPDATE session_turn_remotes SET state = %s, correlation_id = %s WHERE id = %s"
_RELAY = "INSERT INTO session_turn_frames (remote_id, frame) VALUES (%s, %s)"


class TurnRemotes:
    """`session_turn_remotes` and `session_turn_frames`, on the session-store database (D-002)."""

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `chemclaw.agent.session_store`)."""
        return _session_connection(self._dsn)

    # -- the asking replica --------------------------------------------------------------------

    async def holding(self, session_id: str) -> Holding | None:
        """The live claim on `session_id` — whichever process holds it — or `None`."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_HOLDING, (session_id,))
                row = await cur.fetchone()
        if row is None:
            return None
        return Holding(str(row[0]), None if row[1] is None else str(row[1]))

    async def ask(
        self, session_id: str, holder: str, kind: Kind, actor: str, lease_seconds: float
    ) -> str:
        """Address a request to the turn `holder` holds on `session_id`; its id."""
        request_id = uuid.uuid4().hex
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _ASK, (request_id, session_id, holder, kind, actor, lease_seconds)
                )
            await conn.commit()
        return request_id

    async def refresh(self, request_id: str, lease_seconds: float) -> tuple[State, str] | None:
        """Keep the request alive and read the holder's answer; `None` once the request is gone."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_REFRESH, (lease_seconds, request_id))
                row = await cur.fetchone()
            await conn.commit()
        if row is None:
            return None
        return row[0], "" if row[1] is None else str(row[1])

    async def poll(
        self, request_id: str, lease_seconds: float
    ) -> tuple[tuple[State, str] | None, list[dict[str, str] | None]]:
        """One poll of a followed view: refresh the request, and take what was relayed since.

        One transaction, so a view costs one round trip per poll. The frames come oldest first and
        are deleted as they are taken; a `None` among them is the holder ending the view.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_REFRESH, (lease_seconds, request_id))
                answer = await cur.fetchone()
                await cur.execute(_TAKE, (request_id,))
                rows = await cur.fetchall()
            await conn.commit()
        frames = [frame for _seq, frame in sorted(rows, key=lambda row: int(row[0]))]
        if answer is None:
            return None, frames
        return (answer[0], "" if answer[1] is None else str(answer[1])), frames

    async def withdraw(self, request_id: str) -> None:
        """Delete the request and every frame not yet taken (idempotent)."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_WITHDRAW, (request_id,))
            await conn.commit()

    # -- the holding process -------------------------------------------------------------------

    async def pending(self, turns: Sequence[tuple[str, str]]) -> list[Request]:
        """Every live request addressed to one of these `(session_id, holder)` turns.

        Sweeps the lapsed ones in the same round trip, so an asker that died leaves its request
        behind for one lease at most.
        """
        if not turns:
            return []
        sessions = [session_id for session_id, _holder in turns]
        holders = [holder for _session_id, holder in turns]
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SWEEP, (sessions, holders))
                await cur.execute(_PENDING, (sessions, holders))
                rows = await cur.fetchall()
            await conn.commit()
        return [Request(*(str(value) for value in row)) for row in rows]  # type: ignore[arg-type]

    async def answer(self, request_id: str, state: State, correlation_id: str = "") -> None:
        """Write the holder's answer onto the request."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_ANSWER, (state, correlation_id or None, request_id))
            await conn.commit()

    async def relay(self, request_id: str, frames: Sequence[dict[str, Any] | None]) -> None:
        """Append frames for the asker to take, in order; `None` marks the end of the view.

        A request the asker has already withdrawn takes its frames' foreign key with it, so the
        insert fails rather than strand rows nobody will read — `psycopg.errors.
        ForeignKeyViolation`, which the relay reads as the view having gone.
        """
        if not frames:
            return
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    _RELAY,
                    [
                        (request_id, None if frame is None else json_column(frame))
                        for frame in frames
                    ],
                )
            await conn.commit()
