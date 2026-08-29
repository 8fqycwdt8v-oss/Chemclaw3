"""The inbox and the answer: HTTP routes over the durable wait (`durable/awaiting.py`).

**These are routes and not agent tools, and that is the whole control.** A signal is unsigned —
anyone who can reach the broker can send one — so `AwaitAnswerWorkflow` treats `answered_by` as
attribution and never as authorization
(`D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned`). Deciding *who may answer*
therefore has to happen on this side of the wire, before the signal is sent, exactly as
`POST /sessions/{id}/plan/decision` and `POST /proposals/{id}/decision` are routes for the reason
that a model must never authorize its own work.

**`asked_of` is routing and `_may_answer` is the gate, and they are deliberately not the same
thing.** A request routed to nobody in particular is answerable by any authenticated caller; one
routed to a named actor or an entitlement is answerable by that actor, by a holder of that
entitlement, and by nobody else. The requester is not automatically permitted: "I asked the QA lead
to approve this" must not mean "and I may approve it myself".
"""

import logging

from fastapi import FastAPI, HTTPException
from starlette.responses import Response

from chemclaw.api.auth import GROUP_ROLE_PREFIX, Principal
from chemclaw.api.deps import CurrentUser
from chemclaw.api.schemas import PendingAnswerIn, PendingRequestOut, PendingRequestsOut
from chemclaw.core.temporal_client import connect
from chemclaw.durable import pending_store

logger = logging.getLogger(__name__)


def _may_answer(principal: Principal, asked_of: str) -> bool:
    """Whether this caller may answer a request routed to `asked_of`.

    Empty routing means anyone authenticated: the request is open to whoever is entitled to reach
    this service at all, which is the same posture every read here has. A named routing matches the
    caller's object id, their user principal name, or an entitlement they hold — the last spelled
    both bare and with `GROUP_ROLE_PREFIX`, because a security group reaches the role set through
    that prefix and an app role does not, and a deployment routes to whichever it has.
    """
    if not asked_of:
        return True
    if asked_of in {principal.oid, principal.upn}:
        return True
    return asked_of in principal.roles or f"{GROUP_ROLE_PREFIX}{asked_of}" in principal.roles


async def list_pending(principal: CurrentUser) -> PendingRequestsOut:
    """What is waiting on you — every open request routed to you or to nobody in particular.

    The cross-conversation read, for the reason `GET /plans/pending` exists: a question raised in a
    turn the asker has closed lives only inside that turn otherwise, and the person who has to
    answer it is usually not the person who asked.
    """
    requests = await pending_store.open_requests(asked_of=principal.oid)
    return PendingRequestsOut(
        requests=[PendingRequestOut(**request.model_dump()) for request in requests],
        count=len(requests),
    )


async def answer_pending(
    request_id: str, body: PendingAnswerIn, principal: CurrentUser
) -> Response:
    """Answer one held-open question, releasing whatever is waiting on it.

    Four refusals, each a different fact and each with its own status:

    - **404** — no such request. Also what an already-settled request returns from the *store*
      check below, but not the same case, so they are separated.
    - **403** — the caller is not who this was routed to.
    - **409** — it is no longer waiting. An answered, expired or cancelled request is a decided
      one, and a second answer must be told rather than silently ignored. The workflow ignores a
      duplicate signal because a signal has no reply channel; this route is where a caller can
      actually be told.
    - **503** — the broker is unreachable, so the answer was not delivered. Deliberately not
      written to the store first: a row saying `answered` with nothing released is worse than a
      failed request, because the thing waiting would wait forever while the inbox looked clean.
    """
    stored = await pending_store.get_request(request_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="no such request")
    if not _may_answer(principal, stored.asked_of):
        raise HTTPException(status_code=403, detail="this request is not routed to you")
    if stored.state != "waiting":
        raise HTTPException(status_code=409, detail=f"this request is already {stored.state}")

    try:
        client = await connect()
        handle = client.get_workflow_handle(request_id)
        # The signal carries the *authenticated* actor, never anything the body supplied: the
        # workflow records it, and a body-supplied name would be a caller writing their own
        # attribution into an audit-bearing record.
        await handle.signal("provide", {"answered_by": principal.oid, "payload": body.payload})
    except Exception as exc:
        logger.warning("pending.signal_failed: %s: %s", request_id, exc)
        raise HTTPException(
            status_code=503, detail="the answer could not be delivered; try again"
        ) from exc

    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    On the app's own decorators rather than an `APIRouter`, for the reasons
    `chemclaw.api.routes.plan.register` states in full.
    """
    # Not under `/sessions/…`: a question about all of them, asked by someone who holds no session
    # id — the same shape, and the same reason, as `GET /plans/pending`.
    app.get("/pending")(list_pending)
    app.post("/pending/{request_id}/answer", status_code=204)(answer_pending)


__all__ = ["register"]
