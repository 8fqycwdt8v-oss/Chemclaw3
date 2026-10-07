"""The only path by which a composed workflow's durable jobs become runnable.

`D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction` refuses `job` steps in an
agent-composed workflow until a person approves; these routes are that person. Three facts: only a
person can approve (no tool can), the approval binds to the version shown (409 otherwise), and it
is scoped to the caller's own workflows.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.core.config import settings
from chemclaw.durable.template_job import template_fingerprint
from chemclaw.templates import composed
from chemclaw.templates.composed import ComposedWorkflow, default_composed_store
from chemclaw.templates.manifest import Template

_ALICE = Principal(oid="u-alice", upn="alice@example.com", roles=frozenset())
_BOB = Principal(oid="u-bob", upn="bob@example.com", roles=frozenset())


def _no_connectors(profile: str | None = None) -> list[object]:
    """No connector session: nothing here reaches a capability server."""
    return []


def _app() -> FastAPI:
    """The front door over the real store — the wiring these routes' claims are about."""
    return create_app(connector_factory=_no_connectors, graph_factory=lambda *a, **k: None)


def _client(app: FastAPI, principal: Principal) -> TestClient:
    """A client whose every request arrives as `principal`."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


def _document(name: str = "ranking") -> Template:
    """A composed workflow with one durable job step — the case an approval is about."""
    return Template.model_validate(
        {
            "name": name,
            "summary": "Rank the species and say which dominates.",
            "inputs": [{"name": "smiles", "type": "string", "description": "the molecule"}],
            "steps": [
                {"id": "rank", "kind": "job", "job": "rank_species", "arguments": {}},
                {"id": "say", "kind": "agent", "prompt": "which one: ${steps.rank.result}"},
            ],
        }
    )


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """A *fresh* in-memory backend per test — a real backend, so these tests need no database.

    `composed._IN_MEMORY` is a process singleton and a save carries an approval forward, so a shared
    one would let one test's approval authorize the next test's document.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(composed, "_IN_MEMORY", composed.InMemoryComposedStore())


def _store(owner: str, document: Template, name: str = "ranking") -> None:
    """Put one workflow in the store as `owner` would have composed it."""
    asyncio.run(
        default_composed_store().save(
            ComposedWorkflow(owner=owner, name=name, summary="s", document=document)
        )
    )


def test_the_read_shows_what_approving_would_authorize() -> None:
    """The read shows what approving would authorize.

    The person deciding must see the steps, which cost compute, and the fingerprint they decide on.
    """
    document = _document()
    _store(_ALICE.oid, document)

    body = _client(_app(), _ALICE).get("/workflows/ranking").json()

    assert [step["id"] for step in body["steps"]] == ["rank", "say"]
    assert body["job_steps"] == ["rank"]
    assert body["fingerprint"] == template_fingerprint(document)
    assert body["approved_fingerprint"] == ""
    # **The step ids are not the procedure**, and returning them alone is an approval that names no
    # job. What the widening's own justification promises the approver is the job's *name* and its
    # *arguments*, so those are what the screen has to carry.
    job = next(step for step in body["steps"] if step["id"] == "rank")
    assert job["calls"] == "rank_species"
    assert job["kind"] == "job"
    reasoning = next(step for step in body["steps"] if step["id"] == "say")
    assert "which one" in reasoning["prompt"]
    # And where it came from. The column existed from migration 100 and nothing in `src/` selected
    # it, so "a workflow that later looks wrong can be traced back to the conversation that
    # produced it" was a promise only somebody holding a psql prompt could keep.
    assert "composed_in_session" in body


def test_approving_the_version_that_was_shown_authorizes_it() -> None:
    """The happy path, end to end through the real app and the real store."""
    document = _document()
    _store(_ALICE.oid, document)
    client = _client(_app(), _ALICE)

    shown = client.get("/workflows/ranking").json()["fingerprint"]
    response = client.post("/workflows/ranking/approval", json={"fingerprint": shown})

    assert response.status_code == 204
    stored = asyncio.run(default_composed_store().get(_ALICE.oid, "ranking"))
    assert stored is not None
    assert stored.approved_fingerprint == template_fingerprint(document)
    # The row names the person, which is how this system audits a human decision — no `AuditEvent`
    # is written by any route, and the domain row is the record (`plan_approvals.actor`,
    # `pending_requests.answered_by`, `effects.approved_by` are each the same shape).
    assert stored.approved_by == _ALICE.oid
    # And it is readable: a column written and never selected is an attribution nothing can see,
    # which is the mirror of the defect D-2026-08-26 names. The GET is that reader.
    assert stored.approved_at is not None
    assert client.get("/workflows/ranking").json()["approved_at"] is not None


