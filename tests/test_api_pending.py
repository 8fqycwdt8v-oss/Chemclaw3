"""The answer route: who may settle a held-open question.

**This file is the control.** A Temporal signal is unsigned, so `AwaitAnswerWorkflow` treats
`answered_by` as attribution and never as authorization
(`D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned`). The decision about who may answer
therefore lives entirely on this side of the wire, and if these tests do not hold it, nothing does —
the workflow will accept any signal that reaches the broker.

The four refusals are separated deliberately, because they are four different facts: 404 no such
request, 403 not yours to answer, 409 already decided, 503 the answer was not delivered. Collapsing
any two of them would tell a caller to retry something that will never succeed, or to give up on
something that would.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.api.auth import GROUP_ROLE_PREFIX, Principal, require_principal
from chemclaw.api.routes.pending import _may_answer
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import pending_store
from tests.pg import migrated_db_or_skip

_ALICE = Principal(oid="u-alice", upn="alice@example.com", roles=frozenset())
_BOB = Principal(oid="u-bob", upn="bob@example.com", roles=frozenset())
_QC_LEAD = Principal(
    oid="u-carol", upn="carol@example.com", roles=frozenset({f"{GROUP_ROLE_PREFIX}qc-team"})
)

REQUESTER = "api-pending-requester"


def _no_connectors(profile: str | None = None) -> list[object]:
    """No connector session: nothing here reaches a capability server."""
    return []


def _app() -> FastAPI:
    """The front door over the real pending store — the wiring these routes' claims are about."""
    return create_app(
        connector_factory=_no_connectors,
        graph_factory=lambda *args, **kwargs: None,
    )


def _client(app: FastAPI, principal: Principal) -> TestClient:
    """A client whose every request arrives as `principal`."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


async def _open(request_id: str, *, asked_of: str = "") -> None:
    """Open one wait to answer."""
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM pending_requests WHERE request_id = %s", (request_id,))
        await conn.commit()
    await pending_store.open_request(
        request_id=request_id,
        kind="measurement",
        subject="run the four conditions",
        rationale="the campaign is suspended on this batch",
        asked_of=asked_of,
        requested_by=REQUESTER,
        session_id="s-1",
        correlation_id="c-1",
        due_at=datetime.now(UTC) + timedelta(days=7),
    )


def _routed(
    asked_of: str, *, kind: str = "measurement", requested_by: str = "u-someone-else"
) -> pending_store.PendingRequest:
    """A stored request with just the three fields the gate reads, so the gate is what is tested."""
    return pending_store.PendingRequest(
        request_id="r-gate",
        kind=kind,
        subject="s",
        rationale="r",
        asked_of=asked_of,
        requested_by=requested_by,
        session_id="s-1",
        state="waiting",
    )


def test_routing_decides_who_may_answer_and_the_requester_is_not_automatic() -> None:
    """`_may_answer` is the gate; `asked_of` is only where the question was pointed.

    The requester is deliberately not privileged. "I asked the QA lead to approve this" must not
    also mean "and I may approve it myself", which is the shape every self-approval hole has.
    """
    # Unrouted: open to anyone authenticated, which is the same posture every read here has.
    assert _may_answer(_ALICE, _routed("")) is True
    # Routed to an actor, by object id or by user principal name.
    assert _may_answer(_ALICE, _routed("u-alice")) is True
    assert _may_answer(_ALICE, _routed("alice@example.com")) is True
    assert _may_answer(_BOB, _routed("u-alice")) is False
    # Routed to an entitlement, spelled bare — a security group reaches the role set prefixed, and
    # a deployment routes to whichever spelling it has.
    assert _may_answer(_QC_LEAD, _routed("qc-team")) is True
    assert _may_answer(_ALICE, _routed("qc-team")) is False


def test_the_requester_can_never_approve_their_own_irreversible_change() -> None:
    """Separation of duties, asserted against the routing that used to defeat it.

    The seam shipped raising every approval with `asked_of` unset, which took the "anyone
    authenticated" branch and let the requester sign off their own unrecoverable change. Both halves
    are pinned: an unrouted approval no longer admits its requester, and neither does one routed to
    a group the requester belongs to — because routing a control to a team the requester is on is
    exactly how a separation-of-duties rule gets quietly lost again.
    """
    assert _may_answer(_ALICE, _routed("", kind="approval", requested_by="u-alice")) is False
    assert _may_answer(_ALICE, _routed("qc-team", kind="approval", requested_by="u-alice")) is False
    qc_asked = _routed("qc-team", kind="approval", requested_by="u-alice")
    assert _may_answer(_QC_LEAD, qc_asked) is True
    # A measurement is ordinary work: a chemist answering their own lab's request is not a breach,
    # so the rule is scoped to the kind rather than applied to every request.
    assert _may_answer(_ALICE, _routed("", kind="measurement", requested_by="u-alice")) is True


def test_answering_a_request_routed_to_somebody_else_is_refused() -> None:
    """403, and the wait is untouched."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _open("api-pending-403", asked_of="u-alice")

    asyncio.run(_run())
    with _client(_app(), _BOB) as client:
        response = client.post("/pending/api-pending-403/answer", json={"payload": {}})
    assert response.status_code == 403

    async def _check() -> None:
        stored = await pending_store.get_request("api-pending-403")
        assert stored is not None and stored.state == "waiting"

    asyncio.run(_check())


