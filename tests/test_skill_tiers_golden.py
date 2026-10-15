"""Who sees which skill, and who may change which tier: a golden table over actors, tiers and roles.

The three tiers (the reviewed tree, the organisation's, a chemist's own) are read through one
narrowing and written through three different doors. This file pins the outcome of every
combination in one place, as input -> output rows rather than per-module assertions, so the
blast-radius rule (`CLAUDE.md`: "a skill changes only by blast radius") holds however the code that
implements it is arranged:

- visibility: the listing a model is shown, per actor and role, and the two predicates behind it;
- reads: another actor's tier is unreachable, and a gated skill is refused in words that do not say
  whether it exists;
- writes: no turn writes any tier, the organisation's tier changes only for the privileged role,
  a chemist's own tier only for its owner, and no route writes the reviewed tree.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.langgraph_agent import (
    NO_SKILLS,
    _skills_middleware,
    shipped_skill_names,
    skill_narrowing,
    skills_backend,
)
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.scratchpad import scratchpad_backend
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes import org_skills as org_routes
from chemclaw.api.routes import skills as mine_routes
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.skills.access import skill_permits
from chemclaw.skills.backends import (
    _LOCAL_READ_ONLY,
    _ORG_READ_ONLY,
    _READ_ONLY,
    REFUSED,
    NarrowedSkillsBackend,
    SkillsReadOnlyRefusal,
    local_skills_backend,
    org_skills_backend,
)
from chemclaw.skills.store import save_local_skill, save_org_skill, stored_skill_declarations

_ADMIN = Principal(oid="u-admin", upn="admin@example.com", roles=frozenset({"chemclaw.reviewer"}))
_ALICE = Principal(oid="u-alice", upn="alice@example.com", roles=frozenset())
_BOB = Principal(oid="u-bob", upn="bob@example.com", roles=frozenset())


def _skill(name: str, *, tools: tuple[str, ...] = (), note: str = "Do it this way.") -> str:
    """One well-formed `SKILL.md`, frontmatter included."""
    declared = f"tools: [{', '.join(tools)}]\n" if tools else ""
    return f"---\nname: {name}\ndescription: how {name} goes\n{declared}---\n\n{note}\n"


# --- the two predicates: one name universe, every narrowing ---

_UNIVERSE = ("alpha", "beta", "gamma", "delta", "reviewed")


def _visible(roles: frozenset[str] | None, **narrowing: Any) -> tuple[list[str], list[str]]:
    """The universe's names the filed predicate and the stored predicate each permit."""
    kwargs: dict[str, Any] = {
        "enabled": None,
        "declared": {},
        "available": [],
        "gates": None,
    }
    kwargs.update(narrowing)
    permits = skill_permits(**kwargs)
    token = set_current_identity("u-1", roles) if roles is not None else None
    try:
        return (
            [name for name in _UNIVERSE if permits.filed(name)],
            [name for name in _UNIVERSE if permits.stored(name)],
        )
    finally:
        if token is not None:
            reset_current_identity(token)


_ALL = list(_UNIVERSE)


