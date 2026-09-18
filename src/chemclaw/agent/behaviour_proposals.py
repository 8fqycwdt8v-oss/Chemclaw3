"""Proposed changes to what the agent *does*, and what a person decided about them.

`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` drew the axis this module sits on: a thing
is gated when it changes what the agent does. Knowledge does not, so it lands in the graph and is
corrected. A **skill** does — it is injected into the prompt and reshapes every later answer with no
citation trail — which is why `agent/skill_backend.SkillsReadOnlyRefusal` refuses every write a turn
could attempt, on the shared tree and on the chemist's own tier alike.

That refusal left one thing unanswered, and this module is the answer. The agent often *is* the
party that has just worked out a procedure worth keeping, and until now the only thing it could do
was write the text into an answer and hope somebody copied it into
`POST /skills/mine`. `propose_skill` lets it put one here instead. **A row here is a proposal and
never a skill**: nothing reads it as judgment, no prompt contains it, and the only thing that turns
one into behaviour is a person calling a route. The refusal is untouched, because this is a
different table.

**Modelled on two predecessors, because the halves have different shapes and each has a table that
already got its half right.**

From `plan_approvals`: two backends chosen by `session_store`, for the reason
`agent/plan_approval_store.py` gives at length — a record must not outlive, or be outlived by, the
thing it authorizes, and under `session_store="memory"` the thing it authorizes is a process.

From retired `note_proposals`, including the defect its own successor migration had to fix: **the
key is the content, not the name.** Re-proposing byte-identical text is the same proposal, so a
rejection in July survives the same text arriving again in August — which is the whole of "an
unchanged re-proposal cannot reopen a rejection". A *changed* body is a different proposal and
appends a new row, and the earlier one is marked `superseded` rather than left `open`: migration 058
records what happens otherwise, a queue rendering versions nothing would deliver and one decision
later applied to both.

**A decision is final, and that is a decision rather than an omission.** `decide` transitions `open`
to `accepted` or `rejected` and nothing else moves; a second call reports the standing decision
instead of replacing it. A person who rejected something and later wants it has a shorter path than
reopening a queue entry — `POST /skills/mine` writes the skill directly, which is the same act with
one fewer indirection and no pretence that the agent proposed it twice. The alternative, letting a
decision be overwritten, buys a change of mind at the cost of the property this table exists for:
that a rejection is evidence somebody can find later.

**Content identity is per actor.** Two chemists may independently be offered the same procedure, and
one rejecting it must not decide for the other — a proposal is per person, like the tier an accepted
one is written to.
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
    """The proposal store could not answer — a fault, never a refusal.

    Separate from a refusal because the two read differently to everything downstream: a refusal is
    an answer (`decide` on a decided proposal), and this is the store failing to do arithmetic it
    guaranteed.
    """


#: What a proposal proposes. Constrained because the accepting code dispatches on it, and an
#: unknown kind is a proposal nobody can act on — the database says the same thing in a CHECK.
ProposalKind = Literal["skill", "profile"]

#: Where a proposal can be in its life. `superseded` is deliberately not a decision: a newer version
#: of the same name replaced it in the queue and no human decided anything about it, which is what
#: keeps `decided_at` meaning what an auditor reads it as.
ProposalState = Literal["open", "accepted", "rejected", "superseded"]

#: The states a person's decision produces, as opposed to the two the system produces.
DECIDED: frozenset[str] = frozenset({"accepted", "rejected"})


def _book(kind: str, outcome: str) -> None:
    """Count one outcome of this queue.

    **Booked in the store rather than in either caller, because only the store knows which of the
    four happened.** A `propose` that met its own content is not a fresh proposal, and a `decide` on
    a decided row is not a decision — a caller counting its own intent would report a queue busier
    than it is, in the one series whose purpose is telling an operator whether anybody is reading
    it.
    """
    record_metric(
        lambda m: m.increment(
            "chemclaw_behaviour_proposals_total", labels={"kind": kind, "outcome": outcome}
        )
    )


def _arrival(inserted: bool, stored: Proposal) -> str:
    """What a `propose` call actually was, which is not the same as what its caller intended.

    Three outcomes, because a queue's usefulness is measured by the gap between them. A **fresh**
    row is a proposal. A call that met its own content on an **open** row proposed nothing — the
    model is repeating itself, which is worth seeing and is not a second proposal. A call that met a
    **decided** row is the idempotent path this table exists for: the same text cannot reopen a
    rejection, and counting it as a proposal would report a queue busier than it is in the one
    series whose purpose is telling an operator whether anybody is reading it.
    """
    if inserted:
        return "proposed"
    return "already_decided" if stored.decided else "already_open"


def content_hash(content: str) -> str:
    """The identity of one proposed document.

    `stable_hash` rather than a fresh digest so this repository has one answer to "are these the
    same bytes" — the same function `local_skills_namespace` and the note index use.
    """
    return stable_hash(content)


@dataclass(frozen=True)
class Proposal:
    """One proposed change to what the agent does, as the store answers it.

    Frozen because a caller holding one is holding a record of something that happened, and the one
    field that changes after the fact — the decision — changes in the store rather than in a
    caller's copy.
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
    # Defaulted because the store stamps it: Postgres with `now()`, the in-process backend
    # with its own clock. A caller that had to supply one would be inventing a time the row
    # then overwrites, which is a second answer to "when was this proposed".
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

    async def list_for(
        self, actor: str, *, states: Collection[str] = (), limit: int = 50
    ) -> list[Proposal]:
        """This person's proposals, newest first, optionally narrowed to some states."""
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

