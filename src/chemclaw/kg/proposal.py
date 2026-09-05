"""The durable record of a note proposed to the PR-gate, and of what a human decided.

Why this exists: the PR-gate is the control every other control in this system is justified by —
"the agent proposes, a human decides" (D-005) — and until now it terminated in a branch push.
Nothing listed what was awaiting review, nothing told the chemist who proposed a note what became of
it, and a rejection left no trace whatsoever, because a rejection is a deleted branch. A gate whose
outcomes are invisible is a gate nobody operates.

This module is the dependency-free half: the models, the two backends' contract, the in-memory
backend and the facade `chemclaw.kg.pr_gate` calls. The psycopg backend lives in
`chemclaw.kg.proposal_store`, imported lazily, exactly as `chemclaw.durable.job_record` is split
from `job_record_store` — a deployment (or a test, or a connector worker) running without Postgres
must never pull a database driver for a store it will not use.

**Submissions are recorded best-effort; decisions are not.** A proposal record is a compliance
nicety compared to the note actually reaching the branch, so a database hiccup must not turn a
successful submission into a failed tool call — the same trade `chemclaw.agent.audit` makes. A
*decision* is the opposite: a reviewer told their rejection was stored when it was not is the one
failure mode a review surface cannot tolerate, so that path propagates.
"""

import logging
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.session_context import get_current_session_id
from chemclaw.kg.submission import NoteFile

logger = logging.getLogger(__name__)


class ProposalState(StrEnum):
    """Where a proposal stands. Mirrors the `note_proposals_state_known` CHECK constraint.

    `FAILED` is deliberately not a decision: it means the submission never reached git, so there is
    nothing a human could have decided about. Keeping it in the same column rather than a separate
    flag is what makes "everything the gate has ever been asked to do" one query.
    """

    OPEN = "open"
    MERGED = "merged"
    REJECTED = "rejected"
    FAILED = "failed"
    # A newer version of the same note replaced this one in the queue. Not a decision — no human
    # decided anything about the old bytes — and not a failure: the branch is per-note while the
    # record is per-version, so without this state a changed re-proposal left the old version
    # `open`, rendering bytes that existed on no branch, and the merge webhook then marked both
    # rows merged (migration 058).
    SUPERSEDED = "superseded"


DECIDED_STATES = frozenset({ProposalState.MERGED, ProposalState.REJECTED})

# The states a re-proposal of byte-identical content may move a row *out of*: the two that record
# something other than a human's judgement. Named beside `DECIDED_STATES` rather than spelled as a
# tuple at its one use, because the rule is the enum's and not the store's — the Postgres backend
# states the identical set as `IN ('failed', 'superseded')` in `proposal_store._UPSERT`, and a
# future state has to be sorted into one of these two lists in both places or the backends diverge.
_REOPENABLE_STATES = frozenset({ProposalState.FAILED, ProposalState.SUPERSEDED})