@pytest.mark.parametrize(
    ("label", "roles", "narrowing", "filed", "stored"),
    [
        ("nothing configured", None, {}, _ALL, _ALL),
        ("enable-list names shipped skills only", None, {"enabled": ["alpha"]}, ["alpha"], _ALL),
        (
            "profile names narrow both tiers",
            None,
            {"names": ["alpha", "beta"]},
            ["alpha", "beta"],
            ["alpha", "beta"],
        ),
        ("an empty profile list reaches nothing", None, {"names": []}, [], []),
        (
            "a gated skill is hidden from an anonymous caller",
            None,
            {"gates": {"beta": ["chemist"]}},
            ["alpha", "gamma", "delta", "reviewed"],
            ["alpha", "gamma", "delta", "reviewed"],
        ),
        (
            "a gated skill is hidden from a caller without the role",
            frozenset({"viewer"}),
            {"gates": {"beta": ["chemist"]}},
            ["alpha", "gamma", "delta", "reviewed"],
            ["alpha", "gamma", "delta", "reviewed"],
        ),
        (
            "a gated skill is shown to a holder of the role",
            frozenset({"chemist"}),
            {"gates": {"beta": ["chemist"]}},
            _ALL,
            _ALL,
        ),
        (
            "a skill whose every declared tool is absent is hidden",
            None,
            {"declared": {"gamma": frozenset({"calc_xtb"})}, "available": ["find_notes"]},
            ["alpha", "beta", "delta", "reviewed"],
            ["alpha", "beta", "delta", "reviewed"],
        ),
        (
            "one surviving declared tool keeps the skill",
            None,
            {
                "declared": {"gamma": frozenset({"calc_xtb", "find_notes"})},
                "available": ["find_notes"],
            },
            _ALL,
            _ALL,
        ),
        (
            "a missing required tool hides a skill whose other tools remain",
            None,
            {
                "declared": {"delta": frozenset({"a", "b"})},
                "required": {"delta": frozenset({"a"})},
                "available": ["b"],
            },
            ["alpha", "beta", "gamma", "reviewed"],
            ["alpha", "beta", "gamma", "reviewed"],
        ),
        (
            "a reserved name is hidden from the stored tiers only",
            None,
            {"reserved": ["reviewed"]},
            _ALL,
            ["alpha", "beta", "gamma", "delta"],
        ),
        (
            "everything at once",
            frozenset({"chemist"}),
            {
                "enabled": ["alpha", "beta", "gamma"],
                "names": ["alpha", "beta", "reviewed"],
                "gates": {"beta": ["chemist"]},
                "declared": {"alpha": frozenset({"missing"})},
                "available": ["find_notes"],
                "reserved": ["beta"],
            },
            ["beta"],
            ["reviewed"],
        ),
    ],
)
def test_the_two_predicates_over_every_narrowing(
    label: str,
    roles: frozenset[str] | None,
    narrowing: dict[str, Any],
    filed: list[str],
    stored: list[str],
) -> None:
    """The filed and stored predicates each permit exactly the rows' names, per narrowing."""
    assert _visible(roles, **narrowing) == (filed, stored), label


# --- the listing a model is shown, per actor and role ---


@pytest.fixture
def store() -> InMemoryStore:
    """A store standing in for the deployment's `AsyncPostgresStore`, same `BaseStore` contract."""
    return InMemoryStore()


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """A reviewed tree of three skills, one role-gated and one about a tool nobody binds."""
    for name, tools in (("alpha", ()), ("beta", ()), ("gamma", ("calc_xtb",))):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(_skill(name, tools=tools), encoding="utf-8")
    monkeypatch.setattr(settings, "skill_role_gates", {"beta": ["chemist"]})
    return [("skills", str(tmp_path))]


@contextmanager
def _as(actor: str, roles: frozenset[str] = frozenset()) -> Iterator[None]:
    """Run as `actor` holding `roles`, the ambient identity a turn's tiers read."""
    tokens = set_current_identity(actor, roles)
    try:
        yield
    finally:
        reset_current_identity(tokens)


def _listing(
    store: InMemoryStore,
    labelled: list[tuple[str, str]],
    actor: str,
    roles: frozenset[str],
    names: frozenset[str] | None = None,
) -> list[str]:
    """The skill paths the skills middleware would advertise to `actor`, sorted.

    Built as a turn builds it: the declarations of the stored tiers are read first, the narrowing
    is computed once, and the middleware lists through the mounted backend.
    """
    profile = AgentProfile(name="default", skill_names=names)
    with _as(actor, roles):
        declarations = asyncio.run(stored_skill_declarations(store))
        permits = skill_narrowing(
            profile, [], labelled, available={"find_notes"}, stored=declarations
        )
        backend = scratchpad_backend(
            skills_backend(
                profile, [], labelled=labelled, available={"find_notes"}, permits=permits
            ),
            store,
            permits=permits,
        )
        middleware = _skills_middleware(backend, labelled, profile)
        loaded = middleware.before_agent({}, None, None) or {}
        metadata = loaded.get("skills_metadata", [])
        rendered = str(middleware._format_skills_list(metadata))
    paths = sorted(str(item["path"]) for item in metadata)
    if not paths:
        assert rendered == NO_SKILLS, (
            "an empty listing says so, in words that do not invite a write"
        )
    for path in paths:
        assert path in rendered, f"{path} was loaded but is not in the text the model reads"
    return paths


