"""Who, besides its owner, may reach a session — and the one rule every reader of that asks.

`D-2026-09-27-in-a-shared-session-the-sender-governs`. A session has one owner, recorded once in
`session_owners`, and until this module that owner was the only person who could reach it
(`session_store.owner_permits`). A **member** is someone the owner has let in: they may read the
transcript and send messages, and every message they send runs as *them* — their roles, their
memories, their spend caps — because the turn's identity is always the sender's
(`api/routes/turns.post_message` passes the request's principal to `run_turn`, never the owner).
Membership widens who may reach a session and grants nothing else.

**The owner alone admits and removes** (`PUT`/`DELETE /sessions/{id}/members/{actor}`); a member may
remove only themselves. Membership is read on every request that is not the owner's rather than
cached on the live session, so removing somebody takes effect on their next request rather than on
the next cache eviction.

**Two backends, chosen as `plan_approval_store` chooses one**: durable where sessions are durable,
process-lifetime where they are not, so a membership never outlives or is outlived by the session it
admits to. The durable rows cascade from `session_owners` (`infra/sql/110_shared_sessions.sql`), so
deleting, expiring or erasing a session takes its memberships with it.
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
# The sessions somebody else owns that `actor` has been let into, with what a conversation list
# shows about each. Joined to `session_owners` rather than stored twice: the owner and the title are
# that row's facts, and a copy here would be a second answer to "whose session is this".
_SHARED_WITH = (
    "SELECT m.session_id, o.owner, o.title, m.added_at FROM session_members m "
    "JOIN session_owners o ON o.session_id = m.session_id "
    "WHERE m.actor = %s ORDER BY m.added_at DESC"
)


class Member(NamedTuple):
    """One person the owner has let into a session, and since when."""

    actor: str
    added_at: datetime


class SharedSession(NamedTuple):
    """A session somebody else owns that the caller is a member of.

    `owner` and `title` are `None` under the in-process backend, which keeps memberships and nothing
    else; the durable one reads both off the session's ownership row.
    """

    session_id: str
    owner: str | None
    title: str | None
    added_at: datetime


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
                    SharedSession(str(row[0]), row[1], row[2], row[3])
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

    Not a test double, for `InMemoryPlanApprovalStore`'s reason: `session_store="memory"` is a real
    deployment, and a membership there has exactly the lifetime of the session it admits to.
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

    One instance per process, for `plan_approval_store`'s reason: the front door writes memberships
    and the agent's own session-scoped tools read them (`agent/evidence_tools.py`), and under the
    in-process backend a second instance would be a second, empty registry.
    """
    if settings.session_store == "postgres":
        return SessionMemberStore()
    return InMemorySessionMemberStore()


async def participant_permits(session_id: str, owner: str | None, actor: str | None) -> bool:
    """Whether `actor` may reach `session_id` — as its owner, or as a member the owner let in.

    **The one rule, and it extends `owner_permits` rather than replacing it.** The owner is decided
    exactly as before, dev/enforced split included, and the membership question is asked only when
    that answer is no — so a single-person session costs no extra statement, and a session with no
    recorded owner can have no members: nobody holds the standing to have admitted them. A request
    with no authenticated actor is never a member.

    Read by the front door's session gate (`api/deps._resolve_session`) and by the agent's own
    session-scoped read (`agent/evidence_tools.assemble_evidence_pack`), so a route and a tool
    cannot disagree about who is in a conversation.
    """
    if owner_permits(owner, actor):
        return True
    if not owner or not actor:
        return False
    return await session_member_store().is_member(session_id, actor)