class NoteProposal(BaseModel):
    """One submission of one note version, with its provenance and its outcome.

    `content` is the rendered subject note and `dependencies` the supporting files that would land
    beside it, both kept verbatim. That is what separates this from a counter: a `FAILED` row can
    be replayed because the bytes it would have written are still here, and a reviewer opening one
    proposal sees what would land rather than a summary of it.

    **`dependencies` exists because `content` alone made both of those sentences false for a
    multi-file submission.** The row kept `files[0]` and dropped the rest, so replaying a
    `job-result` proposal would have written a note whose `[[wikilink]]` to its `compound` dangled
    — the exact failure the multi-file `NoteSubmission` was introduced to prevent (D-133) — and a
    reviewer was shown one file of a unit that is defined as indivisible. Four of the nine
    `propose_note` call sites pass dependencies.

    **The identity stays the subject note's bytes.** `content_hash` covers `content` and not the
    dependencies, deliberately: the row is a record of *this version of this note*, and folding the
    supporting files into the hash would make every pre-existing row look like changed content and
    append a second "the note changed" row to a compliance table where that is a claim about
    history. `dependencies` is refreshed on an unchanged re-proposal exactly as `reference` and the
    provenance columns already are.

    `submitted_at` is unset on the way in and filled by the store, for the reason
    `JobRecord.completed_at` is: a caller may be a Temporal activity, and the row's timestamp
    should come from the same clock that orders the rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    note_id: str = Field(min_length=1)
    note_type: str = Field(min_length=1)
    content: str
    dependencies: tuple[NoteFile, ...] = ()
    branch: str = Field(min_length=1)
    reference: str = ""
    actor: str = ""
    session_id: str = ""
    correlation_id: str = ""
    state: ProposalState = ProposalState.OPEN
    reason: str = ""
    # Assigned by the store on insert; present when a proposal is read back.
    id: int = 0
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str = ""

    @property
    def content_hash(self) -> str:
        """The identity of this *version* of the note — the key a re-proposal collapses onto.

        Derived rather than stored so it cannot drift from the content it names. Re-proposing a
        byte-identical note touches the existing row (matching `GitNoteSubmitter`, which pushes
        nothing when there is no diff); a changed body appends a new row and leaves any earlier
        decision standing, because overwriting a rejection with a fresh `open` row would erase the
        one thing this record exists to keep. Collapsing onto one row is also what lets the retry
        of a `FAILED` submission correct it rather than leave a permanently false record — see
        `InMemoryProposalStore.upsert`.
        """
        return stable_hash(self.content)


@runtime_checkable
class ProposalStore(Protocol):
    """Reads and writes the PR-gate's record, whichever backend holds it."""

    async def upsert(self, proposal: NoteProposal) -> int:
        """Store the proposal (refreshing an unchanged re-proposal); return its id."""
        ...

    async def read(self, proposal_id: int) -> NoteProposal | None:
        """One proposal in full, or None when there is no such row."""
        ...

    async def listing(
        self, state: ProposalState | None, actor: str, limit: int, before_id: int | None
    ) -> list[NoteProposal]:
        """Proposals newest-first, filtered by state and proposer, paged by `before_id`."""
        ...

    async def decide(
        self, proposal_id: int, state: ProposalState, decided_by: str, reason: str
    ) -> NoteProposal | None:
        """Record a decision on an *open* proposal; None when absent or already decided."""
        ...

    async def mark_merged(self, note_ids: list[str], decided_by: str) -> int:
        """Close every open proposal for the named notes as merged; return how many moved."""
        ...

    async def history(self, note_id: str, actor: str) -> list[NoteProposal]:
        """Every recorded version of one note, oldest first, optionally scoped to one proposer.

        What a reviewer needs and could not get: this note has been here before, and somebody
        already decided something about it. `decided_version` answers the *exact bytes* question
        and is asked by the gate; this answers the reviewer's question, which is about the note.
        """
        ...

    async def decided_version(self, note_id: str, content_hash: str) -> NoteProposal | None:
        """The *decided* row for exactly these bytes, or None when this version is undecided.

        Asked **before** a submission reaches git, which is the only moment it can prevent
        anything: `upsert` already refuses to reopen a rejection, but it runs after the push, so a
        re-proposed rejection left a live, mergeable branch that no longer appeared in any queue.
        """
        ...


