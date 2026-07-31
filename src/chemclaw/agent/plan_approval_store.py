"""Durable record of the human decision on a harness plan (REV-1, D-137).

The store behind `plan_approvals`. Kept beside `chemclaw.agent.session_store` and using the same DSN
resolution, because a plan approval is durable session-scoped evidence with exactly the lifetime
of the session's history — one database, one connection story (D-002).

Why a store at all, rather than session state: an approval that lived only in the front door's
in-process session would be lost on a pod restart or an LRU eviction, and the mode it authorized
would survive it (the mode lives in the session's own persisted state). A control that silently
disappears while its effect persists is worse than no control — the audit would show a session
running in execute mode with nothing recording who allowed it.

**Two backends, chosen the way `default_audit_sink` chooses one** (D-167). Enforcing the approval
raised a question recording it never had to answer: what does a deployment with no Postgres do —
fail open, and the GxP gate is decorative, or fail closed, and `make chat --admin` cannot run a
single state-changing tool? Both answers are bad, and the question is malformed. The paragraph
above is the whole argument for durability and it is an argument about a *mismatch*: the approval
must not outlive, or be outlived by, the mode it authorizes. Under `session_store="memory"` that
mode lives in an in-process session and dies with the process, so a process-lifetime approval
matches it exactly; under `postgres` both are durable. So the backend follows the session store,
there is no third posture to configure, and the gate is fail-closed everywhere — a plan that was
never approved is never approved, whichever backend answered.
"""

from contextlib import AbstractAsyncContextManager
from functools import cache
from typing import Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.core.config import settings

# Append-only: every decision is a GxP record of something a person did at a moment, so a second
# decision on the same plan is a second row rather than an update of the first.
_INSERT = (
    "INSERT INTO plan_approvals (session_id, plan_hash, actor, approved) VALUES (%s, %s, %s, %s)"
)

# The latest decision wins, so a rejection recorded after an approval revokes it — which is what a
# person clicking "no" second means. `LIMIT 1` over the covering index; no sort at run time.
_LATEST = (
    "SELECT approved, actor FROM plan_approvals "
    "WHERE session_id = %s AND plan_hash = %s "
    "ORDER BY decided_at DESC, id DESC LIMIT 1"
)


@runtime_checkable
class ApprovalStore(Protocol):
    """Reads and writes the human decision on one session's plan, whichever backend holds it."""

    async def record(self, session_id: str, plan_hash: str, actor: str, approved: bool) -> None:
        """Record one human decision about one specific plan."""
        ...

    async def decision(self, session_id: str, plan_hash: str) -> tuple[bool, str] | None:
        """The latest `(approved, actor)` for this exact plan, or None if nobody has decided."""
        ...


class PlanApprovalStore:
    """Reads and writes the human decision on one session's plan."""

    def __init__(self, *, dsn: str | None = None) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn(dsn)

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `chemclaw.agent.session_store`)."""
        return _session_connection(self._dsn)

    async def record(self, session_id: str, plan_hash: str, actor: str, approved: bool) -> None:
        """Record one human decision about one specific plan."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_INSERT, (session_id, plan_hash, actor, approved))
            await conn.commit()

    async def decision(self, session_id: str, plan_hash: str) -> tuple[bool, str] | None:
        """The latest `(approved, actor)` for this exact plan, or None if nobody has decided.

        Returning the actor as well as the verdict is what lets a caller say *who* approved rather
        than only *that* it was approved — the difference between a usable GxP record and a flag.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LATEST, (session_id, plan_hash))
                row = await cur.fetchone()
        return (bool(row[0]), str(row[1])) if row is not None else None


class InMemoryPlanApprovalStore:
    """The same contract for a deployment whose sessions are in-process too.

    Append-only like its Postgres sibling, and for the same reason: a second decision on one plan
    is a second thing a person did, not an edit of the first, so a rejection after an approval
    revokes rather than overwrites. Reading the last matching entry reproduces `_LATEST` exactly.

    It is *not* a test double. It is the backend a `session_store="memory"` deployment gets, and
    the CLI is a real one of those — so the harness gate holds there rather than being waived, and
    what it holds against has precisely the lifetime of the session state it authorizes.
    """

    def __init__(self) -> None:
        """Start with no decisions recorded."""
        self._decisions: list[tuple[str, str, str, bool]] = []

    async def record(self, session_id: str, plan_hash: str, actor: str, approved: bool) -> None:
        """Append one human decision about one specific plan."""
        self._decisions.append((session_id, plan_hash, actor, approved))

    async def decision(self, session_id: str, plan_hash: str) -> tuple[bool, str] | None:
        """The latest `(approved, actor)` for this exact plan, or None if nobody has decided."""
        for stored_session, stored_hash, actor, approved in reversed(self._decisions):
            if stored_session == session_id and stored_hash == plan_hash:
                return (approved, actor)
        return None


@cache
def plan_approval_store() -> ApprovalStore:
    """The approval store this deployment gets: durable where its sessions are durable.

    One instance per process, because the two callers must see the same decisions: the front door's
    `POST /sessions/{id}/plan/decision` writes, and `chemclaw.agent.plan_gate` reads. Under Postgres
    that would hold anyway; under the in-memory backend a second instance would be a second, empty
    store, and an approval recorded through the route would be invisible to the gate — which fails
    closed, so the symptom would be "approving does nothing" rather than anything unsafe, but it
    would still be broken.

    Gated on `session_store` for the reason the module docstring gives, and matching the polarity
    `default_audit_sink` and `history_provider` already use: that switch is a deployment's statement
    that a Postgres exists and durable records belong in it.
    """
    if settings.session_store == "postgres":
        return PlanApprovalStore()
    return InMemoryPlanApprovalStore()