# Everything this person still has open under one name, other than the version just proposed. The
# `<> %s` is what keeps a re-proposal of the *same* content from superseding itself, which would
# turn the idempotent path into a state change.
_SUPERSEDE = (
    "UPDATE behaviour_proposals SET state = 'superseded' "
    "WHERE actor = %s AND kind = %s AND name = %s AND content_hash <> %s AND state = 'open'"
)

# **The serializer for one name's queue.** `_SUPERSEDE` reads under READ COMMITTED before a peer's
# insert is visible, so N concurrent proposes of N different bodies each found nothing to supersede
# and left N open rows — measured at 8 concurrent, 7 open, no error and no deadlock, which is
# silently the state the module says must never exist ("a reviewer who sees both has to guess which
# one a decision applies to"). A transaction-scoped advisory lock keyed on the name is what makes
# the read-then-write a critical section; it releases on commit, and it is per name so two chemists
# and two skills never contend.
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

        **The supersede runs before the insert and both are in one transaction**, because the queue
        must never show two open versions of one name — a reviewer who sees both has to guess which
        one a decision applies to, which is the state migration 058 exists to describe. Ordering it
        the other way would supersede the row just inserted in the `content_hash <> %s` sense only
        by accident of the hash comparison; doing it first makes the intent structural.

        `ON CONFLICT DO NOTHING` is the whole of "an unchanged re-proposal cannot reopen a
        rejection": the row already there is returned untouched, whatever state it is in.
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
                # **Only a genuinely new version supersedes anything**, which the first spelling
                # got wrong by running the update unconditionally: re-proposing a body already
                # stored killed the *open* sibling and revived nothing, so a queue holding one
                # open proposal and one superseded one came back holding two superseded ones and
                # nothing to decide. Measured — `OPEN rows: []` — while `propose_skill` went on
                # telling the model its proposal was "already waiting".
                superseded = 0
                if inserted:
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
        _book(stored.kind, _arrival(inserted, stored))
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

    async def list_for(
        self, actor: str, *, states: Collection[str] = (), limit: int = 50
    ) -> list[Proposal]:
        """This person's proposals, newest first, optionally narrowed to some states."""
        wanted = sorted(states)
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LIST, (actor, wanted, wanted, limit))
                rows = await cur.fetchall()
        return [_row(row) for row in rows]