class InMemoryProposalStore:
    """The same contract for a deployment whose durable records live in-process.

    It is *not* a test double. It is the backend a `session_store="memory"` deployment gets — the
    CLI is a real one of those — so the review queue exists there rather than silently reporting an
    empty gate, with precisely the lifetime of the process whose proposals it holds.

    Every rule its Postgres sibling enforces in SQL is enforced here in the same terms, because a
    backend that agrees on the happy path and diverges on the contended one is worse than no second
    backend: keyed on `(note_id, content_hash)` so a re-proposal collapses, a *decision* untouched
    on that collapse so a rejection does not silently reopen, a `FAILED` row superseded by the
    retry that succeeded, and decisions confined to open rows so a second reviewer cannot overwrite
    the first.
    """

    def __init__(self) -> None:
        """Start with no proposals recorded."""
        self._by_id: dict[int, NoteProposal] = {}
        self._by_version: dict[tuple[str, str], int] = {}
        self._next_id = 1

    async def upsert(self, proposal: NoteProposal) -> int:
        """Store the proposal, refreshing the row when this exact content was proposed before."""
        version = (proposal.note_id, proposal.content_hash)
        now = datetime.now(UTC)
        existing_id = self._by_version.get(version)
        if existing_id is not None:
            existing = self._by_id[existing_id]
            update: dict[str, object] = {
                "reference": proposal.reference,
                "actor": proposal.actor,
                "session_id": proposal.session_id,
                "correlation_id": proposal.correlation_id,
                # Refreshed like the provenance above rather than keyed on: the dependency set is
                # derived from the subject note, so an unchanged note re-proposed with a different
                # set means the derivation changed, and the row should describe the submission that
                # will actually be replayed.
                "dependencies": proposal.dependencies,
                "submitted_at": now,
            }
            if existing.state in _REOPENABLE_STATES:
                # The state transitions a re-proposal may make, and the Postgres `CASE` in
                # `proposal_store._UPSERT` is their mirror. Neither is a decision (see
                # `ProposalState`), and both leave the record asserting the opposite of what
                # happened if the row is not moved.
                #
                # `FAILED` says the submission never reached git, and the retry that finally lands
                # carries byte-identical content, so it collapses onto this row. `SUPERSEDED` says
                # a newer version took the queue slot — so an agent that regenerates an earlier
                # form re-proposes bytes that the branch, which is per-note, now really holds.
                # Leaving either one closed made the branch await a review the queue never showed,
                # the decision route answer 409, and `mark_merged` move nothing.
                #
                # A *decision* still stands: a rejected note re-proposed unchanged must not
                # silently reopen, or the gate is defeated by re-asking.
                update["state"] = proposal.state
                update["reason"] = proposal.reason
            refreshed = existing.model_copy(update=update)
            self._by_id[existing_id] = refreshed
            self._supersede_other_open(refreshed, keep_id=existing_id)
            return existing_id
        new_id = self._next_id
        self._next_id += 1
        recorded = proposal.model_copy(update={"id": new_id, "submitted_at": now})
        self._by_id[new_id] = recorded
        self._by_version[version] = new_id
        self._supersede_other_open(recorded, keep_id=new_id)
        return new_id

    def _supersede_other_open(self, recorded: NoteProposal, *, keep_id: int) -> None:
        """Close the note's other open versions once `recorded` is the open one.

        Exactly one row per note may be `open`, because the branch the reviewer merges is per-note.
        The Postgres store runs the same rule as a statement
        (`proposal_store._SUPERSEDE_OTHER_OPEN`), down to the reason it writes: "another", not "a
        newer" — a re-proposal of an older version reopens its row and closes the *newer* one, so
        the sentence a reviewer reads must describe the rule rather than the first caller of it.

        **The trigger is the state the row now has, not the state the caller asked for**, which is
        the difference between the two backends agreeing and only appearing to. A `failed` record
        must not push a reviewable version out of the queue — and neither must a re-proposal of a
        *rejected* version's bytes, which arrives asking for `open`, is correctly refused above,
        and would otherwise close the live version anyway, leaving the note with nothing awaiting
        review. Reading the row makes both cases one condition.
        """
        if recorded.state is not ProposalState.OPEN:
            return
        for other_id, other in self._by_id.items():
            if (
                other_id != keep_id
                and other.note_id == recorded.note_id
                and other.state is ProposalState.OPEN
            ):
                self._by_id[other_id] = other.model_copy(
                    update={
                        "state": ProposalState.SUPERSEDED,
                        "reason": "superseded by another proposed version of this note",
                    }
                )

    async def read(self, proposal_id: int) -> NoteProposal | None:
        """One proposal in full, or None when there is no such row."""
        return self._by_id.get(proposal_id)

    async def listing(
        self, state: ProposalState | None, actor: str, limit: int, before_id: int | None
    ) -> list[NoteProposal]:
        """Proposals newest-first, filtered by state and proposer, paged by `before_id`."""
        matches = [
            proposal
            for proposal in self._by_id.values()
            if (state is None or proposal.state is state)
            and (not actor or proposal.actor == actor)
            and (not before_id or proposal.id < before_id)
        ]
        matches.sort(key=lambda proposal: proposal.id, reverse=True)
        return matches[:limit]

    async def decide(
        self, proposal_id: int, state: ProposalState, decided_by: str, reason: str
    ) -> NoteProposal | None:
        """Record a decision on an open proposal; None when absent or already decided."""
        existing = self._by_id.get(proposal_id)
        if existing is None or existing.state is not ProposalState.OPEN:
            return None
        decided = existing.model_copy(
            update={
                "state": state,
                "decided_at": datetime.now(UTC),
                "decided_by": decided_by,
                "reason": reason,
            }
        )
        self._by_id[proposal_id] = decided
        return decided

    async def mark_merged(self, note_ids: list[str], decided_by: str) -> int:
        """Close every open proposal for the named notes as merged; return how many moved."""
        wanted = set(note_ids)
        moved = 0
        for proposal_id, proposal in list(self._by_id.items()):
            if proposal.note_id not in wanted or proposal.state is not ProposalState.OPEN:
                continue
            self._by_id[proposal_id] = proposal.model_copy(
                update={
                    "state": ProposalState.MERGED,
                    "decided_at": datetime.now(UTC),
                    "decided_by": decided_by,
                }
            )
            moved += 1
        return moved

    async def history(self, note_id: str, actor: str) -> list[NoteProposal]:
        """Every version of one note, oldest first — id order, which is submission order here."""
        versions = [
            proposal
            for proposal in self._by_id.values()
            if proposal.note_id == note_id and (not actor or proposal.actor == actor)
        ]
        versions.sort(key=lambda proposal: proposal.id)
        return versions

    async def decided_version(self, note_id: str, content_hash: str) -> NoteProposal | None:
        """The decided row for these exact bytes, keyed the same way `upsert` keys them."""
        proposal_id = self._by_version.get((note_id, content_hash))
        if proposal_id is None:
            return None
        proposal = self._by_id[proposal_id]
        return proposal if proposal.state in DECIDED_STATES else None


