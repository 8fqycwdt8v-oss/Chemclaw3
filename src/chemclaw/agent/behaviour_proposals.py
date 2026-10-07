"""Proposed changes to what the agent *does*, and what a person decided about them.

A skill changes behaviour, so no turn writes one; `propose_skill` files a proposal here instead.
A row is never a skill: nothing reads it as judgment, and only a person calling a route turns it
into one.

Rules:

- Two backends chosen by `session_store`, like `plan_approvals`: the record must not outlive the
  context it authorizes.
- The key is the content, per actor: re-proposing identical text is the same proposal, so a
  rejection stays standing; a changed body is a new row and marks the open one `superseded`.
- A decision is final: `decide` moves `open` to `accepted` or `rejected` only, so a rejection
  remains findable. A person who changes their mind uses `POST /skills/mine`.
"""

from __future__ import annotations

from collections.abc import Collection
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import record_metric


class ProposalStoreError(ChemclawError):
    """The proposal store could not answer — a fault, never a refusal."""


#: What a proposal proposes. Constrained because the accepting code dispatches on it, and an
#: unknown kind is a proposal nobody can act on — the database says the same thing in a CHECK.
ProposalKind = Literal["skill", "profile"]

# Where a proposal can be in its life. `superseded` is not a decision (no human decided it), which
# keeps `decided_at` meaningful.
ProposalState = Literal["open", "accepted", "rejected", "superseded"]

#: The states a person's decision produces, as opposed to the two the system produces.
DECIDED: frozenset[str] = frozenset({"accepted", "rejected"})


def _book(kind: str, outcome: str) -> None:
    """Count one outcome of this queue.

    Booked in the store because only the store knows which outcome happened; callers would count
    intent and overstate the queue's activity.
    """
    record_metric(
        lambda m: m.increment(
            "chemclaw_behaviour_proposals_total", labels={"kind": kind, "outcome": outcome}
        )
    )


def _arrival(inserted: bool, stored: Proposal, *, revived: bool) -> str:
    """Classify what a `propose` call actually was.

    `fresh` is a new row; `already_open` met its own open row (the model repeating itself);
    `already_decided` met a decided row (the idempotent path); `revived` put a superseded body back
    in the queue, a real state change.
    """
    if inserted:
        return "proposed"
    if revived:
        return "revived"
    return "already_decided" if stored.decided else "already_open"


def content_hash(content: str) -> str:
    """The identity of one proposed document, via the repository-wide `stable_hash`."""
    return stable_hash(content)


@dataclass(frozen=True)
class Proposal:
    """One proposed change to what the agent does, as the store answers it.

    Frozen: the decision changes in the store, not in a caller's copy.
    """

    kind: ProposalKind
    name: str
    content_hash: str
    content: str
    rationale: str
    actor: str
    session_id: str
    correlation_id: str
    state: ProposalState = "open"
    # Defaulted because the store stamps it; a caller-supplied time would be overwritten.
    proposed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    decided_by: str = ""
    reason: str = ""

    @property
    def decided(self) -> bool:
        """Has a person decided this one? `superseded` is not a decision — see `ProposalState`."""
        return self.state in DECIDED


@runtime_checkable
class ProposalStore(Protocol):
    """Reads and writes proposed behaviour changes, whichever backend holds them."""

    async def propose(self, proposal: Proposal) -> Proposal:
        """Record a proposal, or return the standing one for this exact content.

        Idempotent on content, which is what makes an unchanged re-proposal unable to reopen a
        rejection. A *changed* body for a name that already has an open proposal supersedes it.
        """
        ...

    async def decide(
        self,
        actor: str,
        kind: ProposalKind,
        name: str,
        digest: str,
        *,
        accepted: bool,
        decided_by: str,
        reason: str,
    ) -> Proposal | None:
        """Record a person's decision, or return the standing one if there already is one."""
        ...

    async def one(self, actor: str, kind: ProposalKind, name: str, digest: str) -> Proposal | None:
        """One proposal by its identity, or None."""
        ...

    async def list_for(self, actor: str, *, states: Collection[str] = ()) -> list[Proposal]:
        """This person's newest `agent_proposals_list_max` proposals, narrowed by `states`."""
        ...


_COLUMNS = (
    "kind, name, content_hash, content, rationale, actor, session_id, correlation_id, "
    "state, proposed_at, decided_at, decided_by, reason"
)

_INSERT = (
    f"INSERT INTO behaviour_proposals ({_COLUMNS}) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), NULL, '', '') "
    "ON CONFLICT (actor, kind, name, content_hash) DO NOTHING"
)

# Everything this person still has open under one name, other than the version just proposed;
# the `<> %s` stops a re-proposal of the same content superseding itself.
_SUPERSEDE = (
    "UPDATE behaviour_proposals SET state = 'superseded' "
    "WHERE actor = %s AND kind = %s AND name = %s AND content_hash <> %s AND state = 'open'"
)