@pytest.fixture
def populated(store: InMemoryStore) -> InMemoryStore:
    """An organisation skill, two chemists' own, and one of alice's that teaches an absent tool."""
    asyncio.run(save_org_skill(store, "house-workup", _skill("house-workup"), activated_by="admin"))
    asyncio.run(save_local_skill(store, "alice-oid", "alice-notes", _skill("alice-notes")))
    asyncio.run(
        save_local_skill(
            store, "alice-oid", "alice-calc", _skill("alice-calc", tools=("calc_xtb",))
        )
    )
    asyncio.run(save_local_skill(store, "bob-oid", "bob-notes", _skill("bob-notes")))
    return store


@pytest.mark.parametrize(
    ("actor", "roles", "expected"),
    [
        (
            "alice-oid",
            frozenset(),
            ["/mine/alice-notes/SKILL.md", "/org/house-workup/SKILL.md", "/skills/alpha/SKILL.md"],
        ),
        (
            "alice-oid",
            frozenset({"chemist"}),
            [
                "/mine/alice-notes/SKILL.md",
                "/org/house-workup/SKILL.md",
                "/skills/alpha/SKILL.md",
                "/skills/beta/SKILL.md",
            ],
        ),
        (
            "bob-oid",
            frozenset(),
            ["/mine/bob-notes/SKILL.md", "/org/house-workup/SKILL.md", "/skills/alpha/SKILL.md"],
        ),
        ("", frozenset(), ["/org/house-workup/SKILL.md", "/skills/alpha/SKILL.md"]),
        ("nobody-oid", frozenset(), ["/org/house-workup/SKILL.md", "/skills/alpha/SKILL.md"]),
    ],
)
def test_the_listing_a_model_is_shown_per_actor_and_role(
    populated: InMemoryStore,
    tree: list[tuple[str, str]],
    actor: str,
    roles: frozenset[str],
    expected: list[str],
) -> None:
    """Each tier contributes only what this actor may reach; none shows another actor's skill."""
    assert _listing(populated, tree, actor, roles) == expected


def test_a_profile_that_names_skills_narrows_every_tier(
    populated: InMemoryStore, tree: list[tuple[str, str]]
) -> None:
    """`skill_names` is one list over all three tiers: an unnamed stored skill is gone too."""
    named = frozenset({"alpha", "house-workup"})
    assert _listing(populated, tree, "alice-oid", frozenset(), named) == [
        "/org/house-workup/SKILL.md",
        "/skills/alpha/SKILL.md",
    ]
    assert _listing(populated, tree, "alice-oid", frozenset(), frozenset()) == []


# --- reads: a refusal reveals nothing ---


def test_a_read_outside_what_the_actor_may_reach_reveals_nothing(
    populated: InMemoryStore, tree: list[tuple[str, str]]
) -> None:
    """A gated skill is refused in fixed words; another chemist's skill reads as an absent one."""
    profile = AgentProfile(name="default")
    with _as("alice-oid"):
        permits = skill_narrowing(profile, [], tree, available={"find_notes"})
        backend = scratchpad_backend(
            skills_backend(profile, [], labelled=tree, available={"find_notes"}, permits=permits),
            populated,
            permits=permits,
        )
        gated = backend.read("/skills/beta/SKILL.md")
        theirs = backend.read("/mine/bob-notes/SKILL.md")
        absent = backend.read("/mine/nobody-wrote-this/SKILL.md")
        allowed = backend.read("/mine/alice-notes/SKILL.md")
    assert gated.error == REFUSED
    assert "Do it this way." in str(allowed.file_data)
    # The stored tiers answer for a path outside the actor's namespace as for a missing file.
    assert theirs.error is not None and theirs.file_data is None
    assert absent.error is not None and absent.file_data is None
    assert "not found" in theirs.error and "not found" in absent.error


