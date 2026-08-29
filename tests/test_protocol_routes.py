"""The HTTP surface an expert tailors a design on, driven through the real app.

The two claims worth the round trip. **A concurrent edit is a 409 carrying a machine-readable
code**, because the caller's next move is to re-read and re-apply rather than to retry — a
last-write-wins store would lose one of two chemists editing one plate, silently. And **a blocking
check does not refuse a human edit**, which is the one place this surface deliberately differs from
`draft_experiment_protocol`: a chemist editing towards a working protocol passes through invalid
intermediate states and can see the verdict, and a model cannot.

Authentication is not re-asserted per route here — `tests/test_route_auth_coverage.py` walks every
`APIRoute` the app declares and requires `require_principal` in its dependency tree, so these five
are covered the moment they are registered. What this file pins instead is that they *are*
registered as gatable routes and are not on that file's probe allowlist, which is the only way they
could slip out of that sweep.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import chemclaw.api.routes.protocols as routes
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.protocols.checks import run_checks
from chemclaw.protocols.models import (
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    ProtocolArm,
    Setpoints,
)
from chemclaw.protocols.store import InMemoryDesignStore
from tests.test_route_auth_coverage import _PROBE_ALLOWLIST

_OID = "chemist-a"
_DESIGN_ID = "design-http"

# The five routes this module registers, as the pair `test_route_auth_coverage` keys its sweep on.
_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("/protocols", "GET"),
        ("/protocols/{design_id}", "GET"),
        ("/protocols/{design_id}/revisions", "POST"),
        ("/protocols/{design_id}/diff", "GET"),
        ("/protocols/{design_id}/status", "POST"),
    }
)


def _request(**overrides: object) -> ExperimentRequest:
    fields: dict[str, object] = {"title": "SM-3 Suzuki", "goal": "couple the aryl chloride"}
    fields.update(overrides)
    return ExperimentRequest.model_validate(fields)


def _design(*, arms: int = 1, cited: bool = True, **overrides: object) -> ExperimentDesign:
    """A design that clears every blocking check unless `cited` is turned off."""
    fields: dict[str, object] = {
        "request": _request(),
        "arms": [ProtocolArm(arm_id=f"A{index}") for index in range(1, arms + 1)],
        "evidence": [
            EvidenceRef(kind="precedent", ref="reaction-1", summary="a run like this gave 72%"),
            EvidenceRef(kind="tool", tool="predict_pka", summary="the base is strong enough"),
        ]
        if cited
        else [],
    }
    fields.update(overrides)
    return ExperimentDesign.model_validate(fields)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryDesignStore:
    """A fresh real backend behind the routes — never the module-level singleton."""
    fresh = InMemoryDesignStore()
    monkeypatch.setattr(routes, "default_design_store", lambda: fresh)
    return fresh


@pytest.fixture
def client(store: InMemoryDesignStore) -> Iterator[TestClient]:
    """The real app with a known principal, so `author` on a stored edit is checkable."""
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: Principal(oid=_OID)
    with TestClient(app) as running:
        yield running
    app.dependency_overrides.clear()


def _seed(store: InMemoryDesignStore, design: ExperimentDesign, **kwargs: Any) -> None:
    """Put one revision in the store the way the agent tool would have."""
    kwargs.setdefault("kind", "protocol")
    kwargs.setdefault("author_kind", "agent")
    asyncio.run(store.append(_DESIGN_ID, design, run_checks(design), **kwargs))


# --- listing ------------------------------------------------------------------------------------


def test_an_empty_listing_is_an_empty_list_and_not_a_404(client: TestClient) -> None:
    """The policy the client's own `orEmpty()` expects of every listing here."""
    response = client.get("/protocols")
    assert response.status_code == 200
    assert response.json() == {"designs": []}


def test_the_listing_reports_the_header_row_of_each_design(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design(arms=3, cited=False), author="chemist-b", status="draft")

    row = client.get("/protocols").json()["designs"][0]
    assert row["design_id"] == _DESIGN_ID
    assert row["title"] == "SM-3 Suzuki"
    assert (row["head_revision"], row["arms"], row["status"]) == (1, 3, "draft")
    # `evidence_present` is the blocker an uncited design fails, and it is counted on the row.
    assert row["blockers"] == 1


