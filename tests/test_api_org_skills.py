"""The six routes that are the whole of an administrator's control over the organisation's skills.

`tests/test_org_skills.py` drives the tier — the mount, the refusal, the version history. This
drives the surface: who may change it, who may see it, and that a refusal is recorded rather than
only answered.

**The read/write split is the file's subject.** `D-2026-09-05-the-gate-follows-behaviour-not-
knowledge` §3 makes inspectability the condition a skills tier holds its exemption under, and this
one is in the prompt of every turn every chemist takes — so the three reads must be open to any
authenticated caller and the three writes must not be. Both halves are asserted here, because
either one alone is a different system: open writes would be no gate, and closed reads would be a
behaviour change nobody it acts on can see.
"""

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import org_skills as org_routes
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.conftest import log_field

_ADMIN = Principal(oid="u-admin", upn="admin@example.com", roles=frozenset({"chemclaw.reviewer"}))
_ALICE = Principal(oid="u-alice", upn="alice@example.com", roles=frozenset())


def _body(name: str = "house-workup", note: str = "Quench cold.") -> str:
    """A well-formed `SKILL.md`, frontmatter included — what an administrator posts."""
    return f"---\nname: {name}\ndescription: how this house works one up\n---\n\n{note}\n"


def _no_connectors(profile: str | None = None) -> list[object]:
    """No connector session: nothing here reaches a capability server."""
    return []


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryStore:
    """A real `BaseStore` in place of the deployment's `AsyncPostgresStore`.

    Patched at `turn_store` rather than by configuring Postgres, for
    `tests/test_api_local_skills.py`'s reason: what these tests are about is the routes' behaviour
    over a store that answers.
    """
    backing = InMemoryStore()

    async def _store() -> Any:
        return backing

    monkeypatch.setattr(org_routes, "turn_store", _store)
    return backing


@pytest.fixture
def enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identity enforced and a privileged role named — the shape a deployment runs.

    Without this `_is_reviewer` returns True for everybody, because dev has no real roles and is
    open exactly as `authorize_tool` is. A role test that ran in dev mode would assert nothing, so
    the two write-refusal tests turn it on rather than inheriting it.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_privileged_roles", "chemclaw.reviewer")


@pytest.fixture
def app(store: InMemoryStore) -> FastAPI:
    """The real front door over a store that answers — one app per test, so nothing leaks."""
    return create_app(connector_factory=_no_connectors, graph_factory=lambda *a, **k: None)