@cache
def proposal_store() -> ProposalStore:
    """The proposal store this deployment gets: durable where its other records are.

    One instance per process, because the writer and the readers must see the same rows: the
    PR-gate writes, and `GET /proposals` reads. Under Postgres that holds anyway; under the
    in-memory backend a second instance would be a second, empty store and the review queue would
    always be empty — the failure that made this whole area invisible in the first place.

    Gated on `session_store` for the reason `plan_approval_store` and `default_audit_sink` are:
    that switch is a deployment's statement that a Postgres exists and durable records belong in
    it.
    """
    if settings.session_store == "postgres":
        from chemclaw.kg.proposal_store import PostgresProposalStore

        return PostgresProposalStore()
    return InMemoryProposalStore()


async def rejected_version(note_id: str, content_hash: str) -> NoteProposal | None:
    """The rejection standing against exactly these bytes, if there is one. Never raises.

    Read by the gate before it submits. A store that cannot answer must not block a proposal — the
    record exists to describe the gate, not to become a second way for it to fail — so an
    unreachable database degrades to the behaviour that shipped before this check existed.
    """
    try:
        decided = await proposal_store().decided_version(note_id, content_hash)
    except Exception:
        logger.warning("could not check for a standing rejection of %s; proceeding", note_id)
        return None
    return decided if decided is not None and decided.state is ProposalState.REJECTED else None


async def record_proposal_submitted(proposal: NoteProposal) -> None:
    """Record a submission (or refresh the row for an unchanged re-proposal). Never raises."""
    await _write(proposal, "submitted")


async def record_proposal_failed(proposal: NoteProposal) -> None:
    """Record a submission that never reached git, keeping its content for replay. Never raises."""
    await _write(proposal.model_copy(update={"state": ProposalState.FAILED}), "failure")


