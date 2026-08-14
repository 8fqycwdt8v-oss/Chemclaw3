"""The durable Yes/No holds (D-032): list what awaits the caller, read one, deliver the decision.

Every route is owner-scoped through `chemclaw.api.deps.owned_approval` — a hold authorizes a
knowledge write, so it is answerable only by the chemist whose turn raised it, and an unknown hold
is indistinguishable from someone else's (404, no existence leak). The Temporal-backed
collaborators are read through the front-door module at call time because the suite patches them
there (`chemclaw.api.app.approval_owner` and friends) — see `chemclaw/api/routes/README.md`.
"""

from fastapi import Depends, FastAPI, HTTPException
from starlette.responses import Response

from chemclaw.agent.interaction_tools import PendingApproval
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser, owned_approval
from chemclaw.api.schemas import ApprovalDecisionIn, ApprovalStatusOut


async def list_approvals(
    principal: CurrentUser,
) -> list[PendingApproval]:
    """The caller's open approval holds — the review queue (gap RCH-3).

    Without this route the durable Yes/No hold (D-032) was a dead end: a hold could be
    started, but its id was only ever returned into a turn that then ended, and the thin UI
    rendered the request as an inert trace line. A hold that nobody can find or answer can
    only time out, which silently drops the knowledge it was holding.

    Scoped to the caller: a hold authorizes a knowledge write, so it is answerable only by
    the chemist whose turn raised it.
    """
    return await front_door.list_pending_approvals(owner=principal.oid)


async def get_approval(
    approval_id: str,
) -> ApprovalStatusOut:
    """One hold's current state (`pending`/`approved`/`rejected`/`expired`)."""
    try:
        return ApprovalStatusOut(
            approval_id=approval_id, status=await front_door.approval_status(approval_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="no such approval hold") from exc


async def decide(
    approval_id: str,
    body: ApprovalDecisionIn,
) -> Response:
    """Deliver the human Yes/No to a pending hold — the button click, finally wired.

    Deliberately an HTTP route and **not** an agent tool: the agent proposes, a human signs
    off (D-005). A tool would let the agent approve its own candidate and collapse the review
    line the whole PR-gate exists to draw.
    """
    try:
        await front_door.decide_approval(approval_id, body.approved)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="no such approval hold") from exc
    return Response(status_code=204)


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
    app.get("/approvals")(list_approvals)
    app.get("/approvals/{approval_id}", dependencies=[Depends(owned_approval)])(get_approval)
    app.post(
        "/approvals/{approval_id}/decision",
        status_code=204,
        dependencies=[Depends(owned_approval)],
    )(decide)
