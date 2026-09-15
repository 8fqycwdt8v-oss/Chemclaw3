"""The only path by which a composed workflow's durable jobs become runnable.

**This file is the control.** `D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction`
refuses a `job` step in a workflow the agent composed, because a template run has no session and so
nothing can put a human in front of an unreviewed launch. These routes are that human. If the tests
here do not hold, the widening is a hole rather than a control.

Three facts are checked separately because they are three different things: **only a person can
approve** (there is no tool, over the whole registered surface), **the approval binds to the
version that was shown** (a 409, not a silent approval of whatever it is now), and **it is scoped
to the caller's own workflows** (a name that is not yours is not found).
"""

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.core.config import settings
from chemclaw.durable.template_job import template_fingerprint
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
    """The in-memory backend, which is a real one, so these tests need no database."""
    monkeypatch.setattr(settings, "session_store", "memory")


def _store(owner: str, document: Template, name: str = "ranking") -> None:
    """Put one workflow in the store as `owner` would have composed it."""
    asyncio.run(
        default_composed_store().save(
            ComposedWorkflow(owner=owner, name=name, summary="s", document=document)
        )
    )


def test_the_read_shows_what_approving_would_authorize() -> None:
    """An approval that names no steps authorizes whatever the name currently contains.

    That is `D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` one layer over, and
    it is why this route exists at all rather than the POST standing alone: the person deciding has
    to be shown the steps, which of them cost compute, and the fingerprint they are deciding about.
    """
    document = _document()
    _store(_ALICE.oid, document)

    body = _client(_app(), _ALICE).get("/workflows/ranking").json()

    assert body["steps"] == ["rank", "say"]
    assert body["job_steps"] == ["rank"]
    assert body["fingerprint"] == template_fingerprint(document)
    assert body["approved_fingerprint"] == ""


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


def test_approving_a_version_that_is_no_longer_current_is_a_409() -> None:
    """The binding is the control: a workflow that changed after being shown is a different one.

    Without this, a client that rendered one version and posted another would silently approve the
    version it never displayed — which is the defect `decide_plan`'s own hash check exists for, and
    which `D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read` measured
    one layer up.
    """
    _store(_ALICE.oid, _document())
    client = _client(_app(), _ALICE)

    response = client.post("/workflows/ranking/approval", json={"fingerprint": "a-stale-hash"})

    assert response.status_code == 409
    stored = asyncio.run(default_composed_store().get(_ALICE.oid, "ranking"))
    assert stored is not None and stored.approved_fingerprint == ""


def test_re_composing_lapses_the_approval_with_nothing_having_to_clear_it() -> None:
    """The property that makes this an approval of a *version* rather than of an actor.

    A standing per-actor permission never lapses, so a workflow re-composed into something else
    inherits the approval granted to what it used to be. Driven here rather than asserted: save a
    different document under the same name, and the stored approval stops matching by itself.
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

    A composed workflow is keyed `(owner, name)` and both routes resolve against the caller's own
    rows — the same answer the store gives the agent, and one fewer branch than a 403 nobody can
    get to. Asserted in both directions, because a read that leaked the steps would be as bad as a
    write that approved them.
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
    """`tests/test_route_auth_coverage.py` walks the whole table; this says it of these two.

    Named here as well because forgetting `CurrentUser` skips authentication *and* the per-principal
    rate budget in one stroke, and a control whose gate is asserted only in aggregate is one nobody
    reads when they add the next route.
    """
    from chemclaw.api.routes import workflows

    handler = workflows.get_workflow if method == "GET" else workflows.approve_workflow
    annotations = handler.__annotations__

    assert "principal" in annotations, f"{path} does not take an authenticated caller"


def test_nothing_the_agent_can_call_approves_a_workflow() -> None:
    """**The control this whole seam rests on**, asserted over the surface rather than believed.

    `api/routes/plan.py::decide_plan` states the rule for plans — *"a model must never be able to
    authorize its own plan"* — and obtains it by not building the tool. This asserts that of the
    registered surface: no advertised tool writes an approval, so a workflow cannot approve itself.

    Scanned by *behaviour* and not by name: a tool called something innocuous that reached
    `ComposedStore.approve` would pass a name check and is exactly what this is for.

    **Writes, not reads.** `run_composed_workflow` reads `approved_fingerprint` and must — that
    read *is* the enforcement, and a scan that forbade it would be asking the gate not to look at
    the thing it gates on. What no tool may do is call `approve` or set the column, which is the
    difference between consulting a decision and making one.
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

    The separation is the control rather than tidiness: `_UPSERT` and `_APPROVE` are two statements
    on purpose, so the agent's write has no column in common with the human's decision. Asserted on
    the SQL, because folding them into one statement is the tempting simplification.
    """
    from chemclaw.templates.composed import PostgresComposedStore

    assert "approved_fingerprint" not in PostgresComposedStore._UPSERT
    assert "approved_by" not in PostgresComposedStore._UPSERT
    assert "approved_fingerprint" in PostgresComposedStore._APPROVE


def test_saving_a_revision_does_not_carry_the_approval_forward(monkeypatch: Any) -> None:
    """The in-memory backend must lapse an approval exactly as the SQL one does.

    Both are real backends, so a property that held in only one of them is a property this
    deployment does not have.
    """
    _store(_ALICE.oid, _document())
    client = _client(_app(), _ALICE)
    client.post(
        "/workflows/ranking/approval",
        json={"fingerprint": client.get("/workflows/ranking").json()["fingerprint"]},
    )

    # A save is the agent's write; it carries no approval, so the stored one is whatever it was.
    _store(_ALICE.oid, _document("ranking"))
    stored = asyncio.run(default_composed_store().get(_ALICE.oid, "ranking"))

    assert stored is not None
    assert stored.approved_fingerprint == ""
