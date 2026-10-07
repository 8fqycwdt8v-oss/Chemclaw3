"""Durable record of the human decision on a harness plan (D-137).

The store behind `plan_approvals`, using `session_store`'s DSN resolution. An approval must have
exactly the lifetime of the mode it authorizes, so the backend follows the session store: Postgres
for durable sessions, in-process for `session_store="memory"` (the CLI). Both backends behave
identically and fail closed.

Consumption is recorded on the row (`consumed_at`) and folded into `decision`, so an approval is
spent durably and cannot be revived by a session rebuild. Each row also carries `scope`, the tool
names the plan's steps declared when the human read it, stamped by the decision and never
re-derived from the live plan; the plan hash covers each step's declaration, so a widened plan is
a different plan. A `status` flip changes neither.
"""

from collections.abc import Collection
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import NamedTuple, Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.core.config import settings

# Append-only: each decision is a separate act, so re-approving an unchanged plan inserts a fresh,
# unspent row.
_INSERT = (
    "INSERT INTO plan_approvals (session_id, plan_hash, actor, approved, scope) "
    "VALUES (%s, %s, %s, %s, %s)"
)

# The latest decision wins, so a later rejection revokes an approval. `approved AND consumed_at IS
# NULL` is the effective verdict; the actor is returned either way so "already used" differs from
# "nobody decided".
_LATEST = (
    "SELECT approved AND consumed_at IS NULL, actor, scope FROM plan_approvals "
    "WHERE session_id = %s AND plan_hash = %s "
    "ORDER BY decided_at DESC, id DESC LIMIT 1"
)

# Spend every unspent approval in the session, whatever plan it was recorded against: a turn that
# reworded its plan must not leave the old plan's approval live. Idempotent, and never stamps a
# rejection.
_CONSUME_ALL = (
    "UPDATE plan_approvals SET consumed_at = now() "
    "WHERE session_id = %s AND approved AND consumed_at IS NULL"
)


# Whose turn last wrote a plan, keyed by plan identity
# (`D-2026-09-27-in-a-shared-session-the-sender-governs`). Last writer wins. A session with no
# `session_owners` row records no author, and the decision route falls back to the owner rule.
_AUTHOR_UPSERT = (
    "INSERT INTO plan_authors (session_id, plan_hash, actor) "
    "SELECT o.session_id, %s, %s FROM session_owners o WHERE o.session_id = %s "
    "ON CONFLICT (session_id, plan_hash) DO UPDATE "
    "SET actor = EXCLUDED.actor, recorded_at = now()"
)
_AUTHOR = "SELECT actor FROM plan_authors WHERE session_id = %s AND plan_hash = %s"


class Decision(NamedTuple):
    """One decision as the store answers it: the verdict, who took it, and what it permits.

    A `NamedTuple` because older readers index `[0]` and `[1]`. `scope` is the set of tool names the
    approval authorizes; empty (every pre-scope row) authorizes no state-changing tool.
    """

    approved: bool
    actor: str
    scope: frozenset[str]


@runtime_checkable
class ApprovalStore(Protocol):
    """Reads and writes the human decision on one session's plan, whichever backend holds it."""

    async def record(
        self,
        session_id: str,
        plan_hash: str,
        actor: str,
        approved: bool,
        scope: Collection[str],
    ) -> None:
        """Record one human decision about one specific plan, and what it authorizes."""
        ...

    async def consume_all(self, session_id: str) -> None:
        """Spend every live approval this session holds, so the next turn needs its own."""
        ...

    async def record_author(self, session_id: str, plan_hash: str, actor: str) -> None:
        """Record that `actor`'s turn is the last to have written this plan."""
        ...

    async def author(self, session_id: str, plan_hash: str) -> str | None:
        """Whose turn last wrote this plan, or `None` when nobody is recorded."""
        ...

    async def decision(self, session_id: str, plan_hash: str) -> Decision | None:
        """The latest *effective* decision, or None if nobody has decided."""
        ...


