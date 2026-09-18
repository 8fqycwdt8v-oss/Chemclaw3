"""The four routes that are the whole of a chemist's control over their own skills.

**This file is the control, the way `tests/test_api_workflows.py` is for an approval.**
`D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved` grants the personal tier its
exemption from review on conditions it states as requirements: a person can see what is acting on
their turns, remove it, and nobody else's turn can reach it. Three of those four conditions are
route behaviour, so if what is here does not hold, the exemption is a claim rather than a bargain.

`tests/test_local_skills.py` drives the tier itself — the mount, the refusal, the namespace. This
drives the surface: who is scoped to what, which refusals are 4xx rather than 500s, and the two
bounds that keep one person's judgment from becoming every turn's prefix.
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

    Patched at `turn_store` rather than by configuring Postgres, because what these tests are about
    is the routes' own behaviour over a store that answers — and `InMemoryStore` satisfies the same
    `BaseStore` contract the tier is written against. The 503 case patches it back to `None`, which
    is the only reason this is a fixture rather than a module-level patch.
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

    The failure mode a tier with no validator has is worse than a refusal: the person is told it
    was saved and no turn ever sees it. The NUL case is the one that was a 500 — the store rejects
    it at the driver, far from the field that carried it, so the person learns nothing.
    """
    response = client.post("/skills/mine", json={"body": body})

    assert response.status_code == 422, f"{because}: got {response.status_code} {response.text}"
    assert client.get("/skills/mine").json()["skills"] == [], "a refused skill was stored anyway"


def test_a_personal_skill_may_not_take_a_shipped_skills_name(client: TestClient) -> None:
    """409, because a collision silently decides which of two documents a model is given.

    Refused here because here there is a person to tell. The collision that arrives the other way
    round — a name entering `skills/` after somebody saved theirs — has no route to refuse at, and
    `tests/test_local_skills.py::test_a_reviewed_skill_wins_a_name_a_personal_one_also_claims` is
    where that half is decided.
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

    **The row cap is a bound on prefix spend, not on storage**, and nothing enforced it: the memory
    tier's cap lives in `scratchpad.BoundedStoreBackend`, which mounts `/memories/` and not this
    root. Every personal skill's name and description sit in the system message of every turn its
    owner takes, so an unbounded row count is an unbounded prefix.

    Refused rather than evicted — a memory a turn wrote may be dropped silently, and judgment a
    person authored may not — and a *replacement* is allowed at the cap, or a chemist who filled it
    could not correct any of them.
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

    An empty list about a mechanism that is not running reads as "you have no skills" when the
    truth is "this deployment cannot keep any".

    All four, not one: a list that 503s beside a save that appears to succeed is the worse shape,
    since the person is told their judgment was kept and it was not.
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
    """`BaseStore.asearch`'s default limit is 10, and the route inherited it.

    Driven through the route rather than the function because this is the surface the licence
    condition names: a chemist with twelve skills was shown ten, and the two beyond the page could
    not be deleted through the only route that deletes.
    """
    names = [f"skill-{index:02d}" for index in range(12)]
    for name in names:
        asyncio.run(save_local_skill(store, _ALICE.oid, name, _body(name)))

    assert client.get("/skills/mine").json()["skills"] == names
    assert client.delete(f"/skills/mine/{names[11]}").status_code == 200
