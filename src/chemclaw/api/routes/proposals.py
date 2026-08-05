"""The PR-gate's review queue: list, read and decide note proposals, and close them on merge.

The gate is named across `CLAUDE.md`, `ARCHITECTURE.md`, `SECURITY.md` and D-005 as the line that
makes machine-written knowledge safe; these routes are its surface. Visibility is the one gate in
`chemclaw.api.deps` that is *not* the shared owner rule — a reviewer may see any proposal — and
the decision route keeps its refusal order in the body (403 → 422 → 404) rather than taking the
visibility dependency, so the split changed no status code.

`POST /events/knowledge-merged` lives here rather than in an events module of its own because its
consequential half *is* a proposal transition: closing the rows whose notes a git host reports as
merged. The reindex it also kicks is idempotent.
"""

import hashlib
import hmac

from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import Response

from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser, VisibleProposal, _is_reviewer, _visible_proposal
from chemclaw.api.schemas import (
    KnowledgeMergedIn,
    ProposalDecisionIn,
    ProposalDetail,
    ProposalFile,
    ProposalSummary,
    _proposal_summary,
)
from chemclaw.core.config import settings
from chemclaw.kg.proposal import (
    ProposalState,
    close_merged_notes,
    decide_proposal,
    list_proposals,
)

# Where *this* endpoint expects the body signature, in the `sha256=<hex>` shape GitHub's own
# `X-Hub-Signature-256` uses.
#
# **A translation step is required, and this comment used to say the opposite.** It claimed the
# shape was what "GitHub, GitLab and Azure DevOps webhooks all produce, so an operator wires this
# without a translation step" — false on both halves, measured by the 2026-08-05 review: GitHub
# sends the signature under `X-Hub-Signature-256` with a pull-request payload, GitLab sends
# `X-Gitlab-Token` carrying the raw secret rather than an HMAC, and Azure DevOps uses Basic auth.
# This route additionally requires a `{"note_ids": [...]}` body (`api/schemas.py`), which no host
# emits for a merge. So the contract is deliberately *ours*: a small proxy or a CI step maps the
# host's event onto it, and that is a step to document rather than a claim to make disappear.
_WEBHOOK_SIGNATURE_HEADER = "X-Chemclaw-Signature"


def _webhook_signature_ok(body: bytes, header: str) -> bool:
    """Whether `header` is a valid HMAC-SHA256 of `body` under the configured webhook secret.

    False when no secret is configured — "unsigned" rather than "trusted", so the caller decides
    what an unsigned call may do rather than this function deciding for it. `compare_digest` for
    the comparison: a byte-at-a-time `==` on a MAC leaks its prefix through timing, which is the
    one implementation detail of a signature check that matters.
    """
    secret = settings.note_webhook_secret
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def list_note_proposals(
    principal: CurrentUser,
    state: str = "",
    before_id: int = 0,
) -> list[ProposalSummary]:
    """The PR-gate's queue: what has been proposed, and what became of it.

    The gate is named across `CLAUDE.md`, `ARCHITECTURE.md`, `SECURITY.md` and D-005 as the
    line that makes machine-written knowledge safe — and it had no surface at all. A note was
    pushed to `note/<id>` and that was the end of it: nothing listed what was awaiting review,
    the chemist who proposed a note could not find out what happened to it, and a rejection
    left no trace, because a rejection is a deleted branch. Browsing refs in a git host was the
    only discovery mechanism.

    A reviewer sees every proposal; anyone else sees their own. `before_id` pages backwards
    through the monotonic row ids rather than offsetting, so a proposal arriving mid-page
    cannot make the next page skip or repeat a row.
    """
    try:
        wanted = ProposalState(state) if state else None
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"unknown state {state!r}; expected one of "
            f"{sorted(s.value for s in ProposalState)}",
        ) from exc
    scope = "" if _is_reviewer(principal) else principal.oid
    proposals = await list_proposals(wanted, scope, settings.proposal_list_limit, before_id or None)
    return [_proposal_summary(proposal) for proposal in proposals]


async def get_note_proposal(
    proposal: VisibleProposal,
) -> ProposalDetail:
    """One proposal with everything it would write, exactly as it would land in the tree."""
    return ProposalDetail(
        **_proposal_summary(proposal).model_dump(),
        content=proposal.content,
        dependencies=[ProposalFile(**file.model_dump()) for file in proposal.dependencies],
        session_id=proposal.session_id,
        correlation_id=proposal.correlation_id,
    )


