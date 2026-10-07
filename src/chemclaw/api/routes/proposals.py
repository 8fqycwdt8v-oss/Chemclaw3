"""The human gate on a proposed behaviour change: read what is waiting, accept it, or decline it.

`agent/behaviour_proposals.py` holds the queue; this is the half a person acts through. A model must
never authorize its own behaviour change: the agent proposes with `propose_skill`, and nothing it
can call decides.

Owner-scoped by construction: every handler keys the store by `principal.oid`, so there is no
authorization decision to get wrong. Accepting re-applies every admission rule `POST /skills/mine`
enforces (including `agent_local_skills_max`); a refusal leaves the proposal `open`. A decision is
final: deciding twice reports what stands.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from chemclaw.agent.behaviour_proposals import (
    DECIDED,
    Proposal,
    ProposalKind,
    default_proposal_store,
)
from chemclaw.agent.local_skills import (
    SkillRefused,
    delete_local_skill,
    read_local_skill,
    save_local_skill,
    validated_skill,
)
from chemclaw.api.deps import CurrentUser
from chemclaw.api.routes.skill_http import skill_refusal_http, store_or_503
from chemclaw.api.runner import turn_store

logger = logging.getLogger(__name__)


class ProposalOut(BaseModel):
    """One proposal as a person reads it, body included.

    The body is here and not only in a detail route, because the question this surface answers is
    "should this act on my turns", and that is not answerable from a name and a rationale. A listing
    that withheld the document would be asking for a decision about something unseen, which is the
    failure `D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` names one layer
    over.
    """

    kind: str
    name: str
    content_hash: str
    content: str
    rationale: str
    state: str
    session_id: str
    decided_by: str = ""
    reason: str = ""


class ProposalsOut(BaseModel):
    """Everything waiting on this person, newest first."""

    proposals: list[ProposalOut]


class DecisionIn(BaseModel):
    """One person's decision about one exact document.

    **`content_hash` is required and is the whole reason this is a body rather than a path.** A
    decision that named only the skill would authorize whatever that name currently holds, and the
    proposer can supersede an open proposal between the read and the click. Binding the decision to
    the hash the person was shown is the same control `plan_approvals` gets from keying on
    `plan_hash` — a changed document is a different document and is undecided until somebody
    decides it too.
    """

    content_hash: str = Field(min_length=1)
    accepted: bool
    reason: str = Field(default="", max_length=2_000)


def _rendered(proposal: Proposal) -> ProposalOut:
    """One stored proposal as the API shape — the single place the mapping is written."""
    return ProposalOut(
        kind=proposal.kind,
        name=proposal.name,
        content_hash=proposal.content_hash,
        content=proposal.content,
        rationale=proposal.rationale,
        state=proposal.state,
        session_id=proposal.session_id,
        decided_by=proposal.decided_by,
        reason=proposal.reason,
    )


async def list_proposals(principal: CurrentUser, state: str = "open") -> ProposalsOut:
    """What is waiting on this person — open by default.

    `state=""` returns every state (the audit read). Either read is bounded to the newest
    `agent_proposals_list_max` rows.
    """
    store = default_proposal_store()
    wanted = [state] if state else []
    return ProposalsOut(
        proposals=[
            _rendered(proposal) for proposal in await store.list_for(principal.oid, states=wanted)
        ]
    )


async def decide_proposal(
    kind: ProposalKind, name: str, body: DecisionIn, principal: CurrentUser
) -> ProposalOut:
    """Accept or decline one proposal, and — on an acceptance — write what it proposed.

    The write happens inside the decision: if it cannot happen, the decision is refused and the
    proposal stays open, which is recoverable; a recorded acceptance that wrote nothing is not.
    """
    store = default_proposal_store()
    standing = await store.one(principal.oid, kind, name, body.content_hash)
    if standing is None:
        raise HTTPException(
            404,
            f"you have no {kind} proposal named {name!r} with that content: it may have been "
            "superseded by a newer version, which is a different document and a different decision",
        )
    if standing.state in DECIDED:
        raise HTTPException(
            409,
            f"this proposal was already {standing.state}"
            + (f" ({standing.reason})" if standing.reason else "")
            + ". A decision is a record of something somebody did, so it is not replaced; write "
            "the skill directly through POST /skills/mine if you have changed your mind",
        )
    if standing.state == "superseded":
        raise HTTPException(
            409,
            "a newer version of this proposal replaced it, so deciding this one would decide a "
            "document nothing would deliver. Read the open one instead",
        )
    undo = await _write_what_was_accepted(standing, principal.oid) if body.accepted else None
    decided = await store.decide(
        principal.oid,
        kind,
        name,
        body.content_hash,
        accepted=body.accepted,
        decided_by=principal.oid,
        reason=body.reason,
    )
    if decided is None:  # pragma: no cover - `one` above found it a moment ago
        raise HTTPException(404, f"the {kind} proposal {name!r} disappeared while being decided")
    # The decision is conditional (`AND state = 'open'`) and the write came first, so this request
    # can lose a race to a concurrent decline or supersession; the write is then undone.
    wanted = "accepted" if body.accepted else "rejected"
    if decided.state != wanted:
        lost = f"this proposal was {decided.state} by another request while you were deciding it"
        if undo is not None:
            # A failed undo is still a lost race: answer 409, and log who, what and why so the stray
            # skill can be removed.
            try:
                await undo()
            except Exception as exc:
                logger.exception(
                    "could not undo the accepted skill %r for %s after the proposal was %s by "
                    "another request; the written body may still be live in that tier",
                    name,
                    principal.oid,
                    decided.state,
                )
                raise HTTPException(
                    409,
                    f"{lost}, and that decision stands — but removing the skill this request had "
                    "already written failed, so the accepted version may still be in your tier. "
                    f"Check it through GET /skills/mine/{name} and correct it through the skills "
                    "routes",
                ) from exc
        raise HTTPException(
            409, f"{lost}, and that decision stands; nothing you asked for was applied"
        )
    return _rendered(decided)


async def _write_what_was_accepted(
    proposal: Proposal, actor: str
) -> Callable[[], Awaitable[None]] | None:
    """Put an accepted proposal where it acts, or refuse the acceptance.

    Returns what undoes the write (restore the replaced version, or remove the skill), or `None`
    when nothing was written. Only `skill` has a destination a route can write; an accepted profile
    proposal is a record, since profiles are reviewed files in `data/profiles/`.

    Raises:
        HTTPException: The deployment keeps no store, or a tier admission rule refuses the body.
            Both leave the proposal open.
    """
    if proposal.kind != "skill":
        return None
    # Every admission rule, re-validated at decision time rather than trusted from the proposal: the
    # rules may have changed since it was written.
    try:
        validated_skill(proposal.content, expected_name=proposal.name)
    except SkillRefused as refusal:
        raise skill_refusal_http(refusal) from refusal
    store = store_or_503(
        await turn_store(),
        "this deployment keeps no personal skills, so accepting one would record a decision "
        "that changes nothing (CHEMCLAW_AGENT_MEMORY_ENABLED with a Postgres session store)",
    )
    # The row cap is enforced by the writer, so this door and the save route share one bound and
    # lock.
    replaced = await read_local_skill(store, actor, proposal.name)
    try:
        await save_local_skill(store, actor, proposal.name, proposal.content)
    except SkillRefused as refusal:
        raise skill_refusal_http(refusal) from refusal

    async def undo() -> None:
        """Put the tier back as it stood before this acceptance wrote to it."""
        if replaced is None:
            await delete_local_skill(store, actor, proposal.name)
        else:
            await save_local_skill(store, actor, proposal.name, replaced)

    return undo


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only."""
    app.get("/proposals", response_model=ProposalsOut)(list_proposals)
    app.post("/proposals/{kind}/{name}", response_model=ProposalOut)(decide_proposal)
