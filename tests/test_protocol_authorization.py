"""Who may write to a design — the question nothing asked.

A design is a shared scientific artifact: anyone in the deployment may read one, and `opened_by` is
kept through offboarding because the person who opened it is part of its provenance. Writing to one
is a different question, and neither surface put it. Measured before this file existed:

- A second chemist's *turn* reached the first chemist's design, because `design_id_for` hashed the
  title, goal, transformation and mode and nothing about who was asking. He restructured her ask,
  demoted her `approved` header to `draft`, and replaced her two-arm plate with his own — while
  `status_history` still recorded her sign-off at revision 2.
- An unrelated principal with **no role at all** wrote `executed` into the status trail of somebody
  else's design over HTTP, and then landed a revision on it as its author.

Both halves are needed and neither works alone: owner-scoped ids stop the ordinary collision, and
the ownership gate stops an explicit `design_id` from reaching another chemist's design.

The gates degrade open in dev (`entra_required` off), exactly as `_is_reviewer` and every other
route does, so these tests run the **enforced** posture — which is the one a deployment has.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent import protocol_design_tools as tools
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import protocols as routes
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.checks import run_checks
from chemclaw.protocols.models import (
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    ProtocolArm,
    ProtocolBody,
    Setpoints,
    design_id_for,
)
from chemclaw.protocols.store import InMemoryDesignStore

ALICE = "alice-oid"
BOB = "bob-oid"
DESIGN_ID = "design-aaaaaaaaaaaa"

REQUEST = ExperimentRequest(title="SM-3 Suzuki", goal="hit 90% conversion", mode="single")


def _design() -> ExperimentDesign:
    """A design that clears every blocking check."""
    return ExperimentDesign(
        request=REQUEST,
        base=ProtocolBody(setpoints=Setpoints(temperature_c=80, time_h=16, solvent="2-MeTHF")),
        arms=[ProtocolArm(arm_id="A1")],
        evidence=[
            EvidenceRef(kind="precedent", ref="reaction-1", summary="a run like this gave 72%"),
            EvidenceRef(kind="tool", tool="predict_pka", summary="the base is strong enough"),
        ],
    )


@pytest.fixture
def enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The posture a deployment runs: real identity, and one named privileged role."""
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_privileged_roles", "reviewer")


# --- the id itself --------------------------------------------------------------------------


def test_one_ask_by_two_chemists_is_two_designs() -> None:
    """The collision that let a second chemist's turn land on the first chemist's design."""
    assert design_id_for(REQUEST, owner=ALICE) != design_id_for(REQUEST, owner=BOB)


def test_the_id_is_still_stable_for_one_chemist_across_sessions() -> None:
    """The property owner-scoping had to keep: restructuring the same ask reopens the design."""
    assert design_id_for(REQUEST, owner=ALICE) == design_id_for(REQUEST, owner=ALICE)
    assert design_id_for(REQUEST, owner=ALICE, salt="second") != design_id_for(REQUEST, owner=ALICE)


# --- the agent's write path -----------------------------------------------------------------


def test_a_turn_cannot_draft_onto_another_chemists_design(
    monkeypatch: pytest.MonkeyPatch, enforced: None
) -> None:
    """An explicit `design_id` is the half owner-scoping cannot close."""
    store = InMemoryDesignStore()
    monkeypatch.setattr(tools, "_store", lambda: store)
    design = _design()
    asyncio.run(
        store.append(
            DESIGN_ID,
            design,
            run_checks(design),
            kind="protocol",
            author_kind="agent",
            author=ALICE,
            parent_revision=0,
            change_note="drafted",
        )
    )

    monkeypatch.setattr(tools, "require_actor", lambda: BOB)
    with pytest.raises(ChemclawError, match="belongs to another chemist"):
        asyncio.run(
            tools.draft_experiment_protocol(
                design_id=DESIGN_ID,
                parent_revision=1,
                base=design.base,
                evidence=list(design.evidence),
                change_note="bob rewrites it",
            )
        )

    # Untouched: the store still holds exactly Alice's revision.
    header = asyncio.run(store.summary(DESIGN_ID))
    assert header is not None
    assert header.head_revision == 1
    assert header.opened_by == ALICE