# Put a superseded body back in the queue, which is what re-proposing it means. The unique key
# means that row is the only one the body can have, so without this the proposal would be
# reported waiting while listed nowhere. `proposed_at` is refreshed because the ask is now; the
# decision columns stay untouched since the row was never decided.
_REVIVE = (
    "UPDATE behaviour_proposals SET state = 'open', proposed_at = now() "
    "WHERE actor = %s AND kind = %s AND name = %s AND content_hash = %s AND state = 'superseded'"
)

# Serializes one name's queue: without a transaction-scoped advisory lock, concurrent proposes of
# different bodies each find nothing to supersede and leave several open rows. Per name, so
# unrelated proposals never contend.
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"

_ONE = (
    f"SELECT {_COLUMNS} FROM behaviour_proposals "
    "WHERE actor = %s AND kind = %s AND name = %s AND content_hash = %s"
)

# Only an `open` row moves. A second decision on a decided proposal changes nothing and the caller
# is handed what already stands — see this module's header for why a decision is final.
_DECIDE = (
    "UPDATE behaviour_proposals "
    "SET state = %s, decided_at = now(), decided_by = %s, reason = %s "
    "WHERE actor = %s AND kind = %s AND name = %s AND content_hash = %s AND state = 'open'"
)

_LIST = (
    f"SELECT {_COLUMNS} FROM behaviour_proposals "
    "WHERE actor = %s AND (%s::text[] = '{}' OR state = ANY(%s)) "
    "ORDER BY proposed_at DESC, id DESC LIMIT %s"
)


def _row(row: tuple[object, ...]) -> Proposal:
    """One database row as a `Proposal` — the single place the column order is read."""
    return Proposal(
        kind=str(row[0]),  # type: ignore[arg-type]
        name=str(row[1]),
        content_hash=str(row[2]),
        content=str(row[3]),
        rationale=str(row[4]),
        actor=str(row[5]),
        session_id=str(row[6]),
        correlation_id=str(row[7]),
        state=str(row[8]),  # type: ignore[arg-type]
        proposed_at=row[9],  # type: ignore[arg-type]
        decided_at=row[10],  # type: ignore[arg-type]
        decided_by=str(row[11]),
        reason=str(row[12]),
    )


class PostgresProposalStore:
    """The durable backend, on the session-store database."""

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `chemclaw.agent.session_store`)."""
        return _session_connection(self._dsn)

    async def propose(self, proposal: Proposal) -> Proposal:
        """Record a proposal, or return the standing one for this exact content.

        Invariant: at most one open row per name. All writes run in one transaction under the
        advisory
        lock, insert-or-revive then supersede, and only a version that arrived sweeps its siblings.
        `ON CONFLICT DO NOTHING` keeps a decided row untouched; `_REVIVE` reopens only a superseded
        one.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    # `\x1f` (unit separator) rather than `\x00`: Postgres text may not carry a
                    # NUL byte at all, so the obvious separator is a `DataError` on the first call.
                    _LOCK,
                    (f"{proposal.actor}\x1f{proposal.kind}\x1f{proposal.name}",),
                )
                await cur.execute(
                    _INSERT,
                    (
                        proposal.kind,
                        proposal.name,
                        proposal.content_hash,
                        proposal.content,
                        proposal.rationale,
                        proposal.actor,
                        proposal.session_id,
                        proposal.correlation_id,
                        "open",
                    ),
                )
                inserted = cur.rowcount == 1
                # A body already stored under this name is put back in the queue rather than left
                # where a decision cannot reach it. Only ever one row, by the unique key.
                revived = False
                if not inserted:
                    await cur.execute(
                        _REVIVE,
                        (proposal.actor, proposal.kind, proposal.name, proposal.content_hash),
                    )
                    revived = cur.rowcount == 1
                # Only a version that arrived (inserted or revived) supersedes its siblings;
                # otherwise a repeat
                # would close the open sibling and leave nothing to decide.
                superseded = 0
                if inserted or revived:
                    await cur.execute(
                        _SUPERSEDE,
                        (proposal.actor, proposal.kind, proposal.name, proposal.content_hash),
                    )
                    superseded = max(cur.rowcount, 0)
                await cur.execute(
                    _ONE,
                    (proposal.actor, proposal.kind, proposal.name, proposal.content_hash),
                )
                row = await cur.fetchone()
            await conn.commit()
        if row is None:  # pragma: no cover - the insert-or-conflict above guarantees a row
            raise ProposalStoreError(
                f"{proposal.kind} proposal {proposal.name!r} was neither inserted nor already "
                "present, which the insert-or-conflict above makes unreachable"
            )
        stored = _row(row)
        for _ in range(superseded):
            _book(stored.kind, "superseded")
        _book(stored.kind, _arrival(inserted, stored, revived=revived))
        return stored

    async def decide(
        self,
        actor: str,
        kind: ProposalKind,
        name: str,
        digest: str,
        *,
        accepted: bool,
        decided_by: str,
        reason: str,
    ) -> Proposal | None:
        """Record a person's decision, or return the standing one if there already is one."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _DECIDE,
                    (
                        "accepted" if accepted else "rejected",
                        decided_by,
                        reason,
                        actor,
                        kind,
                        name,
                        digest,
                    ),
                )
                await cur.execute(_ONE, (actor, kind, name, digest))
                row = await cur.fetchone()
            await conn.commit()
        if row is None:
            return None
        decided = _row(row)
        _book(decided.kind, decided.state)
        return decided

    async def one(self, actor: str, kind: ProposalKind, name: str, digest: str) -> Proposal | None:
        """One proposal by its identity, or None."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_ONE, (actor, kind, name, digest))
                row = await cur.fetchone()
        return _row(row) if row is not None else None

    async def list_for(self, actor: str, *, states: Collection[str] = ()) -> list[Proposal]:
        """This person's newest `agent_proposals_list_max` proposals, narrowed by `states`."""
        wanted = sorted(states)
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LIST, (actor, wanted, wanted, settings.agent_proposals_list_max))
                rows = await cur.fetchall()
        return [_row(row) for row in rows]


