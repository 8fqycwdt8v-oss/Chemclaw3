"""The inbox and the answer: HTTP routes over the durable wait (`durable/awaiting.py`).

Routes, not agent tools: a Temporal signal is unsigned, so `AwaitAnswerWorkflow` treats
`answered_by` as attribution only, and who may answer must be decided here, before the signal is
sent — a model must never authorize its own work.

`asked_of` is routing; `_may_answer` is the gate. An unrouted request is answerable by any
authenticated caller; a routed one only by the named actor or a holder of the named entitlement.
Asking does not entitle the requester to answer.
"""

import logging

from fastapi import FastAPI, HTTPException
from starlette.responses import Response

from chemclaw.api.auth import GROUP_ROLE_PREFIX, Principal
from chemclaw.api.deps import CurrentUser
from chemclaw.api.schemas import PendingAnswerIn, PendingRequestOut, PendingRequestsOut
from chemclaw.core.temporal_client import connect
from chemclaw.durable import pending_store
from chemclaw.kg.premise import count_refusals, premise_breaks

logger = logging.getLogger(__name__)


#: Request kinds where the requester may never be the answerer, whatever the routing says.
#:
#: Separation of duties: an `approval` gates an irreversible external change, and its whole point is
#: that a second person looked — even when routed to a group the requester belongs to. Approvals
#: must also be routed at launch. Other kinds (`measurement`, `question`) may be answered within the
#: requester's own team.
SECOND_PERSON_KINDS = frozenset({"approval"})


def _may_answer(principal: Principal, stored: pending_store.PendingRequest) -> bool:
    """Whether this caller may answer this request.

    First separation of duties: for a kind in `SECOND_PERSON_KINDS` the requester is refused before
    routing is consulted. Then routing: empty means any authenticated caller; otherwise the caller's
    oid, user principal name, or an entitlement they hold, matched bare and with `GROUP_ROLE_PREFIX`
    (security groups carry the prefix, app roles do not).
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

    Mirrors `_may_answer`'s routing branch, so anything answerable is visible. Separation of duties
    cannot be expressed as a routing query, so `list_pending` applies the gate itself.
    """
    identities = [principal.upn, *principal.roles]
    identities += [
        role.removeprefix(GROUP_ROLE_PREFIX)
        for role in principal.roles
        if role.startswith(GROUP_ROLE_PREFIX)
    ]
    return [identity for identity in identities if identity]


async def list_pending(principal: CurrentUser, limit: int = 50) -> PendingRequestsOut:
    """One page of what is waiting on you — the open requests you may actually answer.

    The cross-conversation read: the person who must answer is usually not the one who asked.
    `total_routed_to_you` and `truncated` say what the page holds; `limit` asks for more, bounded by
    the store. No cursor: the list is ordered by deadline, so it does not reorder under the reader.
    """
    # The caller's whole routing surface (oid, upn, entitlements), so team-routed requests appear.
    page = await pending_store.open_requests(
        asked_of=principal.oid, identities=_routing_identities(principal), limit=limit
    )
    # Filtered through `_may_answer` itself, so the inbox never lists a request (such as one's own
    # approval) that the answer route would refuse.
    answerable = [request for request in page.requests if _may_answer(principal, request)]
    return PendingRequestsOut(
        requests=[PendingRequestOut(**request.model_dump()) for request in answerable],
        count=len(answerable),
        total_routed_to_you=page.total_waiting,
        # Only the store's truncation hides rows; rows the gate removes are not the caller's to page
        # for.
        truncated=page.truncated,
    )


async def answer_pending(
    request_id: str, body: PendingAnswerIn, principal: CurrentUser
) -> Response:
    """Answer one held-open question, releasing whatever is waiting on it.

    Refusals, each a different fact:

    - **404** — no such request.
    - **403** — the caller is not who this was routed to.
    - **409** — it is no longer waiting (answered, expired or cancelled); a signal has no reply
      channel, so this route is where a second answer is told.
    - **409** — the knowledge it rests on was superseded or refuted while it waited. The request
      stays `waiting`: it can still be answered by someone who re-reads it, or expire.
    - **503** — the broker is unreachable and nothing was delivered. The store is not written first,
      so a row never says `answered` while the waiter still waits.

    The premise check lives here, not in the workflow: a workflow cannot do I/O without a new
    activity and a replay guard, and this route already has the caller, the row and a way to refuse.
    """
    stored = await pending_store.get_request(request_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="no such request")
    if not _may_answer(principal, stored):
        raise HTTPException(status_code=403, detail="this request is not routed to you")
    if stored.state != "waiting":
        raise HTTPException(status_code=409, detail=f"this request is already {stored.state}")
    # Only breaks that `blocks_an_answer`: an `absent` note may just be a checkout behind, and this
    # route has no override. A `review` is exempt: its premise is what is under review, and
    # superseding that note is the natural outcome of reading it.
    breaks = await premise_breaks(stored.premise_note_ids) if stored.kind != "review" else []
    broken = [item for item in breaks if item.blocks_an_answer()]
    if broken:
        count_refusals("answer", broken)
        raise HTTPException(
            status_code=409,
            detail=(
                "the knowledge this question rests on has changed since it was asked: "
                + "; ".join(item.describe() for item in broken)
                + ". Re-read it before answering; the question is still open."
            ),
        )

    try:
        client = await connect()
        handle = client.get_workflow_handle(request_id)
        # The authenticated actor, never a body-supplied name, goes into the audit-bearing record.
        await handle.signal("provide", {"answered_by": principal.oid, "payload": body.payload})
    except Exception as exc:
        logger.warning("pending.signal_failed: %s: %s", request_id, exc)
        raise HTTPException(
            status_code=503, detail="the answer could not be delivered; try again"
        ) from exc

    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    App decorators, not an `APIRouter`; see `chemclaw/api/routes/jobs.py`'s `register`.
    """
    # Not under `/sessions/…`: a question about all of them, asked by someone who holds no session
    # id — the same shape, and the same reason, as `GET /plans/pending`.
    app.get("/pending")(list_pending)
    app.post("/pending/{request_id}/answer", status_code=204)(answer_pending)


__all__ = ["register"]
