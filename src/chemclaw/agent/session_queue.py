"""The messages waiting for a session's running turn to end — an ordered, bounded, leased queue.

`D-2026-09-27-a-queued-message-waits-in-its-senders-request`. A message sent while another turn runs
on the session used to be refused 409 (`SessionTurnClaims`, D-121), which was the right
serialisation and the wrong answer once a session holds several people: the second chemist's
question is not a double-submit, it is the next thing somebody said. So it waits.

**What this module holds is the order, never the message.** A row names a session, a sender, a
ticket and a lease — no text, no roles. The waiting itself happens in the sender's own request
(`api/routes/turns.post_message`): its stream reports the position, and when the ticket reaches the
head and the session's turn claim comes free, that same request runs the turn with the principal
its own token established. That is how a queued message runs as its sender without this table
carrying anybody's authority, and it is why nothing here can run a message on its own: there is no
dispatcher, only waiters asking "am I next?".

**A lease, like the turn claim beside it, and for the same reason.** The waiter refreshes its row
every time it asks for its position; a waiter whose process died stops refreshing, and once its
lease lapses the rows behind it stop counting it and the next enqueue sweeps it. A crashed pod
therefore delays the queue by at most one lease, exactly as it delays the session's turn claim.

**Two backends, chosen as `session_members` chooses one**: durable where sessions are durable (two
replicas share one session, so they must share one order), process-lifetime where they are not.
The durable rows cascade from `session_owners` (`infra/sql/111_session_turn_queue.sql`), so deleting
a session, the retention sweep and an owner's erasure take its queue with it — which a waiter
notices on its next poll as its ticket being gone.
"""

import itertools
import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NamedTuple, Protocol

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.session_store import _session_connection, _session_dsn

#: Why an enqueue was refused. `full`: the session already holds its cap of waiting messages.
#: `waiting`: this sender already has a message waiting in this session — one each, so a member
#: cannot fill the queue ahead of everybody else and a client that retries a POST queues at most one
#: duplicate rather than a stream of them.
Refusal = Literal["full", "waiting"]


class QueueRefused(Exception):
    """The session cannot take another waiting message from this sender right now."""

    def __init__(self, reason: Refusal) -> None:
        """Carry which of the two limits refused, so the route can say which one."""
        super().__init__(reason)
        self.reason: Refusal = reason


class QueuedMessage(NamedTuple):
    """One waiting message as the queue knows it: its place, whose it is, and since when."""

    ticket: int
    sender: str
    enqueued_at: datetime


class TurnQueue(Protocol):
    """The per-session wait line — durable or in-process, one contract."""

    async def enqueue(
        self, session_id: str, sender: str, *, capacity: int, lease_seconds: float
    ) -> int:
        """Join the end of `session_id`'s line; the ticket, or `QueueRefused`."""
        ...

    async def position(self, session_id: str, ticket: int, lease_seconds: float) -> int | None:
        """Live tickets ahead of this one (0 = next), refreshing it; `None` once it is gone."""
        ...

    async def leave(self, session_id: str, ticket: int) -> None:
        """Take `ticket` out of the line (idempotent)."""
        ...

    async def waiting(self, session_id: str) -> list[QueuedMessage]:
        """Every live ticket in `session_id`, first in line first."""
        ...


# Serialises one session's enqueues so the cap check and the insert are one decision across
# replicas. A transaction-scoped advisory lock rather than a row lock on `session_owners`: a row
# lock needs UPDATE on that table and would queue behind the title write, while this names nothing
# but the queue and is gone at commit. Namespaced by prefix so it cannot meet another subsystem's
# `hashtextextended` key (`agent/skill_store.py`, `agent/behaviour_proposals.py`).
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_SWEEP = "DELETE FROM session_turn_queue WHERE session_id = %s AND lease_until <= now()"
_SENDERS = "SELECT sender FROM session_turn_queue WHERE session_id = %s"
_ENQUEUE = (
    "INSERT INTO session_turn_queue (session_id, sender, lease_until) "
    "VALUES (%s, %s, now() + make_interval(secs => %s)) RETURNING ticket"
)
# Refresh and count in one statement. The outer count reads the snapshot from before the UPDATE,
# which is exactly "the others"; a ticket that no longer exists returns no row at all, and that is
# how a waiter learns it was cancelled, erased or swept with its session. A *lapsed* ticket returns
# no row either rather than being revived: the rows behind it already stopped counting it, so
# letting it back in would put it ahead of messages that have moved up past it.
_POSITION = (
    "WITH mine AS ("
    "  UPDATE session_turn_queue SET lease_until = now() + make_interval(secs => %s) "
    "  WHERE session_id = %s AND ticket = %s AND lease_until > now() RETURNING ticket"
    ") "
    "SELECT (SELECT count(*) FROM session_turn_queue q "
    "        WHERE q.session_id = %s AND q.ticket < mine.ticket AND q.lease_until > now()) "
    "FROM mine"
)
_LEAVE = "DELETE FROM session_turn_queue WHERE session_id = %s AND ticket = %s"
_WAITING = (
    "SELECT ticket, sender, enqueued_at FROM session_turn_queue "
    "WHERE session_id = %s AND lease_until > now() ORDER BY ticket"
)