async def decide_note_proposal(
    proposal_id: int,
    body: ProposalDecisionIn,
    principal: CurrentUser,
) -> Response:
    """Record the human sign-off — or, for the first time, the refusal.

    Deliberately an HTTP route and not an agent tool, for the reason
    `POST /approvals/{id}/decision` is not (D-005): a tool would let the agent sign off on its
    own proposal and collapse the line the whole gate draws.

    This records the *decision*, which for a rejection is the entire outcome — there is no git
    action to take, and the record is what makes "we considered this and said no" answerable
    later. A merge additionally happens in the git host; the webhook below closes the row when
    the host reports it, so a proposal merged without anyone calling this is not left open.
    """
    if not _is_reviewer(principal):
        raise HTTPException(status_code=403, detail="deciding a proposal needs a review role")
    if not body.approved and not body.reason.strip():
        raise HTTPException(
            status_code=422, detail="a rejection must state why; that is what the record is for"
        )
    await _visible_proposal(proposal_id, principal)
    state = ProposalState.MERGED if body.approved else ProposalState.REJECTED
    decided = await decide_proposal(proposal_id, state, principal.oid or "", body.reason)
    if decided is None:
        raise HTTPException(status_code=409, detail="this proposal has already been decided")
    return Response(status_code=204)


async def knowledge_merged(
    request: Request,
    principal: CurrentUser,
) -> dict[str, str]:
    """Tell the deployment a note merged, so freshness stops being bounded by a timer (SCH-6).

    The whole system was poll-on-a-timer: there was no inbound event path at all, so the
    worst-case staleness of a merged note was the slowest configured interval, everywhere. A
    git host's post-merge webhook (or an operator) calls this, and the derived note index is
    rebuilt now rather than at the next scheduled sweep — collapsing gap SCH-2's staleness
    window from an interval to seconds.

    It now also **closes the proposals** the named notes belong to, which is what turns the
    gate into a loop rather than an outbox: without it a merged note's row would sit `open`
    forever and the review queue would only ever grow. Only open rows move, so a duplicate
    delivery — which webhooks routinely are — decides nothing twice.

    **Signed**, because the body now carries an authorization-shaped claim ("a human merged
    these"). While this route only kicked an idempotent reindex, any authenticated principal
    calling it was harmless; a principal who can assert a merge could close their own proposal
    without a reviewer ever seeing it. The signature is HMAC-SHA256 over the raw body under
    `note_webhook_secret`, compared in constant time. With no secret configured the route keeps
    its old behaviour and refuses to decide anything — an unsigned caller may still force a
    reindex, which is what an operator running it by hand needs.
    """
    raw = await request.body()
    signed = _webhook_signature_ok(raw, request.headers.get(_WEBHOOK_SIGNATURE_HEADER, ""))
    if settings.note_webhook_secret and not signed:
        raise HTTPException(status_code=401, detail="invalid or missing webhook signature")
    merged = KnowledgeMergedIn.model_validate_json(raw) if raw else KnowledgeMergedIn()
    closed = 0
    if merged.note_ids:
        if not signed:
            raise HTTPException(
                status_code=401,
                detail="closing a proposal needs a signed webhook; configure "
                "CHEMCLAW_NOTE_WEBHOOK_SECRET and sign the body",
            )
        closed = await close_merged_notes(merged.note_ids, principal.oid or "webhook")
    started = await front_door.request_note_reindex()
    return {"status": "accepted", "workflow_id": started, "proposals_closed": str(closed)}


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`:
    since FastAPI 0.139 `include_router` is lazy — `app.routes` would hold opaque
    `_IncludedRouter` nodes, invisible to everything that walks the route table by type
    (`tests/test_route_auth_coverage.py`, the session-scope inventory in
    `tests/test_service.py`) — and a standalone router's routes carry no
    `dependency_overrides_provider`, which silently disables `app.dependency_overrides`.
    Registering on the app keeps both exactly as they were when these handlers lived in
    `create_app`.
    """
    app.get("/proposals")(list_note_proposals)
    app.get("/proposals/{proposal_id}")(get_note_proposal)
    app.post("/proposals/{proposal_id}/decision", status_code=204)(decide_note_proposal)
    app.post("/events/knowledge-merged", status_code=202)(knowledge_merged)