def test_approving_a_version_that_is_no_longer_current_is_a_409() -> None:
    """The binding is the control: a workflow that changed after being shown is a different one.

    Otherwise a client could display one version and approve another.
    """
    _store(_ALICE.oid, _document())
    client = _client(_app(), _ALICE)

    response = client.post("/workflows/ranking/approval", json={"fingerprint": "a-stale-hash"})

    assert response.status_code == 409
    stored = asyncio.run(default_composed_store().get(_ALICE.oid, "ranking"))
    assert stored is not None and stored.approved_fingerprint == ""


def test_re_composing_lapses_the_approval_with_nothing_having_to_clear_it() -> None:
    """The property that makes this an approval of a *version* rather than of an actor.

    Saving a different document under the same name makes the stored approval stop matching by
    itself.
    """
    _store(_ALICE.oid, _document())
    client = _client(_app(), _ALICE)
    shown = client.get("/workflows/ranking").json()["fingerprint"]
    client.post("/workflows/ranking/approval", json={"fingerprint": shown})

    widened = Template.model_validate(
        {
            "name": "ranking",
            "summary": "Now it does two.",
            "inputs": [{"name": "smiles", "type": "string", "description": "the molecule"}],
            "steps": [
                {"id": "rank", "kind": "job", "job": "rank_species", "arguments": {}},
                {"id": "more", "kind": "job", "job": "rank_species", "arguments": {}},
                {"id": "say", "kind": "agent", "prompt": "${steps.more.result}"},
            ],
        }
    )
    _store(_ALICE.oid, widened)

    after = client.get("/workflows/ranking").json()
    assert after["approved_fingerprint"] != after["fingerprint"]
    assert after["job_steps"] == ["rank", "more"]


def test_another_chemists_workflow_is_not_found_rather_than_forbidden() -> None:
    """Owner scoping *is* the authorization here, so there is no 403 to reach.

    Workflows are keyed `(owner, name)` and both routes resolve against the caller's rows. Asserted
    for reads and writes.
    """
    _store(_ALICE.oid, _document())
    client = _client(_app(), _BOB)

    assert client.get("/workflows/ranking").status_code == 404
    assert client.post("/workflows/ranking/approval", json={"fingerprint": "x"}).status_code == 404
    stored = asyncio.run(default_composed_store().get(_ALICE.oid, "ranking"))
    assert stored is not None and stored.approved_by == ""


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/workflows/ranking"), ("POST", "/workflows/ranking/approval")],
)
def test_both_routes_are_behind_the_authentication_gate(method: str, path: str) -> None:
    """Both routes are behind the authentication gate.

    `tests/test_route_auth_coverage.py` walks the whole table; named here because a missing
    `CurrentUser` skips authentication and the per-principal rate budget at once.
    """
    from chemclaw.api.routes import workflows

    handler = workflows.get_workflow if method == "GET" else workflows.approve_workflow
    annotations = handler.__annotations__

    assert "principal" in annotations, f"{path} does not take an authenticated caller"


def test_nothing_the_agent_can_call_approves_a_workflow() -> None:
    """**The control this whole seam rests on**, asserted over the surface rather than believed.

    No registered tool writes an approval, so a workflow cannot approve itself. Scanned by
    behaviour, not name. Reading `approved_fingerprint` is allowed — `run_composed_workflow` must
    read it to enforce it; calling `approve` or setting the column is not.
    """
    import inspect

    from chemclaw.agent.chemclaw_agent import available_tool_names
    from chemclaw.core.tool_registry import registered_tools

    assert "approve_composed_workflow" not in available_tool_names()

    writing = [
        fn.__name__
        for fn in registered_tools()
        for source in [inspect.getsource(fn)]
        if ".approve(" in source or "approved_fingerprint=" in source or "approved_by=" in source
    ]
    assert writing == [], f"these agent tools write the approval: {writing}"


def test_the_approval_column_is_not_writable_through_the_compose_path() -> None:
    """A save must not be able to set what only a person may set.

    `_UPSERT` and `_APPROVE` are separate statements so the agent's write shares no column with the
    human decision; asserted on the SQL.
    """
    from chemclaw.templates.composed import PostgresComposedStore

    assert "approved_fingerprint" not in PostgresComposedStore._UPSERT
    assert "approved_by" not in PostgresComposedStore._UPSERT
    assert "approved_fingerprint" in PostgresComposedStore._APPROVE


