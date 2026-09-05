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
fail open, and the approval gate is decorative, or fail closed, and `make chat --admin` cannot run a
single state-changing tool? Both answers are bad, and the question is malformed. The paragraph
above is the whole argument for durability and it is an argument about a *mismatch*: the approval
must not outlive, or be outlived by, the mode it authorizes. Under `session_store="memory"` that
mode lives in an in-process session and dies with the process, so a process-lifetime approval
matches it exactly. So the backend follows the session store, there is no third posture to
configure, and the gate is fail-closed everywhere — a plan that was never approved is never
approved, whichever backend answered.

**Both halves of the control are durable, and for a while only one was.** A decision row here
survived anything; the marker recording that the row had been *spent* lived in `session.state`
(`harness_mode._CONSUMED_STATE_KEY`), which an LRU eviction or a pod roll drops —
`chemclaw.api.deps._rehydrate_session` rebuilds the session handle over the durable history alone.
So a session that reconstructed a byte-identical todo list after an eviction hashed to the same plan
and met its own already-spent approval looking fresh: an authorization revived by an infrastructure
event rather than by a person, outside the one-turn limit D-167 states.

Consumption is therefore recorded where the decision is — `plan_approvals.consumed_at`
(`infra/sql/034_plan_approval_consumption.sql`), stamped by `consume` and folded into `decision`,
which reports a spent approval as *not approved* while still naming who decided. That fold is the
point: every caller already asks this store one question ("is this plan approved right now?") and
now gets one answer, instead of asking here and then asking session state whether the answer still
counts. It also deletes the seam — there is no longer a `plan_consumed` for a caller to forget.

The in-memory backend mirrors it exactly, which costs nothing and matters: `session_store="memory"`
is a real deployment (the CLI is one), and a control with two implementations that disagree about
when an approval is spent is a control nobody can reason about.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.core.config import settings

# Append-only: every decision is a record of something a person did at a moment, so a second
# decision on the same plan is a second row rather than an update of the first. That is also what
# re-arms a plan: approving an unchanged plan again inserts a fresh, unspent row, so "yes, again"
# needs no separate operation and cannot be performed by anything but a decision.
_INSERT = (
    "INSERT INTO plan_approvals (session_id, plan_hash, actor, approved) VALUES (%s, %s, %s, %s)"
)

# The latest decision wins, so a rejection recorded after an approval revokes it — which is what a
# person clicking "no" second means. `LIMIT 1` over the covering index; no sort at run time.
#
# `approved AND consumed_at IS NULL` is the *effective* verdict: an approval that has had its turn
# is no longer an approval (D-167). The actor comes back either way, because "approved earlier,
# already used" is a different thing for a surface to show than "nobody has decided".
_LATEST = (
    "SELECT approved AND consumed_at IS NULL, actor FROM plan_approvals "
    "WHERE session_id = %s AND plan_hash = %s "
    "ORDER BY decided_at DESC, id DESC LIMIT 1"
)

# Spend every still-unspent approval this session holds, whatever plan each was recorded against.
# Session-wide rather than hash-targeted, because hash-targeted consumption leaked: a turn that
# reworded its plan mid-flight hashed the *new* plan at turn end, found no decision for it, and
# left the *old* plan's approval live — re-authorizing any future turn whose todo list hashed back
# to it, outside D-167's one-turn limit. "The turn used its authorization" is a fact about the
# session's turn, not about whichever plan identity survived to the end of it. Scoped by
# `approved AND consumed_at IS NULL` so it is idempotent and can never stamp a rejection.
_CONSUME_ALL = (
    "UPDATE plan_approvals SET consumed_at = now() "
    "WHERE session_id = %s AND approved AND consumed_at IS NULL"
)


@runtime_checkable
class ApprovalStore(Protocol):
    """Reads and writes the human decision on one session's plan, whichever backend holds it."""

    async def record(self, session_id: str, plan_hash: str, actor: str, approved: bool) -> None:
        """Record one human decision about one specific plan."""
        ...

    async def consume_all(self, session_id: str) -> None:
        """Spend every live approval this session holds, so the next turn needs its own."""
        ...

    async def decision(self, session_id: str, plan_hash: str) -> tuple[bool, str] | None:
        """The latest *effective* `(approved, actor)`, or None if nobody has decided."""
        ...


