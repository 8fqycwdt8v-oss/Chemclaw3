"""The human gate on a proposed behaviour change: read what is waiting, accept it, or decline it.

`agent/behaviour_proposals.py` holds the queue and says why it exists; this is the half a person
acts through, and its shape is the one `plan.py`, `workflows.py` and `skills.py` all have for one
reason: **a model must never be able to authorize its own behaviour change.** The agent proposes
with `propose_skill`; nothing it can call decides.

**Owner-scoped by construction, not by a check.** Every handler reads `principal.oid` and passes it
to a store call keyed by it — there is no parameter naming whose queue to touch, so there is no
authorization decision here to get wrong. That is the same argument `skills.py` makes, and it is
the right one for the same reason: an accepted skill acts on one person's turns, so the queue in
front of it is one person's too.

**Accepting is where the two halves meet, and it is the one place a cap could have had a hole.**
`POST /skills/mine` refuses a save past `agent_local_skills_max`, because every personal skill sits
in the prefix of every turn its owner takes. An acceptance that wrote the tier without that check
would be a second door into the same bound, so `accept` refuses with the same 409 and the
proposal stays `open` — the person may make room and accept it later, which is exactly what they
would want and the opposite of a decision silently failing.

**A decision is final**, which `agent/behaviour_proposals.py` argues: deciding twice reports what
stands rather than replacing it. A person who declined something and later wants it writes it
directly through `POST /skills/mine`, one indirection fewer and with no pretence that the agent
proposed it twice.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from chemclaw.agent.behaviour_proposals import (
    DECIDED,
    Proposal,
    ProposalKind,
    default_proposal_store,
)
from chemclaw.agent.local_skills import list_local_skills, save_local_skill
from chemclaw.api.deps import CurrentUser
from chemclaw.api.runner import turn_store
from chemclaw.core.config import settings


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
    """What is waiting on this person — open by default, since that is the question they have.

    `state=""` asks for everything, including what they have already decided and what a newer
    version superseded. That is the audit read rather than the queue read, and it is the same
    surface because the two differ only in a predicate.
    """
    store = default_proposal_store()
    wanted = [state] if state else []
    return ProposalsOut(
        proposals=[
            _rendered(proposal) for proposal in await store.list_for(principal.oid, states=wanted)
        ]
    )


async def decide_proposal(
    kind: ProposalKind, name: str, payload: DecisionIn, principal: CurrentUser
) -> ProposalOut:
    """Accept or decline one proposal, and — on an acceptance — write what it proposed.

    The write happens **inside the decision** rather than being left to a second call, because a
    queue that records "accepted" and writes nothing is the shape where a person believes they have
    changed something and have not. If the write cannot happen, the decision is refused and the
    proposal stays open, which is recoverable; a recorded acceptance that failed to write is not.
    """
    store = default_proposal_store()
    standing = await store.one(principal.oid, kind, name, payload.content_hash)
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
    if payload.accepted:
        await _write_what_was_accepted(standing, principal.oid)
    decided = await store.decide(
        principal.oid,
        kind,
        name,
        payload.content_hash,
        accepted=payload.accepted,
        decided_by=principal.oid,
        reason=payload.reason,
    )
    if decided is None:  # pragma: no cover - `one` above found it a moment ago
        raise HTTPException(404, f"the {kind} proposal {name!r} disappeared while being decided")
    return _rendered(decided)


async def _write_what_was_accepted(proposal: Proposal, actor: str) -> None:
    """Put an accepted proposal where it acts, or refuse the acceptance.

    **Only `skill` has a destination a route can write**, and saying so is better than a column
    pretending otherwise. A profile is a file in `data/profiles/`, git-resident and reviewed in a
    pull request, and no HTTP route can commit — so an accepted profile proposal is a *record* that
    a person wants one, which somebody then raises as a change. That is a smaller thing than it
    sounds and is the honest half of what this queue can do today.

    Raises:
        HTTPException: The deployment keeps no store, or this person is at
            `agent_local_skills_max`. Both leave the proposal open, which is the recoverable
            direction: a person can make room and come back.
    """
    if proposal.kind != "skill":
        return
    store = await turn_store()
    if store is None:
        raise HTTPException(
            503,
            "this deployment keeps no personal skills, so accepting one would record a decision "
            "that changes nothing (CHEMCLAW_AGENT_MEMORY_ENABLED with a Postgres session store)",
        )
    held = await list_local_skills(store, actor)
    if proposal.name not in held and len(held) >= settings.agent_local_skills_max:
        raise HTTPException(
            409,
            f"you already keep {len(held)} personal skills, which is this deployment's limit of "
            f"{settings.agent_local_skills_max}: every one of them is in the prompt of every turn "
            "you take. Remove one and accept this again — it stays here until you do",
        )
    await save_local_skill(store, actor, proposal.name, proposal.content)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only."""
    app.get("/proposals", response_model=ProposalsOut)(list_proposals)
    app.post("/proposals/{kind}/{name}", response_model=ProposalOut)(decide_proposal)