@dataclass
class _Held:
    """One proposal in the in-process backend — mutable where the store's row is UPDATE-able."""

    proposal: Proposal


class InMemoryProposalStore:
    """The same contract for a deployment whose sessions are in-process too.

    It is **not** a test double, for the reason `InMemoryPlanApprovalStore` is not: it is the
    backend a `session_store="memory"` deployment gets, and the CLI is a real one of those. A queue
    with two implementations that disagree about whether a rejection can be reopened is a control
    nobody can reason about, so the three rules are reproduced exactly — content is the key, a
    changed body supersedes an open sibling, and only an `open` proposal moves.

    Unbounded, and measured rather than defended, the same way its sibling is: a proposal is a
    deliberate act by a model that a person then reads, so the arrival rate is bounded by
    attention. The shipped chart sets `session_store="postgres"`, so a deployed fleet never reaches
    this class.
    """

    def __init__(self) -> None:
        """Start with nothing proposed."""
        self._held: dict[tuple[str, str, str, str], _Held] = {}

    def _key(self, actor: str, kind: str, name: str, digest: str) -> tuple[str, str, str, str]:
        """The identity a proposal is stored under — the in-memory spelling of the UNIQUE key."""
        return (actor, kind, name, digest)

    async def propose(self, proposal: Proposal) -> Proposal:
        """Record a proposal, or return the standing one for this exact content."""
        key = self._key(proposal.actor, proposal.kind, proposal.name, proposal.content_hash)
        standing = self._held.get(key)
        if standing is not None:
            _book(proposal.kind, _arrival(False, standing.proposal))
            return standing.proposal
        for held in self._held.values():
            other = held.proposal
            if (
                other.actor == proposal.actor
                and other.kind == proposal.kind
                and other.name == proposal.name
                and other.state == "open"
            ):
                held.proposal = replace(other, state="superseded")
                _book(other.kind, "superseded")
        fresh = replace(proposal, state="open", proposed_at=datetime.now(UTC))
        self._held[key] = _Held(fresh)
        _book(fresh.kind, _arrival(True, fresh))
        return fresh

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

    async def list_for(
        self, actor: str, *, states: Collection[str] = (), limit: int = 50
    ) -> list[Proposal]:
        """This person's proposals, newest first, optionally narrowed to some states."""
        wanted = frozenset(states)
        mine = [
            held.proposal
            for held in self._held.values()
            if held.proposal.actor == actor and (not wanted or held.proposal.state in wanted)
        ]
        # `reverse=True` on a stable sort keeps *insertion* order among equal timestamps, which is
        # oldest-first — the opposite of what this method promises and of what `ORDER BY
        # proposed_at DESC, id DESC` gives. Measured on three rows stamped identically: Postgres
        # answered newest-first and this answered oldest-first. Reversing the list first makes the
        # tiebreak arrival order descending, which is what the id does on the other backend.
        mine.reverse()
        mine.sort(key=lambda proposal: proposal.proposed_at, reverse=True)
        return mine[: max(limit, 0)]


#: The one in-process store for a `session_store="memory"` deployment.
#:
#: A module singleton for the reason `templates/composed.py` has one: the CLI is one process and one
#: person, and a per-call instance would lose every proposal between the turn that made it and the
#: route that decides it.
_IN_MEMORY = InMemoryProposalStore()


def default_proposal_store() -> ProposalStore:
    """This deployment's proposal store, chosen the way the plan-approval store chooses one.

    The backend follows `session_store` rather than being configured separately, and
    `agent/plan_approval_store.py` carries the whole argument: the record must not outlive, or be
    outlived by, the thing it authorizes. A proposal authorizes a change to what the agent does for
    one person, and under `session_store="memory"` that person's whole context is a process.
    """
    if settings.session_store == "postgres":
        return PostgresProposalStore()
    return _IN_MEMORY