class SessionTurnQueue:
    """`session_turn_queue`, on the session-store database (D-002)."""

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `chemclaw.agent.session_store`)."""
        return _session_connection(self._dsn)

    async def enqueue(
        self, session_id: str, sender: str, *, capacity: int, lease_seconds: float
    ) -> int:
        """Join the line under the session's lock: sweep the dead, check both limits, insert.

        One transaction, so two replicas enqueueing at once cannot both see room for one.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LOCK, (f"session_turn_queue:{session_id}",))
                await cur.execute(_SWEEP, (session_id,))
                await cur.execute(_SENDERS, (session_id,))
                senders = [str(row[0]) for row in await cur.fetchall()]
                refusal = _refusal(senders, sender, capacity)
                if refusal is not None:
                    await conn.rollback()
                    raise QueueRefused(refusal)
                await cur.execute(_ENQUEUE, (session_id, sender, lease_seconds))
                row = await cur.fetchone()
            await conn.commit()
        assert row is not None  # an INSERT … RETURNING that did not raise returned its row
        return int(row[0])

    async def position(self, session_id: str, ticket: int, lease_seconds: float) -> int | None:
        """Refresh this ticket's lease and count the live tickets ahead of it."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_POSITION, (lease_seconds, session_id, ticket, session_id))
                row = await cur.fetchone()
            await conn.commit()
        return None if row is None else int(row[0])

    async def leave(self, session_id: str, ticket: int) -> None:
        """Delete this ticket; a ticket already gone is not an error."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LEAVE, (session_id, ticket))
            await conn.commit()

    async def waiting(self, session_id: str) -> list[QueuedMessage]:
        """The live line, first in line first."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_WAITING, (session_id,))
                return [
                    QueuedMessage(int(row[0]), str(row[1]), row[2]) for row in await cur.fetchall()
                ]


def _refusal(senders: list[str], sender: str, capacity: int) -> Refusal | None:
    """Which limit, if any, refuses `sender` a place among the live `senders` already waiting.

    `waiting` is asked first because it is the more specific answer: a chemist who already has a
    message in line is told that, rather than that the line is full.
    """
    if sender in senders:
        return "waiting"
    if len(senders) >= capacity:
        return "full"
    return None


@dataclass
class _Entry:
    """One in-process ticket; `lease_until` is on the monotonic clock."""

    ticket: int
    sender: str
    enqueued_at: datetime
    lease_until: float


class InMemoryTurnQueue:
    """The same contract for a deployment whose sessions are in-process too.

    Not a test double, for `InMemorySessionMemberStore`'s reason: `session_store="memory"` is a real
    deployment. Every method is free of `await`, so each is atomic on the event loop — the lock the
    durable backend takes is what that property costs across processes.
    """

    def __init__(self) -> None:
        """Start with nobody waiting anywhere."""
        self._lines: dict[str, list[_Entry]] = {}
        self._tickets = itertools.count(1)

    def _live(self, session_id: str) -> list[_Entry]:
        """The session's unexpired tickets, dropping the expired ones as it goes."""
        now = time.monotonic()
        line = [entry for entry in self._lines.get(session_id, []) if entry.lease_until > now]
        if line:
            self._lines[session_id] = line
        else:
            self._lines.pop(session_id, None)
        return line

    async def enqueue(
        self, session_id: str, sender: str, *, capacity: int, lease_seconds: float
    ) -> int:
        """Join the end of the line, or raise `QueueRefused`."""
        line = self._live(session_id)
        refusal = _refusal([entry.sender for entry in line], sender, capacity)
        if refusal is not None:
            raise QueueRefused(refusal)
        ticket = next(self._tickets)
        entry = _Entry(ticket, sender, datetime.now(UTC), time.monotonic() + lease_seconds)
        self._lines[session_id] = [*line, entry]
        return ticket

    async def position(self, session_id: str, ticket: int, lease_seconds: float) -> int | None:
        """Refresh this ticket and count the live tickets ahead of it; `None` once it is gone."""
        line = self._live(session_id)
        for index, entry in enumerate(line):
            if entry.ticket == ticket:
                entry.lease_until = time.monotonic() + lease_seconds
                return index
        return None

    async def leave(self, session_id: str, ticket: int) -> None:
        """Drop this ticket; a ticket already gone is not an error."""
        line = [entry for entry in self._live(session_id) if entry.ticket != ticket]
        if line:
            self._lines[session_id] = line
        else:
            self._lines.pop(session_id, None)

    async def waiting(self, session_id: str) -> list[QueuedMessage]:
        """The live line, first in line first."""
        return [
            QueuedMessage(entry.ticket, entry.sender, entry.enqueued_at)
            for entry in self._live(session_id)
        ]
