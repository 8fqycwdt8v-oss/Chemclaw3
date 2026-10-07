"""The four routes that are the whole of a chemist's control over their own skills.

`D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved` exempts the personal tier from
review on conditions that are route behaviour: a person can see and remove what acts on their
turns, and nobody else's turn can reach it. This drives the surface (scoping, 4xx refusals, the
two bounds); `tests/test_local_skills.py` drives the tier itself.
"""

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.langgraph_agent import shipped_skill_names
from chemclaw.agent.local_skills import save_local_skill
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import skills as skills_routes
from chemclaw.core.config import settings

_ALICE = Principal(oid="u-alice", upn="alice@example.com", roles=frozenset())
_BOB = Principal(oid="u-bob", upn="bob@example.com", roles=frozenset())


def _body(name: str = "my-workup", note: str = "Quench cold.") -> str:
    """A well-formed `SKILL.md`, frontmatter included — what a person posts."""
    return f"---\nname: {name}\ndescription: how I work up a Suzuki\n---\n\n{note}\n"


def _no_connectors(profile: str | None = None) -> list[object]:
    """No connector session: nothing here reaches a capability server."""
    return []


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryStore:
    """A real `BaseStore` in place of the deployment's `AsyncPostgresStore`.

    Patched at `turn_store`; `InMemoryStore` satisfies the same contract. A fixture so the 503 case
    can patch it back to `None`.
    """
    backing = InMemoryStore()

    async def _store() -> Any:
        return backing

    monkeypatch.setattr(skills_routes, "turn_store", _store)
    return backing


@pytest.fixture
def app(store: InMemoryStore) -> FastAPI:
    """The real front door over a store that answers — one app per test, so nothing leaks."""
    return create_app(connector_factory=_no_connectors, graph_factory=lambda *a, **k: None)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """A client arriving as alice."""
    return _as(app, _ALICE)


def _as(app: FastAPI, principal: Principal) -> TestClient:
    """The same app, arriving as somebody else."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


def test_a_chemist_lists_reads_and_removes_their_own_skills(client: TestClient) -> None:
    """The licence condition end to end: save, see it, read it verbatim, take it away."""
    saved = client.post("/skills/mine", json={"body": _body()})
    assert saved.status_code == 200, saved.text

    assert client.get("/skills/mine").json()["skills"] == ["my-workup"]
    read = client.get("/skills/mine/my-workup")
    assert read.status_code == 200
    assert read.json()["body"] == _body(), "a person must be shown byte-for-byte what a turn reads"

    gone = client.delete("/skills/mine/my-workup")
    assert gone.status_code == 200
    assert gone.json()["skills"] == [], "the delete answers with what is left"
    assert client.get("/skills/mine/my-workup").status_code == 404


def test_one_chemists_skills_are_invisible_to_another(app: FastAPI, client: TestClient) -> None:
    """Owner-scoped by construction: no parameter names whose tier is touched.

    Driven rather than read off the handlers, because "there is no path parameter" is the kind of
    property that survives a refactor in the prose and not in the code.
    """
    client.post("/skills/mine", json={"body": _body()})

    bob = _as(app, _BOB)
    assert bob.get("/skills/mine").json()["skills"] == []
    assert bob.get("/skills/mine/my-workup").status_code == 404
    assert bob.delete("/skills/mine/my-workup").status_code == 404

    # And alice still holds hers: bob's 404 was an absence, not a deletion.
    assert _as(app, _ALICE).get("/skills/mine").json()["skills"] == ["my-workup"]


@pytest.mark.parametrize(
    ("body", "because"),
    [
        ("no frontmatter at all", "a document with no frontmatter is not a skill"),
        ("---\nname: x\n---\n\nbody\n", "a skill with no description is skipped by the loader"),
        ("---\ndescription: d\n---\n\nbody\n", "a skill with no name has no directory"),
        ("---\nname: a/b\ndescription: d\n---\n\nbody\n", "a '/' is the traversal shape"),
        ("---\nname: .hidden\ndescription: d\n---\n\nbody\n", "a leading '.' is the other one"),
        ('---\nname: "a\\0b"\ndescription: d\n---\n\nb\n', "a NUL reaches the driver as a 500"),
        ("---\nname: 'a b'\ndescription: d\n---\n\nb\n", "whitespace is not a directory name"),
        ("---\nname: x\ndescription: d\nnope: 1\n---\n\nb\n", "an invented key is a typo"),
    ],
)
def test_a_document_that_is_not_a_skill_is_refused_by_name(
    client: TestClient, body: str, because: str
) -> None:
    """422 rather than a stored file the listing then skips, or a 500 from the driver.

    A NUL byte would otherwise fail at the driver, far from the field that carried it.
    """
    response = client.post("/skills/mine", json={"body": body})

    assert response.status_code == 422, f"{because}: got {response.status_code} {response.text}"
    assert client.get("/skills/mine").json()["skills"] == [], "a refused skill was stored anyway"


def test_a_personal_skill_may_not_take_a_shipped_skills_name(client: TestClient) -> None:
    """409, because a collision silently decides which of two documents a model is given.

    The reverse collision — a shipped skill arriving after a personal one — is decided in
    `tests/test_local_skills.py::test_a_reviewed_skill_wins_a_name_a_personal_one_also_claims`.
    """
    contested = sorted(shipped_skill_names())[0]

    response = client.post("/skills/mine", json={"body": _body(contested)})

    assert response.status_code == 409, response.text
    assert contested in response.json()["detail"]
    assert client.get("/skills/mine").json()["skills"] == []


def test_the_tier_is_bounded_in_both_currencies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row cap and a size cap, because either alone is unbounded in the other.

    The row cap bounds prefix spend: every personal skill's name and description sit in each of its
    owner's turns. Refused rather than evicted (authored judgment is never dropped silently), and a
    replacement is allowed at the cap.
    """
    monkeypatch.setattr(settings, "agent_local_skills_max", 3)
    monkeypatch.setattr(settings, "agent_local_skill_max_chars", 200)

    for index in range(3):
        assert (
            client.post("/skills/mine", json={"body": _body(f"skill-{index}")}).status_code == 200
        )

    over = client.post("/skills/mine", json={"body": _body("skill-3")})
    assert over.status_code == 409, over.text
    assert "3" in over.json()["detail"]

    replacing = client.post("/skills/mine", json={"body": _body("skill-1", note="Quench warm.")})
    assert replacing.status_code == 200, "a replacement is not a new row and must not be refused"
    assert client.get("/skills/mine/skill-1").json()["body"].endswith("Quench warm.\n")

    long = client.post("/skills/mine", json={"body": _body("skill-1", note="x" * 300)})
    assert long.status_code == 422
    assert "200" in long.json()["detail"]
    assert client.get("/skills/mine").json()["skills"] == ["skill-0", "skill-1", "skill-2"]