def test_answering_an_unknown_request_is_a_404_and_not_a_403() -> None:
    """A request that does not exist is a different fact from one that is not yours.

    Kept apart on purpose: this route lists nothing a caller could enumerate — `GET /pending` only
    ever returns what is routed to them — so there is no id to probe for, and telling a caller
    plainly that nothing is there is better than making them guess at a permission problem.
    """

    async def _run() -> None:
        await migrated_db_or_skip()

    asyncio.run(_run())
    with _client(_app(), _ALICE) as client:
        response = client.post("/pending/api-pending-nope/answer", json={"payload": {}})
    assert response.status_code == 404


def test_answering_a_decided_request_is_a_409() -> None:
    """A second answer is told, rather than silently ignored.

    The workflow ignores a duplicate signal because a signal has no reply channel. This route reads
    the store first precisely so the caller who is too late finds out.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _open("api-pending-409")
        await pending_store.settle_request(
            "api-pending-409", state="answered", answered_by="u-first", answer={}
        )

    asyncio.run(_run())
    with _client(_app(), _ALICE) as client:
        response = client.post("/pending/api-pending-409/answer", json={"payload": {}})
    assert response.status_code == 409
    assert "already answered" in response.json()["detail"]


def test_an_undeliverable_answer_is_a_503_and_settles_nothing() -> None:
    """With no broker reachable, the route reports 503 and the request stays open.

    The ordering is the decision: the store is *not* written first. A row reading `answered` with
    nothing released would leave the campaign waiting forever while the inbox looked clean, which is
    strictly worse than a failed request the caller can retry.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _open("api-pending-503")

    asyncio.run(_run())
    app = _app()
    with _client(app, _ALICE) as client:
        # No Temporal broker is configured for this test's process, so `connect()` fails.
        response = client.post("/pending/api-pending-503/answer", json={"payload": {}})
    assert response.status_code == 503

    async def _check() -> None:
        stored = await pending_store.get_request("api-pending-503")
        assert stored is not None and stored.state == "waiting"

    asyncio.run(_check())


def test_the_inbox_returns_what_is_waiting_on_the_caller() -> None:
    """`GET /pending` is scoped to the authenticated caller, not to a query parameter."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _open("api-pending-mine", asked_of="u-alice")
        await _open("api-pending-theirs", asked_of="u-bob")

    asyncio.run(_run())
    with _client(_app(), _ALICE) as client:
        body = client.get("/pending").json()
    ids = {row["request_id"] for row in body["requests"]}
    assert "api-pending-mine" in ids
    assert "api-pending-theirs" not in ids
    assert body["count"] == len(body["requests"])


@pytest.mark.parametrize("path", ["/pending", "/pending/{request_id}/answer"])
def test_both_routes_are_behind_the_authentication_gate(path: str) -> None:
    """Neither route is reachable without a principal.

    Asserted here as well as by `tests/test_route_auth_coverage.py`'s walk, because this pair is
    the one place in the tree where an unauthenticated caller could settle work.
    """
    app = _app()
    handlers = {
        route.path: route  # type: ignore[attr-defined]
        for route in app.routes
        if getattr(route, "path", "") == path
    }
    assert path in handlers
    dependencies = str(handlers[path].dependant.dependencies)  # type: ignore[attr-defined]
    assert "require_principal" in dependencies