def _as(app: FastAPI, principal: Principal) -> TestClient:
    """The same app, arriving as somebody."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


def test_an_administrator_publishes_reads_and_retires(app: FastAPI) -> None:
    """The loop end to end, from the surface a person actually uses."""
    admin = _as(app, _ADMIN)

    published = admin.post("/skills/org", json={"body": _body()})
    assert published.status_code == 200, published.text
    assert published.json()["name"] == "house-workup"

    assert admin.get("/skills/org").json()["skills"] == ["house-workup"]
    assert admin.get("/skills/org/house-workup").json()["body"] == _body()

    retired = admin.delete("/skills/org/house-workup")
    assert retired.status_code == 200
    assert retired.json()["skills"] == []


def test_every_chemist_may_read_what_acts_on_their_turns(app: FastAPI) -> None:
    """The exemption's condition: a tier in everybody's prompt is legible to everybody.

    Including the version list, which is the only place "what changed and who changed it" exists for
    a tier with no commit log — a rollback story an organisation has to take on trust is not one.
    """
    _as(app, _ADMIN).post("/skills/org", json={"body": _body()})
    alice = _as(app, _ALICE)

    assert alice.get("/skills/org").json()["skills"] == ["house-workup"]
    assert alice.get("/skills/org/house-workup").json()["body"] == _body()
    versions = alice.get("/skills/org/house-workup/versions")
    assert versions.status_code == 200
    assert [held["activated_by"] for held in versions.json()["versions"]] == ["u-admin"]


@pytest.mark.usefixtures("enforced")
def test_a_chemist_without_the_role_may_not_change_the_organisations_skills(
    app: FastAPI,
) -> None:
    """403 on all three writes, and the tier is unchanged by any of them."""
    _as(app, _ADMIN).post("/skills/org", json={"body": _body()})
    alice = _as(app, _ALICE)

    assert alice.post("/skills/org", json={"body": _body("theirs")}).status_code == 403
    assert (
        alice.post("/skills/org/house-workup/revert", json={"content_hash": "x"}).status_code == 403
    )
    assert alice.delete("/skills/org/house-workup").status_code == 403

    assert _as(app, _ADMIN).get("/skills/org").json()["skills"] == ["house-workup"]


@pytest.mark.usefixtures("enforced")
def test_a_refusal_is_recorded_and_not_only_answered(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """The half `api/routes/protocols.py` exists as the precedent for.

    `deps.record_refusal`'s own docstring records that five raise sites wrote zero log lines and
    zero metrics, so a scan was indistinguishable from ordinary traffic. A 403 nobody can count is
    the same gap on the newest gate.

    The log record is read as well as the counter, because the counter alone does not say *which*
    resource was refused — and the source literal is the thing that keeps
    `chemclaw_authz_refusals_total` from growing a series out of anything a caller sends.
    """
    before = METRICS.value("chemclaw_authz_refusals_total")
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.deps"):
        caplog.clear()
        assert _as(app, _ALICE).delete("/skills/org/house-workup").status_code == 403
    assert METRICS.value("chemclaw_authz_refusals_total") == before + 1
    record = next(r for r in caplog.records if getattr(r, "event", "") == "authz.refused")
    assert log_field(record, "resource") == "org-skill"
    assert log_field(record, "status") == 403


def test_a_bad_publication_is_one_call_away_from_the_bytes_that_stood_before(
    app: FastAPI,
) -> None:
    """The rollback property, driven through the surface an administrator reaches at 3am.

    Byte-identity rather than "the name still resolves", because the second passes on a
    re-authoring — which is the thing the version namespace exists to make unnecessary.
    """
    admin = _as(app, _ADMIN)
    good, bad = _body(note="Quench cold."), _body(note="Quench hot.")

    admin.post("/skills/org", json={"body": good})
    admin.post("/skills/org", json={"body": bad})
    assert admin.get("/skills/org/house-workup").json()["body"] == bad

    versions = admin.get("/skills/org/house-workup/versions").json()["versions"]
    restore = next(held for held in versions if held["body"] == good)

    reverted = admin.post(
        "/skills/org/house-workup/revert", json={"content_hash": restore["content_hash"]}
    )
    assert reverted.status_code == 200
    assert admin.get("/skills/org/house-workup").json()["body"] == good, "not byte-identical"


def test_a_revert_cannot_name_a_document_that_was_never_active(app: FastAPI) -> None:
    """404, and the active body untouched — the pointer only points at history."""
    admin = _as(app, _ADMIN)
    admin.post("/skills/org", json={"body": _body()})

    missed = admin.post(
        "/skills/org/house-workup/revert", json={"content_hash": "not-a-hash-we-hold"}
    )
    assert missed.status_code == 404
    assert admin.get("/skills/org/house-workup").json()["body"] == _body()


def test_retiring_leaves_the_history_and_stays_reversible(app: FastAPI) -> None:
    """A retire is not a shred: the versions survive it and any of them can come back."""
    admin = _as(app, _ADMIN)
    admin.post("/skills/org", json={"body": _body()})
    digest = admin.get("/skills/org/house-workup/versions").json()["versions"][0]["content_hash"]

    admin.delete("/skills/org/house-workup")
    assert admin.get("/skills/org/house-workup").status_code == 404
    assert admin.get("/skills/org/house-workup/versions").json()["versions"], "history went too"

    assert (
        admin.post("/skills/org/house-workup/revert", json={"content_hash": digest}).status_code
        == 200
    )
    assert admin.get("/skills/org").json()["skills"] == ["house-workup"]


def test_a_document_that_is_not_a_skill_is_refused(app: FastAPI) -> None:
    """The admission rules are the tier's, so this door has all four of them.

    `validated_skill` exists because two doors into the personal tier disagreed about what a skill
    is and the one that skipped the checks wrote all three bodies the other refused. A third door
    that re-implemented them would be the same defect with a longer fuse.
    """
    admin = _as(app, _ADMIN)

    assert admin.post("/skills/org", json={"body": "no frontmatter here"}).status_code == 422
    assert (
        admin.post(
            "/skills/org", json={"body": _body(note="x" * settings.agent_local_skill_max_chars)}
        ).status_code
        == 422
    )

    from chemclaw.agent.langgraph_agent import shipped_skill_names

    shipped = next(iter(shipped_skill_names()))
    assert admin.post("/skills/org", json={"body": _body(name=shipped)}).status_code == 409


def test_the_row_cap_is_refused_at_the_route(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """409 rather than a silent drop, because every row is in every chemist's prefix."""
    monkeypatch.setattr(settings, "agent_org_skills_max", 1)
    admin = _as(app, _ADMIN)

    assert admin.post("/skills/org", json={"body": _body("first")}).status_code == 200
    assert admin.post("/skills/org", json={"body": _body("second")}).status_code == 409
    assert admin.get("/skills/org").json()["skills"] == ["first"]


def test_the_tier_answers_unavailable_rather_than_empty(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503, not `[]`.

    A confident empty answer about a mechanism that is not running is the failure
    `api/routes/skills.py` records having had to delete twice from the UI: "this organisation keeps
    no skills" and "this deployment cannot keep any" are different facts.
    """

    async def _none() -> Any:
        return None

    monkeypatch.setattr(org_routes, "turn_store", _none)
    admin = _as(app, _ADMIN)

    assert admin.get("/skills/org").status_code == 503
    assert admin.post("/skills/org", json={"body": _body()}).status_code == 503
