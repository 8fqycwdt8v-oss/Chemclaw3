"""The gate between a model proposing a behaviour change and that change acting on anybody.

**This file is the control.** `propose_skill` is a tool the model calls; nothing it can call
decides. If what is here does not hold, the queue is a longer road to the thing
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` refuses outright — an agent writing its own
judgment into its own prompt.

Four properties, each the thing that would be false without it: the agent cannot decide, a decision
binds to the document the person was shown, accepting actually writes what was accepted, and the
bound on the personal tier holds on this door as well as on the other one.
"""

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.behaviour_proposals import (
    Proposal,
    content_hash,
    default_proposal_store,
)
from chemclaw.agent.local_skills import list_local_skills
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import proposals as proposal_routes
from chemclaw.api.routes import skills as skill_routes
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import registered_tool_names

_ALICE = Principal(oid="u-alice", upn="alice@example.com", roles=frozenset())
_BOB = Principal(oid="u-bob", upn="bob@example.com", roles=frozenset())

_BODY = "---\nname: cold-quench\ndescription: how to quench this class cold\n---\n\nQuench cold.\n"


def _no_connectors(profile: str | None = None) -> list[object]:
    """No connector session: nothing here reaches a capability server."""
    return []


@pytest.fixture(autouse=True)
def _in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """One process, one queue, and a fresh one per test.

    `monkeypatch.setattr` on the module singleton rather than a shared one, because
    `behaviour_proposals._IN_MEMORY` is a process-level object for the reason the CLI needs it to
    be — and a test that inherited the previous test's decisions would pass on rows it did not
    write.
    """
    from chemclaw.agent import behaviour_proposals

    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", True)
    monkeypatch.setattr(
        behaviour_proposals, "_IN_MEMORY", behaviour_proposals.InMemoryProposalStore()
    )


@pytest.fixture
def skills_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryStore:
    """A real `BaseStore` for the tier an acceptance writes.

    Patched on **both** route modules, because each imports `turn_store` by name. That is not a
    workaround for a seam — in production both resolve to the one function, and patching one and
    reading through the other is how a test can watch an acceptance "succeed" while the skill it
    accepted was never written.
    """
    backing = InMemoryStore()

    async def _store() -> Any:
        return backing

    monkeypatch.setattr(proposal_routes, "turn_store", _store)
    monkeypatch.setattr(skill_routes, "turn_store", _store)
    return backing


@pytest.fixture
def app(skills_store: InMemoryStore) -> FastAPI:
    """The real front door."""
    return create_app(connector_factory=_no_connectors, graph_factory=lambda *a, **k: None)


