"""Who, besides its owner, may reach a session — and the one rule every reader of that asks.

`D-2026-09-27-in-a-shared-session-the-sender-governs`. A member is someone the owner has let in:
they may read the transcript and send messages, and every message runs as its sender (their roles,
memories and spend caps). Membership widens who may reach a session and grants nothing else.

The owner alone admits and removes; a member may remove only themselves. Membership is read on every
non-owner request rather than cached, so removal takes effect on the next request.

Durable where sessions are durable, in-process where they are not. Durable rows cascade from
`session_owners`, so deleting, expiring or erasing a session takes its memberships with it.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import NamedTuple, Protocol

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.session_store import _session_connection, _session_dsn, owner_permits
from chemclaw.core.config import settings

# First writer wins: admitting somebody twice is one membership, and its `added_at` is when they
# were first let in.
_ADD = (
    "INSERT INTO session_members (session_id, actor) VALUES (%s, %s) "
    "ON CONFLICT (session_id, actor) DO NOTHING"
)
_REMOVE = "DELETE FROM session_members WHERE session_id = %s AND actor = %s"
_IS_MEMBER = "SELECT 1 FROM session_members WHERE session_id = %s AND actor = %s"
_MEMBERS = "SELECT actor, added_at FROM session_members WHERE session_id = %s ORDER BY added_at"
# Sessions somebody else owns that `actor` was let into. Joined to `session_owners` so owner and
# title are not stored twice.
_SHARED_WITH = (
    "SELECT m.session_id, o.owner, o.title, m.added_at, o.updated_at, o.profile "
    "FROM session_members m "
    "JOIN session_owners o ON o.session_id = m.session_id "
    "WHERE m.actor = %s ORDER BY m.added_at DESC"
)


class Member(NamedTuple):
    """One person the owner has let into a session, and since when."""

    actor: str
    added_at: datetime


class SharedSession(NamedTuple):
    """A session somebody else owns that the caller is a member of.

    `owner`, `title`, `updated_at` and `profile` are `None` under the in-process backend, which
    keeps memberships only. `GET /plans/pending` uses the last two to fold a member's sessions into
    its scan.
    """

    session_id: str
    owner: str | None
    title: str | None
    added_at: datetime
    updated_at: datetime | None = None
    profile: str | None = None


class MemberStore(Protocol):
    """The membership registry — durable or in-process, one contract."""

    async def add(self, session_id: str, actor: str) -> None:
        """Let `actor` into `session_id` (idempotent)."""
        ...

    async def remove(self, session_id: str, actor: str) -> bool:
        """Take `actor` out of `session_id`; whether they were a member."""
        ...

    async def is_member(self, session_id: str, actor: str) -> bool:
        """Whether `actor` is a member of `session_id`."""
        ...

    async def members(self, session_id: str) -> list[Member]:
        """Every member of `session_id`, earliest admitted first. The owner is not a member."""
        ...

    async def shared_with(self, actor: str) -> list[SharedSession]:
        """Every session `actor` is a member of, most recently admitted first."""
        ...


class SessionMemberStore:
    """`session_members`, on the session-store database (D-002)."""

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `chemclaw.agent.session_store`)."""
        return _session_connection(self._dsn)

    async def add(self, session_id: str, actor: str) -> None:
        """Let `actor` into `session_id`; admitting a member twice changes nothing."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_ADD, (session_id, actor))
            await conn.commit()

    async def remove(self, session_id: str, actor: str) -> bool:
        """Take `actor` out of `session_id`; `False` when they were not a member."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_REMOVE, (session_id, actor))
                removed = cur.rowcount > 0
            await conn.commit()
        return removed

    async def is_member(self, session_id: str, actor: str) -> bool:
        """Whether `actor` is a member of `session_id` — what every non-owner request asks."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_IS_MEMBER, (session_id, actor))
                return await cur.fetchone() is not None

    async def members(self, session_id: str) -> list[Member]:
        """Every member of `session_id`, earliest admitted first."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_MEMBERS, (session_id,))
                return [Member(str(row[0]), row[1]) for row in await cur.fetchall()]

    async def shared_with(self, actor: str) -> list[SharedSession]:
        """Every session `actor` has been let into, with its owner and title."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SHARED_WITH, (actor,))
                return [
                    SharedSession(str(row[0]), row[1], row[2], row[3], row[4], row[5])
                    for row in await cur.fetchall()
                ]


@dataclass(frozen=True)
class _Membership:
    """One in-process membership row."""

    session_id: str
    actor: str
    added_at: datetime


class InMemorySessionMemberStore:
    """The same contract for a deployment whose sessions are in-process too.

    Not a test double: `session_store="memory"` is a real deployment.
    """

    def __init__(self) -> None:
        """Start with nobody admitted anywhere."""
        self._rows: dict[tuple[str, str], _Membership] = {}

    async def add(self, session_id: str, actor: str) -> None:
        """Let `actor` into `session_id`; first writer wins, as `_ADD` does."""
        self._rows.setdefault(
            (session_id, actor), _Membership(session_id, actor, datetime.now(UTC))
        )

    async def remove(self, session_id: str, actor: str) -> bool:
        """Take `actor` out of `session_id`; `False` when they were not a member."""
        return self._rows.pop((session_id, actor), None) is not None

    async def is_member(self, session_id: str, actor: str) -> bool:
        """Whether `actor` is a member of `session_id`."""
        return (session_id, actor) in self._rows

    async def members(self, session_id: str) -> list[Member]:
        """Every member of `session_id`, earliest admitted first."""
        rows = sorted(
            (row for row in self._rows.values() if row.session_id == session_id),
            key=lambda row: row.added_at,
        )
        return [Member(row.actor, row.added_at) for row in rows]

    async def shared_with(self, actor: str) -> list[SharedSession]:
        """Every session `actor` is a member of, newest admission first; owner and title unknown."""
        rows = sorted(
            (row for row in self._rows.values() if row.actor == actor),
            key=lambda row: row.added_at,
            reverse=True,
        )
        return [SharedSession(row.session_id, None, None, row.added_at) for row in rows]


@cache
def session_member_store() -> MemberStore:
    """The membership store this deployment gets: durable where its sessions are durable.

    One instance per process, so the front door's writes and the agent's session-scoped reads see
    one registry under the in-process backend.
    """
    if settings.session_store == "postgres":
        return SessionMemberStore()
    return InMemorySessionMemberStore()


async def participant_permits(session_id: str, owner: str | None, actor: str | None) -> bool:
    """Whether `actor` may reach `session_id` — as its owner, or as a member the owner let in.

    Extends `owner_permits`: membership is asked only when the owner check says no, so a
    single-person session costs no extra statement. A session with no recorded owner has no members,
    and an unauthenticated request is never a member. Both the front door's session gate and the
    agent's session-scoped reads call this, so they cannot disagree.
    """
    if owner_permits(owner, actor):
        return True
    if not owner or not actor:
        return False
    return await session_member_store().is_member(session_id, actor)