class PlanApprovalStore:
    """Reads and writes the human decision on one session's plan."""

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `chemclaw.agent.session_store`)."""
        return _session_connection(self._dsn)

    async def record(self, session_id: str, plan_hash: str, actor: str, approved: bool) -> None:
        """Record one human decision about one specific plan."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_INSERT, (session_id, plan_hash, actor, approved))
            await conn.commit()

    async def consume_all(self, session_id: str) -> None:
        """Stamp every live approval this session holds as spent — durably.

        Idempotent by construction (`_CONSUME_ALL` matches only unspent approvals), because the
        callers cannot guarantee they run once: a turn that answers and is then torn down, and a
        turn that fails after running tools, both spend the same approvals. Session-wide for the
        drift-leak reason the SQL's own comment carries.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_CONSUME_ALL, (session_id,))
            await conn.commit()

    async def decision(self, session_id: str, plan_hash: str) -> tuple[bool, str] | None:
        """The latest *effective* `(approved, actor)`, or None if nobody has decided.

        Effective, not merely recorded: an approval that has already had its turn comes back
        `approved=False` (`_LATEST` folds `consumed_at IS NULL` into the verdict). Returning the
        actor as well is what lets a caller say *who* decided rather than only *that* it was
        approved — the difference between a usable record and a flag, and what separates
        "approved earlier, already used" from "nobody has decided".
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LATEST, (session_id, plan_hash))
                row = await cur.fetchone()
        return (bool(row[0]), str(row[1])) if row is not None else None


@dataclass
class _Decision:
    """One recorded decision, with the moment it was spent — the in-memory row of `plan_approvals`.

    A mutable record rather than a tuple precisely because `consumed_at` is the one field that
    changes after the fact, exactly as migration 034 makes it the one column an UPDATE may touch.
    """

    session_id: str
    plan_hash: str
    actor: str
    approved: bool
    consumed_at: datetime | None = None


class InMemoryPlanApprovalStore:
    """The same contract for a deployment whose sessions are in-process too.

    Append-only like its Postgres sibling, and for the same reason: a second decision on one plan
    is a second thing a person did, not an edit of the first, so a rejection after an approval
    revokes rather than overwrites, and re-approving an unchanged plan re-arms it. Reading the last
    matching entry and folding `consumed_at` into the verdict reproduces `_LATEST` exactly; spending
    the latest unspent approval reproduces `_CONSUME`.

    It is *not* a test double. It is the backend a `session_store="memory"` deployment gets, and
    the CLI is a real one of those — so the harness gate holds there rather than being waived, and
    what it holds against has precisely the lifetime of the session state it authorizes.

    **The list is append-only and `_latest` scans it backwards, and that is measured rather than
    defended.** A review filed the unbounded growth as a defect; the numbers say otherwise, so they
    are here instead of a bounded structure nobody needs. At 100 decisions — a long CLI session —
    the worst-case lookup is 0.005 ms; at 10,000 it is 0.2 ms and 0.6 MB; at 200,000, which no
    process reaching this backend will see, 3.5 ms and 12.8 MB. The shipped chart sets
    `session_store="postgres"`, so a deployed fleet uses `PlanApprovalStore` and never this. Adding
    an eviction policy here would buy nothing and cost a second definition of "which approval is
    live" — the one thing the two backends must not disagree about.
    """

    def __init__(self) -> None:
        """Start with no decisions recorded."""
        self._decisions: list[_Decision] = []

    def _latest(self, session_id: str, plan_hash: str) -> _Decision | None:
        """The most recent decision for this plan, mirroring `_LATEST`'s ordering."""
        for decision in reversed(self._decisions):
            if decision.session_id == session_id and decision.plan_hash == plan_hash:
                return decision
        return None

    async def record(self, session_id: str, plan_hash: str, actor: str, approved: bool) -> None:
        """Append one human decision about one specific plan."""
        self._decisions.append(_Decision(session_id, plan_hash, actor, approved))

    async def consume_all(self, session_id: str) -> None:
        """Spend every live approval this session holds, mirroring `_CONSUME_ALL` exactly."""
        for decision in self._decisions:
            if (
                decision.session_id == session_id
                and decision.approved
                and decision.consumed_at is None
            ):
                decision.consumed_at = datetime.now(UTC)

    async def decision(self, session_id: str, plan_hash: str) -> tuple[bool, str] | None:
        """The latest *effective* `(approved, actor)`, or None if nobody has decided."""
        latest = self._latest(session_id, plan_hash)
        if latest is None:
            return None
        return (latest.approved and latest.consumed_at is None, latest.actor)


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