def test_a_deployment_that_keeps_no_store_says_so_rather_than_answering_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 on every route rather than a confident empty answer.

    "You have no skills" and "this deployment cannot keep any" differ; all four routes, so a save
    never appears to succeed beside a 503 listing.
    """

    async def _none() -> Any:
        return None

    monkeypatch.setattr(skills_routes, "turn_store", _none)
    app = create_app(connector_factory=_no_connectors, graph_factory=lambda *a, **k: None)
    app.dependency_overrides[require_principal] = lambda: _ALICE
    client = TestClient(app)

    assert client.get("/skills/mine").status_code == 503
    assert client.post("/skills/mine", json={"body": _body()}).status_code == 503
    assert client.get("/skills/mine/my-workup").status_code == 503
    assert client.delete("/skills/mine/my-workup").status_code == 503


def test_the_listing_route_answers_for_a_tier_larger_than_one_page(
    client: TestClient, store: InMemoryStore
) -> None:
    """The listing route answers for a tier larger than one page.

    `BaseStore.asearch` defaults to 10 results; skills beyond the page would be invisible and
    undeletable.
    """
    names = [f"skill-{index:02d}" for index in range(12)]
    for name in names:
        asyncio.run(save_local_skill(store, _ALICE.oid, name, _body(name)))

    assert client.get("/skills/mine").json()["skills"] == names
    assert client.delete(f"/skills/mine/{names[11]}").status_code == 200


def test_a_padded_oid_saves_where_the_turn_reads(app: FastAPI, store: InMemoryStore) -> None:
    """The route keys on `principal.oid` and the turn on the stripped actor: they must be one key.

    A dev or configured principal `' u-alice '` saved under the padded namespace while her turns
    mounted `'u-alice'`, so the skill never acted. `Principal` strips at construction.
    """
    from chemclaw.agent.local_skills import list_local_skills

    padded = Principal(oid=" u-alice ", upn="alice@example.com", roles=frozenset())
    saved = _as(app, padded).post("/skills/mine", json={"body": _body()})
    assert saved.status_code == 200, saved.text

    assert asyncio.run(list_local_skills(store, "u-alice")) == ["my-workup"]


def test_a_blank_oid_is_not_an_identity() -> None:
    """Stripped to nothing is refused, not admitted as the empty principal."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Principal(oid="   ")