def test_the_owner_can_still_draft_onto_their_own_design(
    monkeypatch: pytest.MonkeyPatch, enforced: None
) -> None:
    """The gate refuses another chemist, never the chemist whose design it is."""
    store = InMemoryDesignStore()
    monkeypatch.setattr(tools, "_store", lambda: store)
    design = _design()
    asyncio.run(
        store.append(
            DESIGN_ID,
            design,
            run_checks(design),
            kind="request",
            author_kind="agent",
            author=ALICE,
            parent_revision=0,
            change_note="structured the request",
        )
    )

    monkeypatch.setattr(tools, "require_actor", lambda: ALICE)
    asyncio.run(
        tools.draft_experiment_protocol(
            design_id=DESIGN_ID,
            parent_revision=1,
            base=design.base,
            evidence=list(design.evidence),
            arms=[ProtocolArm(arm_id="A1")],
            change_note="drafted the protocol",
        )
    )
    header = asyncio.run(store.summary(DESIGN_ID))
    assert header is not None and header.head_revision == 2


# --- the HTTP write path --------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryDesignStore:
    fresh = InMemoryDesignStore()
    monkeypatch.setattr(routes, "default_design_store", lambda: fresh)
    design = _design()
    asyncio.run(
        fresh.append(
            DESIGN_ID,
            design,
            run_checks(design),
            kind="protocol",
            author_kind="agent",
            author=ALICE,
            parent_revision=0,
            change_note="drafted",
        )
    )
    return fresh


def _client(principal: Principal) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: principal
    with TestClient(app) as running:
        yield running
    app.dependency_overrides.clear()


def _revision_body(design: ExperimentDesign) -> dict[str, Any]:
    return {
        "parent_revision": 1,
        "document": design.model_dump(mode="json"),
        "change_note": "an edit",
    }


@pytest.mark.parametrize("path", ["status", "revisions"])
def test_a_stranger_cannot_write_to_someone_elses_design(
    store: InMemoryDesignStore, enforced: None, path: str
) -> None:
    """No role at all, and not the owner: both writes are refused."""
    body: dict[str, Any] = (
        {"status": "executed", "expected_revision": 1, "reason": "ran it"}
        if path == "status"
        else _revision_body(_design())
    )
    for client in _client(Principal(oid="mallory-oid")):
        response = client.post(f"/protocols/{DESIGN_ID}/{path}", json=body)
    assert response.status_code == 403
    assert "another chemist" in response.json()["detail"]
    # Nothing was recorded — the trail is what a lab record rests on.
    assert asyncio.run(store.status_history(DESIGN_ID)) == []
    header = asyncio.run(store.summary(DESIGN_ID))
    assert header is not None and header.head_revision == 1


def test_the_owner_signs_off_on_their_own_design(
    store: InMemoryDesignStore, enforced: None
) -> None:
    """A chemist approves their own plate; that is the ordinary path, not a privilege."""
    for client in _client(Principal(oid=ALICE)):
        response = client.post(
            f"/protocols/{DESIGN_ID}/status",
            json={"status": "approved", "expected_revision": 1, "reason": "the precedent holds"},
        )
    assert response.status_code == 204
    events = asyncio.run(store.status_history(DESIGN_ID))
    assert [(event.status, event.actor) for event in events] == [("approved", ALICE)]


def test_a_reviewer_reaches_another_chemists_design(
    store: InMemoryDesignStore, enforced: None
) -> None:
    """The role that exists to reach other people's work reaches this too."""
    for client in _client(Principal(oid="carol-oid", roles=frozenset({"reviewer"}))):
        response = client.post(
            f"/protocols/{DESIGN_ID}/status",
            json={"status": "abandoned", "expected_revision": 1, "reason": "SM decomposes"},
        )
    assert response.status_code == 204