@dataclass
class _Held:
    """One proposal in the in-process backend — mutable where the store's row is UPDATE-able."""

    proposal: Proposal


class InMemoryProposalStore:
    """The same contract for a deployment whose sessions are in-process too.

    Not a test double: it is the backend `session_store="memory"` (e.g. the CLI) uses, so it
    reproduces the Postgres rules exactly. Unbounded, since proposals arrive at human pace.
    """

    def __init__(self) -> None:
        """Start with nothing proposed."""
        self._held: dict[tuple[str, str, str, str], _Held] = {}

    def _key(self, actor: str, kind: str, name: str, digest: str) -> tuple[str, str, str, str]:
        """The identity a proposal is stored under — the in-memory spelling of the UNIQUE key."""
        return (actor, kind, name, digest)

    async def propose(self, proposal: Proposal) -> Proposal:
        """Record a proposal, or return the standing one for this exact content.

        Mirrors `PostgresProposalStore.propose` step for step, including the revive.
        """
        key = self._key(proposal.actor, proposal.kind, proposal.name, proposal.content_hash)
        standing = self._held.get(key)
        if standing is not None:
            # `_REVIVE`'s `AND state = 'superseded'`, spelled in Python.
            revived = standing.proposal.state == "superseded"
            if revived:
                standing.proposal = replace(
                    standing.proposal, state="open", proposed_at=datetime.now(UTC)
                )
                self._supersede_open_siblings(proposal)
            _book(proposal.kind, _arrival(False, standing.proposal, revived=revived))
            return standing.proposal
        self._supersede_open_siblings(proposal)
        fresh = replace(proposal, state="open", proposed_at=datetime.now(UTC))
        self._held[key] = _Held(fresh)
        _book(fresh.kind, _arrival(True, fresh, revived=False))
        return fresh

    def _supersede_open_siblings(self, proposal: Proposal) -> None:
        """Close every *other* open version of this name; the in-memory `_SUPERSEDE`."""
        for held in self._held.values():
            other = held.proposal
            if (
                other.actor == proposal.actor
                and other.kind == proposal.kind
                and other.name == proposal.name
                and other.content_hash != proposal.content_hash
                and other.state == "open"
            ):
                held.proposal = replace(other, state="superseded")
                _book(other.kind, "superseded")

    async def decide(
        self,
        actor: str,
        kind: ProposalKind,
        name: str,
        digest: str,
        *,
        accepted: bool,
        decided_by: str,
        reason: str,
    ) -> Proposal | None:
        """Record a person's decision, or return the standing one if there already is one."""
        held = self._held.get(self._key(actor, kind, name, digest))
        if held is None:
            return None
        if held.proposal.state == "open":
            held.proposal = replace(
                held.proposal,
                state="accepted" if accepted else "rejected",
                decided_at=datetime.now(UTC),
                decided_by=decided_by,
                reason=reason,
            )
        _book(held.proposal.kind, held.proposal.state)
        return held.proposal

    async def one(self, actor: str, kind: ProposalKind, name: str, digest: str) -> Proposal | None:
        """One proposal by its identity, or None."""
        held = self._held.get(self._key(actor, kind, name, digest))
        return held.proposal if held is not None else None

    async def list_for(self, actor: str, *, states: Collection[str] = ()) -> list[Proposal]:
        """This person's newest `agent_proposals_list_max` proposals, narrowed by `states`."""
        wanted = frozenset(states)
        mine = [
            held.proposal
            for held in self._held.values()
            if held.proposal.actor == actor and (not wanted or held.proposal.state in wanted)
        ]
        # Reverse first so the stable descending sort breaks timestamp ties newest-first, matching
        # `ORDER BY proposed_at DESC, id DESC`.
        mine.reverse()
        mine.sort(key=lambda proposal: proposal.proposed_at, reverse=True)
        return mine[: settings.agent_proposals_list_max]


# The one in-process store: a per-call instance would lose proposals between turn and route.
_IN_MEMORY = InMemoryProposalStore()


def default_proposal_store() -> ProposalStore:
    """This deployment's proposal store; the backend follows `session_store`."""
    if settings.session_store == "postgres":
        return PostgresProposalStore()
    return _IN_MEMORY
