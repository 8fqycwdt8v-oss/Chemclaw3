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

    `composed._IN_MEMORY` is a process singleton, which is correct for a CLI or dev process and
    wrong for a test module where every test stores `(u-alice, "ranking")`. It did not show while a
    `save` replaced the whole row: each test overwrote the last one's approval. Now that a save
    carries an approval forward — because the agent's write must not erase a person's decision —
    the leak is visible, and one test's approval would authorize the next one's document.
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
    """An approval that names no steps authorizes whatever the name currently contains.

    That is `D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` one layer over, and
    it is why this route exists at all rather than the POST standing alone: the person deciding has
    to be shown the steps, which of them cost compute, and the fingerprint they are deciding about.
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


def test_saving_a_revision_lapses_the_approval_without_erasing_who_gave_it() -> None:
    """A re-compose stops the approval working and leaves the record of it standing.

    **Two properties, and the earlier version of this test asserted the wrong one.** It required the
    in-memory backend to *clear* `approved_fingerprint` on save and called that "lapsing exactly as
    the SQL one does" — which the SQL one does not do: `_UPSERT` names no approval column, so the
    stored row keeps it and the lapse comes from the fingerprint no longer matching. Driven against
    both backends, the same call sequence left Postgres approved-by-Alice-at-the-old-hash and memory
    blank, which is the divergence that sentence denied.

    What the two must agree on, and now do: the *effect* lapses (`approved` is derived and goes
    false), and the person's recorded decision is not erased — an agent's write must not be able to
    delete the audit of a human's. What a reader must therefore not do is render `approved_by`
    alone, which is why `approved` ships as a field.
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
    """Discovery, which the feature shipped without and a `BACKLOG.md` row recorded.

    The only way to find a name was the refusal `run_composed_workflow` gives an unknown one, which
    works once you have already guessed wrong. A listing was deferred because a third *tool* costs
    prompt prefix on every model call; a route costs none, which is why it belongs here.
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
