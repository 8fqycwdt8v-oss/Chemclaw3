"""Who may write to a design.

Anyone may read a design and `opened_by` is kept as provenance, but writing is gated by ownership.
Both halves are needed: owner-scoped `design_id_for` stops two chemists' turns colliding on one
design, and the ownership gate stops an explicit `design_id` reaching another chemist's design
over a turn or HTTP. The gates degrade open in dev (`entra_required` off), so these tests run the
enforced posture a deployment has.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent import protocol_design_tools as tools
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import protocols as routes
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.metrics import METRICS
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
from chemclaw.protocols.result_store import InMemoryArmResultStore
from chemclaw.protocols.results import ArmResult
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


def test_a_turn_cannot_attach_results_to_another_chemists_design(
    monkeypatch: pytest.MonkeyPatch, enforced: None
) -> None:
    """A turn cannot attach results to another chemist's design.

    The results table is append-only and `attach_plate_results` its only writer; a stranger's
    numbers would stay there and be fitted by `suggest_next_experiment`.
    """
    store = InMemoryDesignStore()
    results = InMemoryArmResultStore()
    monkeypatch.setattr(tools, "_store", lambda: store)
    monkeypatch.setattr(tools, "default_arm_result_store", lambda: results)
    design = _design()
    asyncio.run(
        store.append(
            DESIGN_ID,
            design,
            run_checks(design),
            author_kind="agent",
            author=ALICE,
            parent_revision=0,
            change_note="drafted",
        )
    )
    measured = [ArmResult(arm_id="A1", outcome="conversion", value=88.0)]

    monkeypatch.setattr(tools, "require_actor", lambda: BOB)
    with pytest.raises(ChemclawError, match="belongs to another chemist"):
        asyncio.run(tools.attach_plate_results(design_id=DESIGN_ID, results=measured))
    assert asyncio.run(results.read(DESIGN_ID, 1)) == []

    monkeypatch.setattr(tools, "require_actor", lambda: ALICE)
    asyncio.run(tools.attach_plate_results(design_id=DESIGN_ID, results=measured))
    assert len(asyncio.run(results.read(DESIGN_ID, 1))) == 1


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
        {
            "status": "executed",
            "expected_revision": 1,
            "expected_status": "approved",
            "reason": "ran it",
        }
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
            json={
                "status": "approved",
                "expected_revision": 1,
                "expected_status": "draft",
                "reason": "the precedent holds",
            },
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
            json={
                "status": "abandoned",
                "expected_revision": 1,
                "expected_status": "draft",
                "reason": "SM decomposes",
            },
        )
    assert response.status_code == 204


@contextmanager
def _refusal_records() -> Iterator[list[logging.LogRecord]]:
    """Every `authz.refused` record `deps` emits, collected off that logger directly.

    Not `caplog`: `create_app()` replaces the handler pytest installs.
    """
    collected: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if getattr(record, "event", "") == "authz.refused":
                collected.append(record)

    logger = logging.getLogger("chemclaw.api.deps")
    handler = _Collect(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        yield collected
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize("path", ["status", "revisions"])
def test_a_refused_design_write_is_recorded_on_the_server_side(
    store: InMemoryDesignStore, enforced: None, path: str
) -> None:
    """A refused design write is recorded server-side with a metric and a WARNING.

    The 403 reveals the design exists; only the server log can show who is scanning design ids.
    """
    body: dict[str, Any] = (
        {
            "status": "executed",
            "expected_revision": 1,
            "expected_status": "approved",
            "reason": "ran it",
        }
        if path == "status"
        else _revision_body(_design())
    )
    before = METRICS.value("chemclaw_authz_refusals_total")
    with _refusal_records() as records:
        for client in _client(Principal(oid="mallory-oid")):
            response = client.post(f"/protocols/{DESIGN_ID}/{path}", json=body)
    assert response.status_code == 403
    assert METRICS.value("chemclaw_authz_refusals_total") == before + 1
    assert len(records) == 1
    assert getattr(records[0], "resource", "") == "design"
    assert getattr(records[0], "status", 0) == 403
    assert getattr(records[0], "actor", "") == "mallory-oid"


def test_a_write_to_an_unknown_design_is_recorded_as_well(
    store: InMemoryDesignStore, enforced: None
) -> None:
    """The other raise site in the same gate, and it was silent for the same reason."""
    before = METRICS.value("chemclaw_authz_refusals_total")
    with _refusal_records() as records:
        for client in _client(Principal(oid="mallory-oid")):
            response = client.post(
                "/protocols/design-nothing/status",
                json={
                    "status": "executed",
                    "expected_revision": 1,
                    "expected_status": "approved",
                    "reason": "ran it",
                },
            )
    assert response.status_code == 404
    assert METRICS.value("chemclaw_authz_refusals_total") == before + 1
    assert len(records) == 1
    assert getattr(records[0], "resource", "") == "design"
    assert getattr(records[0], "status", 0) == 404