def test_the_listing_refuses_a_status_that_is_not_a_status(client: TestClient) -> None:
    """422 rather than an empty list, which would read as "there are none"."""
    assert client.get("/protocols", params={"status": "in-progress"}).status_code == 422


def test_the_listing_filters_by_status(client: TestClient, store: InMemoryDesignStore) -> None:
    _seed(store, _design(), status="draft")
    assert len(client.get("/protocols", params={"status": "draft"}).json()["designs"]) == 1
    assert client.get("/protocols", params={"status": "approved"}).json()["designs"] == []


# --- reading one design ---------------------------------------------------------------------


def test_reading_an_unknown_design_is_a_404(client: TestClient) -> None:
    response = client.get("/protocols/design-nothing")
    assert response.status_code == 404
    assert "design-nothing" in response.json()["detail"]


def test_reading_a_design_returns_the_head_and_the_whole_history(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """The history comes back in the same call, because a document view renders both.

    Asking for them separately makes the two answers race whenever somebody else is editing.
    """
    _seed(store, _design(), author="chemist-b", change_note="drafted the protocol")
    _seed(
        store,
        _design(arms=2),
        author=_OID,
        author_kind="human",
        parent_revision=1,
        change_note="added a second arm",
    )

    body = client.get(f"/protocols/{_DESIGN_ID}").json()
    assert body["design_id"] == _DESIGN_ID
    assert body["revision"] == 2
    assert body["author_kind"] == "human"
    assert body["change_note"] == "added a second arm"
    assert len(body["design"]["arms"]) == 2
    assert body["summary"]["head_revision"] == 2
    assert [item["revision"] for item in body["history"]] == [1, 2]
    assert [item["author_kind"] for item in body["history"]] == ["agent", "human"]
    assert {check["check_id"] for check in body["checks"]} >= {"evidence_present"}


def test_the_revision_query_parameter_selects_an_earlier_revision(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design())
    _seed(store, _design(arms=4), parent_revision=1, change_note="widened it")

    first = client.get(f"/protocols/{_DESIGN_ID}", params={"revision": 1}).json()
    assert first["revision"] == 1 and len(first["design"]["arms"]) == 1
    head = client.get(f"/protocols/{_DESIGN_ID}").json()
    assert head["revision"] == 2 and len(head["design"]["arms"]) == 4


def test_asking_for_a_revision_that_does_not_exist_is_a_404(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design())
    response = client.get(f"/protocols/{_DESIGN_ID}", params={"revision": 9})
    assert response.status_code == 404
    assert "at revision 9" in response.json()["detail"]


# --- writing a human revision ---------------------------------------------------------------


def test_a_human_edit_is_stored_as_a_human_revision(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """`author_kind` is a column rather than a guess from the actor string.

    An agent draft and a human edit are the two sides of the signal this table exists to keep.
    """
    _seed(store, _design())
    edited = _design(base={"setpoints": Setpoints(temperature_c=60.0).model_dump()})

    response = client.post(
        f"/protocols/{_DESIGN_ID}/revisions",
        json={
            "document": edited.model_dump(mode="json"),
            "parent_revision": 1,
            "change_note": "60 C, the chloride decomposes at 80",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 2
    assert body["changed_paths"] == ["base.setpoints.temperature_c"]

    stored = asyncio.run(store.read(_DESIGN_ID))
    assert stored is not None
    assert stored.author_kind == "human"
    assert stored.author == _OID
    assert stored.change_note == "60 C, the chloride decomposes at 80"


def test_a_stale_parent_revision_is_a_409_naming_the_conflict(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """The caller's next move is to re-read and re-apply, so the code is machine-readable."""
    _seed(store, _design())
    _seed(store, _design(arms=2), parent_revision=1, change_note="somebody else got there first")

    response = client.post(
        f"/protocols/{_DESIGN_ID}/revisions",
        json={
            "document": _design(arms=3).model_dump(mode="json"),
            "parent_revision": 1,
            "change_note": "my edit, from the revision I had open",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "revision_conflict"
    assert "is at revision 2" in response.json()["detail"]["message"]
    # And the losing write stored nothing.
    assert [item.revision for item in asyncio.run(store.history(_DESIGN_ID))] == [1, 2]


def test_a_human_edit_with_a_blocking_check_is_accepted_and_told_about_it(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """The documented difference from `draft_experiment_protocol`, which refuses the same document.

    A chemist editing towards a working protocol passes through invalid intermediate states, and
    half a charge table is not a reason to lose their work.
    """
    _seed(store, _design())

    response = client.post(
        f"/protocols/{_DESIGN_ID}/revisions",
        json={
            "document": _design(cited=False).model_dump(mode="json"),
            "parent_revision": 1,
            "change_note": "dropped the citations while I rework them",
        },
    )
    assert response.status_code == 200
    blocking = [
        check["check_id"]
        for check in response.json()["checks"]
        if check["severity"] == "blocker" and not check["passed"]
    ]
    assert blocking == ["evidence_present"]
    assert asyncio.run(store.read(_DESIGN_ID)) is not None


def test_an_edit_of_a_revision_that_does_not_exist_is_a_404(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design())
    response = client.post(
        f"/protocols/{_DESIGN_ID}/revisions",
        json={
            "document": _design().model_dump(mode="json"),
            "parent_revision": 7,
            "change_note": "an edit of a revision nobody has",
        },
    )
    assert response.status_code == 404


def test_an_edit_that_names_no_parent_revision_is_refused(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """`parent_revision` is not defaulted to the head.

    An edit that did not say what it was derived from is precisely the write that silently discards
    somebody else's.
    """
    _seed(store, _design())
    for body in (
        {"document": _design().model_dump(mode="json"), "change_note": "no parent"},
        {
            "document": _design().model_dump(mode="json"),
            "parent_revision": 0,
            "change_note": "parent zero",
        },
        {"document": _design().model_dump(mode="json"), "parent_revision": 1, "change_note": ""},
    ):
        assert client.post(f"/protocols/{_DESIGN_ID}/revisions", json=body).status_code == 422


def test_correcting_the_ask_is_stored_as_a_request_and_graded_as_one(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """The `kind` and the check stage come from the document, not from which route was called.

    This route is the artefact a chemist corrects *before* the expensive work, so a design holding
    only the ask reaches it — and hard-coding `kind="protocol"` recorded the correction as a
    protocol revision, flipped a design with no procedure in it to `draft`, and reported
    `is_a_protocol` and `evidence_present` as blockers on the normal path. A blocker that fires
    where nothing is wrong is a blocker a reader learns to ignore, which is the property the one
    real blocker depends on.
    """
    ask = _design(arms=0, cited=False)
    _seed(store, ask, kind="request", status="requested")

    response = client.post(
        f"/protocols/{_DESIGN_ID}/revisions",
        json={
            "document": _design(
                arms=0, cited=False, request=_request(notes="100 mg, not 5 g")
            ).model_dump(mode="json"),
            "parent_revision": 1,
            "change_note": "corrected what I had read as the scale",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 2
    assert body["changed_paths"] == ["request.notes"]

    graded = {check["check_id"]: check for check in body["checks"]}
    # Graded at the request stage: the protocol-only checks say what they are waiting for rather
    # than failing about a procedure that does not exist yet.
    assert graded["is_a_protocol"]["severity"] == "note"
    assert "not checked yet" in graded["is_a_protocol"]["detail"]
    assert graded["evidence_present"]["severity"] == "note"
    assert [
        check["check_id"]
        for check in body["checks"]
        if check["severity"] == "blocker" and not check["passed"]
    ] == []

    stored = asyncio.run(store.read(_DESIGN_ID))
    assert stored is not None and stored.kind == "request"
    summary = asyncio.run(store.summary(_DESIGN_ID))
    # Correcting an ask does not make the design a draft; only a procedure does.
    assert summary is not None and summary.status == "requested"


def test_editing_a_design_that_has_a_procedure_is_stored_and_graded_as_a_protocol(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """The other direction of the same derivation, so it cannot pass by always saying "request"."""
    _seed(store, _design(), status="draft")

    response = client.post(
        f"/protocols/{_DESIGN_ID}/revisions",
        json={
            "document": _design(arms=2).model_dump(mode="json"),
            "parent_revision": 1,
            "change_note": "added the second arm",
        },
    )
    assert response.status_code == 200

    graded = {check["check_id"]: check for check in response.json()["checks"]}
    assert graded["is_a_protocol"]["severity"] == "blocker"
    assert graded["is_a_protocol"]["passed"] is True
    assert "2 arm(s)" in graded["is_a_protocol"]["detail"]
    assert graded["evidence_present"]["severity"] == "blocker"
    assert "not checked yet" not in graded["evidence_present"]["detail"]

    stored = asyncio.run(store.read(_DESIGN_ID))
    assert stored is not None and stored.kind == "protocol"
    # And the history now reads as the two kinds it holds, which is what a document view renders.
    assert [item["kind"] for item in client.get(f"/protocols/{_DESIGN_ID}").json()["history"]] == [
        "protocol",
        "protocol",
    ]


def test_drafting_a_procedure_onto_a_requested_design_advances_it(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """A chemist who writes the procedure themselves moves the design, exactly as an agent would.

    The `requested` → `draft` transition belongs to the *document*, so it has to happen on the human
    path too — and it is the transition the hard-coded `kind="protocol"` used to fire on an edit
    that added no procedure at all.
    """
    _seed(store, _design(arms=0, cited=False), kind="request", status="requested")

    response = client.post(
        f"/protocols/{_DESIGN_ID}/revisions",
        json={
            "document": _design(arms=1).model_dump(mode="json"),
            "parent_revision": 1,
            "change_note": "wrote the first arm myself",
        },
    )
    assert response.status_code == 200

    stored = asyncio.run(store.read(_DESIGN_ID))
    assert stored is not None and stored.kind == "protocol"
    summary = asyncio.run(store.summary(_DESIGN_ID))
    assert summary is not None and summary.status == "draft"


# --- the lifecycle move ---------------------------------------------------------------------


def test_moving_a_designs_status_answers_204(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design(), status="draft")
    response = client.post(
        f"/protocols/{_DESIGN_ID}/status",
        json={"status": "approved", "expected_revision": 1, "reason": "looks right"},
    )
    assert response.status_code == 204
    assert response.content == b""

    summary = asyncio.run(store.summary(_DESIGN_ID))
    assert summary is not None and summary.status == "approved"


def test_the_reason_the_ui_makes_mandatory_is_actually_recorded(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """The reason the UI makes mandatory reaches the record.

    `Chemclaw3_ui` disables every status button until a reason is typed and confirms the move is
    "recorded against you with the reason you wrote" — and `set_status` took no `reason` at all, so
    a field validated to 2,000 characters was dropped on the way to a 204. The test above sent one
    and asserted only the status, which is how it stayed invisible.
    """
    _seed(store, _design(), status="draft")
    client.post(
        f"/protocols/{_DESIGN_ID}/status",
        json={
            "status": "abandoned",
            "expected_revision": 1,
            "reason": "the SM decomposes above 40 C",
        },
    )

    events = asyncio.run(store.status_history(_DESIGN_ID))
    assert len(events) == 1
    assert events[0].reason == "the SM decomposes above 40 C"
    assert events[0].actor == _OID
    assert events[0].revision == 1


def test_reading_a_design_carries_who_signed_off_on_which_revision(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """Stored is not read.

    A record nothing can reach answers no question, so the sign-off comes back beside the document
    — the same round trip the revision history already takes.
    """
    _seed(store, _design(), status="draft")
    client.post(
        f"/protocols/{_DESIGN_ID}/status",
        json={"status": "approved", "expected_revision": 1, "reason": "80 C is right"},
    )

    body = client.get(f"/protocols/{_DESIGN_ID}").json()
    assert [event["status"] for event in body["status_history"]] == ["approved"]
    assert body["status_history"][0]["revision"] == 1
    assert body["status_history"][0]["reason"] == "80 C is right"
    assert body["status_history"][0]["actor"] == _OID


def test_a_sign_off_against_a_stale_revision_is_a_409(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """The approver names the revision they read, and a colleague's save refuses the sign-off.

    Without it `set_status` stamped whatever `head_revision` had become by the time it ran, so a
    chemist who opened revision 1, thought about it, and clicked Approve while somebody saved
    revision 2 signed a document they had never seen — and the status-event table, which exists to
    say *which* document was signed, recorded revision 2 with their name on it. This is the same
    control, the same status and the same machine-readable code as an edit against a stale parent.
    """
    _seed(store, _design(), status="draft")
    _seed(store, _design(arms=2), status="draft", parent_revision=1)

    response = client.post(
        f"/protocols/{_DESIGN_ID}/status",
        json={"status": "approved", "expected_revision": 1, "reason": "read revision 1"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "revision_conflict"
    # And nothing was recorded: a refused sign-off is not a quieter sign-off.
    assert asyncio.run(store.status_history(_DESIGN_ID)) == []

    named = client.post(
        f"/protocols/{_DESIGN_ID}/status",
        json={"status": "approved", "expected_revision": 2, "reason": "read revision 2"},
    )
    assert named.status_code == 204
    assert asyncio.run(store.status_history(_DESIGN_ID))[0].revision == 2


def test_a_sign_off_that_names_no_revision_is_refused(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    """422 rather than a default to the head — the default is what the field exists to remove."""
    _seed(store, _design(), status="draft")
    response = client.post(f"/protocols/{_DESIGN_ID}/status", json={"status": "approved"})
    assert response.status_code == 422


def test_moving_an_unknown_designs_status_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/protocols/design-nothing/status", json={"status": "approved", "expected_revision": 1}
    )
    assert response.status_code == 404


def test_a_status_that_is_not_a_status_is_refused(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design())
    assert (
        client.post(
            f"/protocols/{_DESIGN_ID}/status",
            json={"status": "in-progress", "expected_revision": 1},
        ).status_code
        == 422
    )


# --- the diff ---------------------------------------------------------------------------------


def test_the_diff_route_reports_what_the_chemist_changed(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design())
    _seed(
        store,
        _design(base={"setpoints": Setpoints(temperature_c=60.0).model_dump()}),
        parent_revision=1,
        change_note="60 C",
    )

    body = client.get(f"/protocols/{_DESIGN_ID}/diff").json()
    assert (body["from_revision"], body["to_revision"]) == (1, 2)
    assert [change["path"] for change in body["changes"]] == ["base.setpoints.temperature_c"]
    assert body["changes"][0]["after"] == "60.0"


def test_the_diff_route_takes_both_endpoints_explicitly(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design())
    _seed(store, _design(arms=2), parent_revision=1, change_note="two")
    _seed(store, _design(arms=3), parent_revision=2, change_note="three")

    body = client.get(
        f"/protocols/{_DESIGN_ID}/diff", params={"from_revision": 2, "to_revision": 3}
    ).json()
    assert (body["from_revision"], body["to_revision"]) == (2, 3)
    assert all(change["kind"] == "added" for change in body["changes"])
    assert {change["path"] for change in body["changes"]} == {
        "arms.A3.arm_id",
        "arms.A3.control",
        "arms.A3.note",
        "arms.A3.replicate_of",
    }


def test_the_diff_route_404s_on_a_revision_that_does_not_exist(
    client: TestClient, store: InMemoryDesignStore
) -> None:
    _seed(store, _design())
    assert (
        client.get(f"/protocols/{_DESIGN_ID}/diff", params={"from_revision": 9}).status_code == 404
    )


# --- the authentication sweep -------------------------------------------------------------


def test_all_five_routes_are_inside_the_apps_authentication_sweep() -> None:
    """Not a second copy of `test_route_auth_coverage`.

    That file already requires `require_principal` in every `APIRoute`'s dependency tree. What is
    asserted here is the two ways these five could fall *outside* that sweep and look gated anyway:
    being registered as something other than an `APIRoute` (a `Mount` or a bare `Route` carries no
    dependency tree to inspect), or appearing on the probe allowlist that sweep waives.
    """
    registered = {
        (route.path, method)
        for route in create_app().routes
        if isinstance(route, APIRoute)
        for method in route.methods or set()
    }
    assert _ROUTES <= registered
    assert _ROUTES.isdisjoint(_PROBE_ALLOWLIST)