def test_saving_a_revision_lapses_the_approval_without_erasing_who_gave_it() -> None:
    """A re-compose stops the approval working and leaves the record of it standing.

    Both backends must agree: `approved` (derived) goes false because the fingerprint no longer
    matches, and the recorded decision (`approved_by`) is not erased by the agent's write. Readers
    must therefore render `approved`, not `approved_by` alone.
    """
    _store(_ALICE.oid, _document())
    client = _client(_app(), _ALICE)
    client.post(
        "/workflows/ranking/approval",
        json={"fingerprint": client.get("/workflows/ranking").json()["fingerprint"]},
    )
    approved = client.get("/workflows/ranking").json()
    assert approved["approved"] is True
    assert approved["approved_by"] == _ALICE.oid

    # A save is the agent's write. It carries no approval and cannot clear one either. A genuinely
    # different document, because the earlier version of this test re-saved a byte-identical one —
    # which has the same fingerprint, so it could only ever have been asserting the clearing.
    _store(
        _ALICE.oid,
        Template.model_validate(
            {
                "name": "ranking",
                "summary": "Now it does something else.",
                "inputs": [{"name": "smiles", "type": "string", "description": "the molecule"}],
                "steps": [
                    {"id": "rank", "kind": "job", "job": "sample_conformers", "arguments": {}},
                    {"id": "say", "kind": "agent", "prompt": "which one: ${steps.rank.result}"},
                ],
            }
        ),
    )
    stored = asyncio.run(default_composed_store().get(_ALICE.oid, "ranking"))
    after = client.get("/workflows/ranking").json()

    assert stored is not None
    assert stored.approved_fingerprint == approved["fingerprint"], (
        "the agent's write must not erase the record of a person's decision"
    )
    assert after["approved"] is False, "and it must not leave that decision in force either"
    assert after["approved_fingerprint"] != after["fingerprint"]


def test_the_listing_is_how_a_chemist_finds_a_workflow_they_forgot() -> None:
    """The listing is how a chemist finds a workflow they forgot.

    A route rather than a third tool, because a tool costs prompt prefix on every model call.
    """
    _store(_ALICE.oid, _document(), "ranking")
    _store(_ALICE.oid, _document("reads"), "reads")

    body = _client(_app(), _ALICE).get("/workflows").json()

    assert {row["name"] for row in body["workflows"]} == {"ranking", "reads"}
    assert body["truncated"] is False
    assert all(row["job_steps"] == ["rank"] for row in body["workflows"])


def test_the_listing_reports_approval_as_it_would_be_enforced_not_as_it_is_stored() -> None:
    """A row re-composed after approval is not approved, and the listing must not say it is.

    The stored column still holds the old fingerprint — that is what makes the lapse automatic — so
    a listing that reported the column would call a workflow approved while the next run refuses it.
    """
    _store(_ALICE.oid, _document())
    client = _client(_app(), _ALICE)
    client.post(
        "/workflows/ranking/approval",
        json={"fingerprint": client.get("/workflows/ranking").json()["fingerprint"]},
    )
    assert client.get("/workflows").json()["workflows"][0]["approved"] is True

    # Re-composed into a different procedure under the same name.
    widened = Template.model_validate(
        {
            "name": "ranking",
            "summary": "Now it does two.",
            "inputs": [{"name": "smiles", "type": "string", "description": "the molecule"}],
            "steps": [
                {"id": "rank", "kind": "job", "job": "rank_species", "arguments": {}},
                {"id": "more", "kind": "job", "job": "rank_species", "arguments": {}},
                {"id": "say", "kind": "agent", "prompt": "${steps.more.result}"},
            ],
        }
    )
    _store(_ALICE.oid, widened)

    assert client.get("/workflows").json()["workflows"][0]["approved"] is False


def test_the_listing_is_owner_scoped_like_everything_else_here() -> None:
    """A listing that leaked names would leak the procedures a colleague is working on."""
    _store(_ALICE.oid, _document())

    assert _client(_app(), _BOB).get("/workflows").json()["workflows"] == []


def test_the_bare_listing_path_is_not_matched_as_a_workflow_named_workflows() -> None:
    """Registration order is load-bearing: Starlette matches routes in the order they are added.

    Registered the other way round, `GET /workflows` resolves to `get_workflow(name="workflows")`
    and answers 404 for every caller — a listing that exists and can never be reached.
    """
    _store(_ALICE.oid, _document())

    response = _client(_app(), _ALICE).get("/workflows")

    assert response.status_code == 200
    assert "workflows" in response.json()