# --- writes: no turn, on any tier ---

_WRITE_VERBS = (
    ("write", ("/x/SKILL.md", "body")),
    ("edit", ("/x/SKILL.md", "a", "b")),
    ("delete", ("/x/SKILL.md",)),
    ("upload_files", ([("/x/SKILL.md", b"body")],)),
)


@pytest.mark.parametrize(
    ("tier", "opening"),
    [
        ("repo", "the skills tree is read-only"),
        ("mine", "your own skills are read-only to a turn"),
        ("org", "the organisation's skills are read-only to a turn"),
    ],
)
def test_no_turn_writes_any_tier_by_any_verb(
    tier: str, opening: str, store: InMemoryStore, tree: list[tuple[str, str]]
) -> None:
    """Every write verb, sync and async, raises the tier's own refusal; nothing is stored."""
    backend: Any
    if tier == "repo":
        backend, expected = NarrowedSkillsBackend(tree[0][1], lambda _name: True), _READ_ONLY
    elif tier == "mine":
        backend = local_skills_backend(store, "alice-oid", lambda _name: True)
        expected = _LOCAL_READ_ONLY
    else:
        backend, expected = org_skills_backend(store, lambda _name: True), _ORG_READ_ONLY
    assert expected.startswith(opening)

    async def attempt(verb: str, args: tuple[Any, ...]) -> None:
        await getattr(backend, f"a{verb}")(*args)

    for verb, args in _WRITE_VERBS:
        with pytest.raises(SkillsReadOnlyRefusal) as sync:
            getattr(backend, verb)(*args)
        assert str(sync.value) == expected, verb
        if tier != "repo":  # the filed backend's async verbs are thread wrappers over these
            with pytest.raises(SkillsReadOnlyRefusal) as native:
                asyncio.run(attempt(verb, args))
            assert str(native.value) == expected, f"a{verb}"
    assert store.list_namespaces() == [], "a refused write must leave no row"


# --- writes: the doors people use ---


def _no_connectors(profile: str | None = None) -> list[object]:
    """No connector session: nothing here reaches a capability server."""
    return []