class PlanApprovalStore:
    """Reads and writes the human decision on one session's plan."""

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `chemclaw.agent.session_store`)."""
        return _session_connection(self._dsn)

    async def record(
        self,
        session_id: str,
        plan_hash: str,
        actor: str,
        approved: bool,
        scope: Collection[str],
    ) -> None:
        """Record one human decision about one specific plan, and what it authorizes.

        `scope` has no default so no caller records an empty authorization by accident. Stored
        sorted.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_INSERT, (session_id, plan_hash, actor, approved, sorted(scope)))
            await conn.commit()

    async def consume_all(self, session_id: str) -> None:
        """Stamp every live approval this session holds as spent — durably.

        Idempotent, because more than one teardown path may spend the same approvals.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_CONSUME_ALL, (session_id,))
            await conn.commit()

    async def record_author(self, session_id: str, plan_hash: str, actor: str) -> None:
        """Stamp `actor` as the author of this plan (`_AUTHOR_UPSERT`: last writer wins)."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_AUTHOR_UPSERT, (plan_hash, actor, session_id))
            await conn.commit()

    async def author(self, session_id: str, plan_hash: str) -> str | None:
        """Whose turn last wrote this plan, or `None` when no author is recorded."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_AUTHOR, (session_id, plan_hash))
                row = await cur.fetchone()
        return None if row is None else str(row[0])

    async def decision(self, session_id: str, plan_hash: str) -> Decision | None:
        """The latest *effective* decision, or None if nobody has decided.

        A spent approval comes back `approved=False`, still naming the actor. `scope` is `NOT NULL
        DEFAULT '{}'`, so rows predating it read as authorizing nothing.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LATEST, (session_id, plan_hash))
                row = await cur.fetchone()
        if row is None:
            return None
        return Decision(bool(row[0]), str(row[1]), frozenset(row[2]))


@dataclass
class _Decision:
    """One recorded decision, with the moment it was spent — the in-memory row of `plan_approvals`.

    Mutable because `consumed_at` changes after the fact.
    """

    session_id: str
    plan_hash: str
    actor: str
    approved: bool
    scope: frozenset[str]
    consumed_at: datetime | None = None


class InMemoryPlanApprovalStore:
    """The same contract for a deployment whose sessions are in-process too.

    Not a test double: it is the backend `session_store="memory"` deployments (the CLI) get, so the
    gate holds there too. Append-only and scanned backwards to reproduce `_LATEST` exactly; growth
    is
    bounded in practice by one process's session and deployed fleets use Postgres.
    """

    def __init__(self) -> None:
        """Start with no decisions recorded."""
        self._decisions: list[_Decision] = []
        self._authors: dict[tuple[str, str], str] = {}

    def _latest(self, session_id: str, plan_hash: str) -> _Decision | None:
        """The most recent decision for this plan, mirroring `_LATEST`'s ordering."""
        for decision in reversed(self._decisions):
            if decision.session_id == session_id and decision.plan_hash == plan_hash:
                return decision
        return None

    async def record(
        self,
        session_id: str,
        plan_hash: str,
        actor: str,
        approved: bool,
        scope: Collection[str],
    ) -> None:
        """Append one human decision about one specific plan, and what it authorizes."""
        self._decisions.append(_Decision(session_id, plan_hash, actor, approved, frozenset(scope)))

    async def consume_all(self, session_id: str) -> None:
        """Spend every live approval this session holds, mirroring `_CONSUME_ALL` exactly."""
        for decision in self._decisions:
            if (
                decision.session_id == session_id
                and decision.approved
                and decision.consumed_at is None
            ):
                decision.consumed_at = datetime.now(UTC)

    async def record_author(self, session_id: str, plan_hash: str, actor: str) -> None:
        """Stamp `actor` as the author of this plan; last writer wins, as `_AUTHOR_UPSERT` does."""
        self._authors[(session_id, plan_hash)] = actor

    async def author(self, session_id: str, plan_hash: str) -> str | None:
        """Whose turn last wrote this plan, or `None` when no author is recorded."""
        return self._authors.get((session_id, plan_hash))

    async def decision(self, session_id: str, plan_hash: str) -> Decision | None:
        """The latest *effective* decision, or None if nobody has decided."""
        latest = self._latest(session_id, plan_hash)
        if latest is None:
            return None
        return Decision(latest.approved and latest.consumed_at is None, latest.actor, latest.scope)


@cache
def plan_approval_store() -> ApprovalStore:
    """The approval store this deployment gets: durable where its sessions are durable.

    One instance per process so the decision route and `plan_gate` see the same in-memory store.
    Gated on `session_store`, like `default_audit_sink` and `history_provider`.
    """
    if settings.session_store == "postgres":
        return PlanApprovalStore()
    return InMemoryPlanApprovalStore()