def _as(app: FastAPI, principal: Principal) -> TestClient:
    """The same app, arriving as somebody."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


def _propose(actor: str = _ALICE.oid, content: str = _BODY, name: str = "cold-quench") -> str:
    """Put one proposal in the queue as `propose_skill` would, and hand back its hash."""
    asyncio.run(
        default_proposal_store().propose(
            Proposal(
                kind="skill",
                name=name,
                content_hash=content_hash(content),
                content=content,
                rationale="this went wrong the same way twice",
                actor=actor,
                session_id="session-1",
                correlation_id="correlation-1",
            )
        )
    )
    return content_hash(content)


def test_no_tool_can_decide_a_proposal() -> None:
    """The property this whole file exists for, asserted over the registry rather than by reading.

    `propose_skill` is registered and is the only thing in the queue's direction a model holds.
    Deciding is `POST /proposals/...`, and a tool that could decide would let the agent propose a
    change to its own behaviour and grant it in the next tool call — with the trail recording a
    person's decision that no person took, which is exactly the defect `plan_approvals` was built
    after (`infra/sql/020_plan_approvals.sql`: "the trail therefore showed an attributable approval
    with no human act behind it").
    """
    from chemclaw.agent import tool_modules  # noqa: F401 - populates the registry

    names = registered_tool_names()

    assert "propose_skill" in names, "the proposer is not registered, so nothing can propose"
    deciding = {name for name in names if "accept" in name or "approve_skill" in name}
    assert not deciding, f"{sorted(deciding)} looks like a tool that can decide a proposal"


def test_a_decision_binds_to_the_document_the_person_was_shown(app: FastAPI) -> None:
    """A hash the caller did not read is a 404, not a decision.

    The proposer can supersede an open proposal between the read and the click, so a decision that
    named only the skill would authorize whatever that name currently holds — the control
    `plan_approvals` gets from keying on `plan_hash` rather than on the session.
    """
    digest = _propose()
    client = _as(app, _ALICE)

    assert (
        client.post(
            "/proposals/skill/cold-quench", json={"content_hash": "not-the-one", "accepted": True}
        ).status_code
        == 404
    )

    superseding = _propose(content=_BODY.replace("cold.", "warm."))
    stale = client.post(
        "/proposals/skill/cold-quench", json={"content_hash": digest, "accepted": True}
    )

    assert stale.status_code == 409, "a superseded document was decided"
    assert "newer version" in stale.json()["detail"]
    assert (
        client.post(
            "/proposals/skill/cold-quench", json={"content_hash": superseding, "accepted": True}
        ).status_code
        == 200
    )


def test_accepting_writes_what_was_accepted(app: FastAPI, skills_store: InMemoryStore) -> None:
    """A queue that records "accepted" and writes nothing is worse than no queue.

    The person would believe they had changed something and they would not have, with the record
    agreeing with them. So the write happens inside the decision, and this reads the tier through
    the store as well as through the route — the two agreeing is the assertion.
    """
    digest = _propose()
    client = _as(app, _ALICE)

    accepted = client.post(
        "/proposals/skill/cold-quench", json={"content_hash": digest, "accepted": True}
    )

    assert accepted.status_code == 200
    assert accepted.json()["state"] == "accepted"
    assert client.get("/skills/mine").json()["skills"] == ["cold-quench"]
    assert asyncio.run(list_local_skills(skills_store, _ALICE.oid)) == ["cold-quench"]


def test_declining_writes_nothing_and_leaves_a_trace(app: FastAPI) -> None:
    """A rejection is the record this table exists for, so it is readable afterwards."""
    digest = _propose()
    client = _as(app, _ALICE)

    declined = client.post(
        "/proposals/skill/cold-quench",
        json={"content_hash": digest, "accepted": False, "reason": "too narrow"},
    )

    assert declined.status_code == 200
    assert (declined.json()["state"], declined.json()["reason"]) == ("rejected", "too narrow")
    assert client.get("/skills/mine").json()["skills"] == []
    listed = client.get("/proposals", params={"state": "rejected"}).json()["proposals"]
    assert [(row["name"], row["reason"]) for row in listed] == [("cold-quench", "too narrow")]


def test_the_personal_tiers_cap_holds_on_this_door_too(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepting is a second way into `agent_local_skills_max`, and a second door is a hole.

    Every personal skill sits in the prefix of every turn its owner takes, which is why
    `POST /skills/mine` refuses past the cap. Refused here with the same 409 — and the proposal
    stays **open**, so the person can remove one and come back. A recorded acceptance that failed
    to write would not be recoverable.
    """
    monkeypatch.setattr(settings, "agent_local_skills_max", 1)
    first = _propose()
    other = _BODY.replace("cold-quench", "another").replace("cold.", "warm.")
    second = _propose(content=other, name="another")
    client = _as(app, _ALICE)

    assert (
        client.post(
            "/proposals/skill/cold-quench", json={"content_hash": first, "accepted": True}
        ).status_code
        == 200
    )

    refused = client.post(
        "/proposals/skill/another", json={"content_hash": second, "accepted": True}
    )

    assert refused.status_code == 409
    assert "limit of 1" in refused.json()["detail"]
    assert [row["state"] for row in client.get("/proposals").json()["proposals"]] == ["open"], (
        "a refused acceptance must leave the proposal open, or the person cannot come back to it"
    )
    assert client.get("/skills/mine").json()["skills"] == ["cold-quench"]


def test_one_chemists_queue_is_invisible_to_another(app: FastAPI) -> None:
    """Owner-scoped by construction: no parameter names whose queue is touched."""
    digest = _propose(actor=_ALICE.oid)

    bob = _as(app, _BOB)

    assert bob.get("/proposals").json()["proposals"] == []
    assert (
        bob.post(
            "/proposals/skill/cold-quench", json={"content_hash": digest, "accepted": True}
        ).status_code
        == 404
    )
    assert [row["state"] for row in _as(app, _ALICE).get("/proposals").json()["proposals"]] == [
        "open"
    ], "another chemist's 404 was a deletion rather than an absence"


def test_a_decision_is_not_replaced_by_a_second_one(app: FastAPI) -> None:
    """Deciding twice reports what stands, and says where the person's actual route is."""
    digest = _propose()
    client = _as(app, _ALICE)
    client.post(
        "/proposals/skill/cold-quench",
        json={"content_hash": digest, "accepted": False, "reason": "too narrow"},
    )

    again = client.post(
        "/proposals/skill/cold-quench", json={"content_hash": digest, "accepted": True}
    )

    assert again.status_code == 409
    assert "already rejected" in again.json()["detail"]
    assert "POST /skills/mine" in again.json()["detail"], (
        "a refusal that does not name the way forward is a dead end"
    )
    assert client.get("/skills/mine").json()["skills"] == []


def test_a_deployment_that_keeps_no_store_refuses_the_acceptance_rather_than_recording_it(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 rather than an accepted proposal whose skill went nowhere.

    The same distinction `skills.py` draws: a confident answer about a mechanism that is not
    running is worse than an error, and here it would be a person's decision recorded as effective
    when it changed nothing.
    """

    async def _none() -> Any:
        return None

    digest = _propose()
    monkeypatch.setattr(proposal_routes, "turn_store", _none)
    client = _as(app, _ALICE)

    refused = client.post(
        "/proposals/skill/cold-quench", json={"content_hash": digest, "accepted": True}
    )

    assert refused.status_code == 503
    assert [row["state"] for row in client.get("/proposals").json()["proposals"]] == ["open"]