@pytest.fixture
def app(store: InMemoryStore, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The real front door over a store that answers, enforcing identity as a deployment does."""

    async def _store() -> Any:
        return store

    monkeypatch.setattr(mine_routes, "turn_store", _store)
    monkeypatch.setattr(org_routes, "turn_store", _store)
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_privileged_roles", "chemclaw.reviewer")
    return create_app(connector_factory=_no_connectors, graph_factory=lambda *a, **k: None)


def _as_principal(app: FastAPI, principal: Principal) -> TestClient:
    """The same app, arriving as somebody."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


def test_the_routes_that_exist_are_the_whole_of_the_write_surface(app: FastAPI) -> None:
    """Skills are written through these routes and no others; the reviewed tree has none."""
    served = sorted(
        (method, getattr(route, "path", ""))
        for route in app.routes
        for method in getattr(route, "methods", ())
        if getattr(route, "path", "").startswith("/skills") and method not in {"HEAD", "OPTIONS"}
    )
    assert served == sorted(
        [
            ("GET", "/skills/mine"),
            ("POST", "/skills/mine"),
            ("GET", "/skills/mine/{name}"),
            ("DELETE", "/skills/mine/{name}"),
            ("GET", "/skills/org"),
            ("POST", "/skills/org"),
            ("GET", "/skills/org/{name}"),
            ("DELETE", "/skills/org/{name}"),
            ("GET", "/skills/org/{name}/versions"),
            ("POST", "/skills/org/{name}/revert"),
        ]
    )


#: Cumulative on purpose: what a refused call leaves behind is part of the answer, so the rows run
#: in order over one store. `(caller, method, path, posted body or None, expected status)`.
_SEQUENCE: list[tuple[Principal, str, str, Any, int]] = [
    # The organisation's tier: open to read, the privileged role to change.
    (_ALICE, "GET", "/skills/org", None, 200),
    (_ALICE, "POST", "/skills/org", _skill("house"), 403),
    (_ALICE, "GET", "/skills/org/house", None, 404),
    (_ADMIN, "POST", "/skills/org", "not a skill", 422),
    (_ADMIN, "POST", "/skills/org", _skill("house"), 200),
    (_ALICE, "GET", "/skills/org/house", None, 200),
    (_ALICE, "GET", "/skills/org/house/versions", None, 200),
    (_ALICE, "POST", "/skills/org/house/revert", {"content_hash": "x"}, 403),
    (_ADMIN, "POST", "/skills/org/house/revert", {"content_hash": "absent"}, 404),
    (_BOB, "DELETE", "/skills/org/house", None, 403),
    # A chemist's own tier: each caller reaches only their own, and nobody else's.
    (_ALICE, "POST", "/skills/mine", _skill("notes"), 200),
    (_BOB, "GET", "/skills/mine/notes", None, 404),
    (_BOB, "DELETE", "/skills/mine/notes", None, 404),
    (_ADMIN, "GET", "/skills/mine/notes", None, 404),
    (_ALICE, "GET", "/skills/mine/notes", None, 200),
    (_ALICE, "POST", "/skills/mine", "not a skill", 422),
    # A name another tier already holds is refused: the organisation's, then a reviewed skill's.
    (_ALICE, "POST", "/skills/mine", _skill("house"), 409),
    (_ALICE, "POST", "/skills/mine", _skill("{shipped}"), 409),
    (_ADMIN, "POST", "/skills/org", _skill("{shipped}"), 409),
    # Retiring is the privileged role's too, and a second retirement finds nothing.
    (_ADMIN, "DELETE", "/skills/org/house", None, 200),
    (_ADMIN, "DELETE", "/skills/org/house", None, 404),
    (_ALICE, "DELETE", "/skills/mine/notes", None, 200),
    (_ALICE, "DELETE", "/skills/mine/notes", None, 404),
]


def test_who_may_change_which_tier_through_which_door(app: FastAPI) -> None:
    """One caller, one door, one status, in order over one shared store."""
    shipped = sorted(shipped_skill_names())[0]
    for caller, method, path, body, status in _SEQUENCE:
        posted = None
        if isinstance(body, dict):
            posted = body
        elif body is not None:
            posted = {"body": body.replace("{shipped}", shipped)}
        response = _as_principal(app, caller).request(method, path, json=posted)
        assert response.status_code == status, (
            f"{caller.oid} {method} {path} -> {response.status_code} {response.text}"
        )


def test_the_caps_are_refused_not_evicted_and_a_held_skill_may_still_be_corrected(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both stored tiers refuse a new skill at their cap (409) and still accept a replacement."""
    monkeypatch.setattr(settings, "agent_local_skills_max", 1)
    monkeypatch.setattr(settings, "agent_org_skills_max", 1)

    def post(caller: Principal, tier: str, name: str, note: str = "Do it this way.") -> int:
        client = _as_principal(app, caller)
        return client.post(f"/skills/{tier}", json={"body": _skill(name, note=note)}).status_code

    assert post(_ALICE, "mine", "one") == 200
    assert post(_ALICE, "mine", "two") == 409
    assert post(_ALICE, "mine", "one", "Fixed.") == 200
    assert post(_BOB, "mine", "two") == 200

    assert post(_ADMIN, "org", "first") == 200
    assert post(_ADMIN, "org", "second") == 409
    assert post(_ADMIN, "org", "first", "v2") == 200
    versions = _as_principal(app, _ADMIN).get("/skills/org/first/versions").json()["versions"]
    assert len(versions) == 2, "the replaced body is held as a revert target"