async def _write(proposal: NoteProposal, what: str) -> None:
    """Persist one proposal, swallowing every failure into a log line.

    The swallow is the point (see the module docstring): by the time this runs the note has already
    reached the branch — or already failed to — and letting a database blip fail the tool call
    would trade the thing that matters for the record of it.
    """
    try:
        await proposal_store().upsert(proposal)
    except Exception:
        logger.warning("could not record note proposal %s (%s)", proposal.note_id, what)
        return
    _count(proposal.state)


def _count(state: ProposalState) -> None:
    """Count one proposal reaching `state` — the series an operator watches the gate through."""
    record_metric(
        lambda m: m.increment("chemclaw_note_proposals_total", labels={"state": state.value})
    )


def ambient_provenance() -> tuple[str, str, str]:
    """The turn's `(actor, session_id, correlation_id)`, empty off the request path.

    Read ambiently rather than threaded through `propose_note`'s eight call sites, and read from
    the same carriers `chemclaw.agent.audit` reads so a proposal and the tool call that produced it
    carry identical keys — which is the entire point of recording them.
    """
    return (
        get_current_actor() or "",
        get_current_session_id() or "",
        get_current_correlation_id() or "",
    )


async def list_proposals(
    state: ProposalState | None, actor: str, limit: int, before_id: int | None = None
) -> list[NoteProposal]:
    """Proposals newest-first, optionally filtered by state and by who proposed them.

    An empty `actor` means "every proposer", which is the reviewer's view; a non-empty one scopes
    to one chemist's own submissions.
    """
    return await proposal_store().listing(state, actor, limit, before_id)


async def read_proposal(proposal_id: int) -> NoteProposal | None:
    """One proposal in full, or None when there is no such row."""
    return await proposal_store().read(proposal_id)


async def proposal_history(note_id: str, actor: str) -> list[NoteProposal]:
    """Every recorded version of one note, oldest first, optionally scoped to one proposer.

    **What the gate already prevented, and what it did not.** `rejected_version` refuses a
    re-proposal of byte-identical rejected content before it reaches git, so the exact repeat is
    closed. A *changed* re-proposal of a note somebody already rejected is a different version, so
    it arrives in the queue legitimately — and it arrived with the earlier rejection and its stated
    reason nowhere in sight, leaving the next reviewer to re-derive the judgement or to merge what
    a colleague had refused. The record was always there (`reason` is written on every decision, and
    `durable/retention.py` refuses to prune this table); nothing read it back.

    `actor` is the visibility rule, not a filter for convenience: a non-reviewer may see their own
    submissions, so a history assembled for them must not disclose that somebody else proposed the
    same note. Empty means "every proposer", which is the reviewer's view.
    """
    return await proposal_store().history(note_id, actor)


async def decide_proposal(
    proposal_id: int, state: ProposalState, decided_by: str, reason: str
) -> NoteProposal | None:
    """Record a human decision on one open proposal; return the updated row, or None if absent.

    Only an `OPEN` row moves: a decision already taken is evidence, and letting a second call
    overwrite it would make the record answer "who decided" with whoever spoke last.
    """
    if state not in DECIDED_STATES:
        raise ValueError(f"a human decision is 'merged' or 'rejected', not {state.value!r}")
    decided = await proposal_store().decide(proposal_id, state, decided_by, reason)
    if decided is not None:
        _count(state)
    return decided


async def close_merged_notes(note_ids: list[str], decided_by: str) -> int:
    """Mark every open proposal for the named notes merged; return how many moved.

    The merge webhook's half of the loop. It names *notes*, not proposal ids, because that is what
    a git host knows about a merged branch — and it moves only open rows, so a duplicate delivery
    (which webhooks routinely are) is a no-op rather than a second decision.
    """
    if not note_ids:
        return 0
    moved = await proposal_store().mark_merged(note_ids, decided_by)
    for _ in range(moved):
        _count(ProposalState.MERGED)
    return moved
