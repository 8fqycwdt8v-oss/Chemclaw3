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


#: Request kinds where the requester may never be the answerer, whatever the routing says.
#:
#: **Separation of duties, and it is a rule about the kind rather than about the routing.** An
#: `approval` is the human gate on an irreversible external change: filing a deviation, releasing a
#: batch, writing to somebody else's system of record. The whole content of that control is that a
#: *second* person looked. Routing alone did not deliver it — the first version of this seam left
#: `asked_of` unset on every approval it raised, which took the "anyone authenticated" branch below
#: and let the requester approve their own unrecoverable change. Routing is now set at the launch
#: site and an unrouted one is refused outright, so this is the third layer rather than the only
#: one; it is here because it holds even when a deployment routes an approval to a group the
#: requester happens to belong to.
#:
#: Not applied to every kind: a `measurement` or a `question` routed to a team the requester is on
#: is ordinary work, and refusing it would stop a chemist answering their own lab's request.
SECOND_PERSON_KINDS = frozenset({"approval"})


def _may_answer(principal: Principal, stored: pending_store.PendingRequest) -> bool:
    """Whether this caller may answer this request.

    Two questions, and the order matters. First, separation of duties: for a kind in
    `SECOND_PERSON_KINDS` the requester is refused before routing is consulted at all, so an
    approval routed to a group cannot be self-signed by a member of it who asked for it.

    Then routing. Empty routing means anyone authenticated: the request is open to whoever is
    entitled to reach this service at all, which is the same posture every read here has. A named
    routing matches the caller's object id, their user principal name, or an entitlement they hold —
    the last spelled both bare and with `GROUP_ROLE_PREFIX`, because a security group reaches the
    role set through that prefix and an app role does not, and a deployment routes to whichever it
    has.
    """
    if stored.kind in SECOND_PERSON_KINDS and stored.requested_by == principal.oid:
        return False
    asked_of = stored.asked_of
    if not asked_of:
        return True
    if asked_of in {principal.oid, principal.upn}:
        return True
    return asked_of in principal.roles or f"{GROUP_ROLE_PREFIX}{asked_of}" in principal.roles


def _routing_identities(principal: Principal) -> list[str]:
    """Every string a request's `asked_of` could name to reach this caller, besides their oid.

    The mirror of `_may_answer`'s routing branch, and deliberately built from the same three
    sources: the user principal name, the roles held bare, and the same roles with
    `GROUP_ROLE_PREFIX` stripped — a security group arrives prefixed and a deployment may route to
    the unprefixed group name. Anything `_may_answer` would accept must appear here, or a request
    is answerable and invisible.
    """
    identities = [principal.upn, *principal.roles]
    identities += [
        role.removeprefix(GROUP_ROLE_PREFIX)
        for role in principal.roles
        if role.startswith(GROUP_ROLE_PREFIX)
    ]
    return [identity for identity in identities if identity]


async def list_pending(principal: CurrentUser) -> PendingRequestsOut:
    """What is waiting on you — every open request routed to you or to nobody in particular.

    The cross-conversation read, for the reason `GET /plans/pending` exists: a question raised in a
    turn the asker has closed lives only inside that turn otherwise, and the person who has to
    answer it is usually not the person who asked.
    """
    # The caller's whole routing surface, not just their object id: `_may_answer` accepts a upn and
    # an entitlement, so an inbox that matched only the oid hid every team-routed request from the
    # team it was routed to.
    requests = await pending_store.open_requests(
        asked_of=principal.oid, identities=_routing_identities(principal)
    )
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
    if not _may_answer(principal, stored):
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
