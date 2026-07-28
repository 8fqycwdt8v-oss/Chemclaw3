"""Durable record of the human decision on a harness plan (REV-1, D-137).

The store behind `plan_approvals`. Kept beside `agents.session_store` and using the same DSN
resolution, because a plan approval is durable session-scoped evidence with exactly the lifetime
of the session's history — one database, one connection story (D-002).

Why a store at all, rather than session state: an approval that lived only in the front door's
in-process session would be lost on a pod restart or an LRU eviction, and the mode it authorized
would survive it (the mode lives in the session's own persisted state). A control that silently
disappears while its effect persists is worse than no control — the audit would show a session
running in execute mode with nothing recording who allowed it.
"""

from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import TupleRow

from agents.session_store import _session_connection, _session_dsn

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


class PlanApprovalStore:
    """Reads and writes the human decision on one session's plan."""

    def __init__(self, *, dsn: str | None = None) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn(dsn)

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `agents.session_store`)."""
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
