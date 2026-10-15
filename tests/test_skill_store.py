"""`SkillStore` and `may_write`: three tiers behind one interface, one rule for who changes which.

`tests/test_skill_tiers_golden.py` pins the outcomes through the doors people and turns use; this
drives the interface itself, including the refusals no route can reach because the route asks first.
"""

import asyncio
from pathlib import Path

import pytest
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.authz import AuthorizationError
from chemclaw.skills.access import SkillWriteRefused, Tier, Writer, may_write
from chemclaw.skills.backends import PermittedStoreBackend, SkillsReadOnlyRefusal
from chemclaw.skills.manifest import SkillRefused
from chemclaw.skills.store import SkillStore

_ADMIN = Writer(actor="u-admin", reviewer=True)
_ALICE = Writer(actor="u-alice")
_BOB = Writer(actor="u-bob")
_TURN = Writer(actor="u-alice", reviewer=True, by_model=True)


def _body(name: str, note: str = "Do it this way.") -> str:
    """One valid `SKILL.md` under `name`."""
    return f"---\nname: {name}\ndescription: how {name} goes\n---\n\n{note}\n"


@pytest.mark.parametrize(
    ("tier", "writer", "owner", "allowed"),
    [
        # The reviewed tree has no run-time writer at all, whoever asks.
        (Tier.REPO, _ADMIN, "u-admin", False),
        (Tier.REPO, _ALICE, "u-alice", False),
        # The organisation's tier: the privileged role, and only that.
        (Tier.ORG, _ADMIN, "", True),
        (Tier.ORG, _ALICE, "u-alice", False),
        (Tier.ORG, Writer(), "", False),
        # A chemist's own tier: its owner, and nobody else, privileged or not.
        (Tier.MINE, _ALICE, "u-alice", True),
        (Tier.MINE, _BOB, "u-alice", False),
        (Tier.MINE, _ADMIN, "u-alice", False),
        (Tier.MINE, Writer(), "", False),
        # A model turn changes nothing, however privileged the person it runs for.
        (Tier.REPO, _TURN, "u-alice", False),
        (Tier.ORG, _TURN, "u-alice", False),
        (Tier.MINE, _TURN, "u-alice", False),
    ],
)
def test_who_may_write_which_tier(tier: Tier, writer: Writer, owner: str, allowed: bool) -> None:
    """The blast-radius rule, as a table over every tier and kind of writer."""
    assert may_write(tier, writer, owner=owner) is allowed


def test_the_refusals_are_authorization_errors_a_turn_can_be_told_about() -> None:
    """A refused write is an `AuthorizationError`, and the backends' refusal is one of them."""
    assert issubclass(SkillWriteRefused, AuthorizationError)
    assert issubclass(SkillsReadOnlyRefusal, SkillWriteRefused)


@pytest.fixture
def tree(tmp_path: Path) -> list[str]:
    """A reviewed tree of two skills."""
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(_body(name), encoding="utf-8")
    return [str(tmp_path)]


def test_every_tier_lists_and_reads_through_the_one_interface(tree: list[str]) -> None:
    """Names are sorted per tier; a body is verbatim; another actor's tier is another namespace."""
    backing = InMemoryStore()
    alice = SkillStore(backing, actor="u-alice", repo_dirs=tree)
    bob = SkillStore(backing, actor="u-bob", repo_dirs=tree)

    async def scenario() -> None:
        await alice.save(Tier.MINE, "notes", _body("notes"), writer=_ALICE)
        await alice.save(Tier.ORG, "house", _body("house"), writer=_ADMIN)

        assert await alice.names(Tier.REPO) == ["alpha", "beta"]
        assert await alice.names(Tier.MINE) == ["notes"]
        assert await bob.names(Tier.MINE) == []
        assert await bob.names(Tier.ORG) == ["house"], "the organisation's tier is everyone's"

        assert await alice.read(Tier.REPO, "alpha") == _body("alpha")
        assert await alice.read(Tier.MINE, "notes") == _body("notes")
        assert await bob.read(Tier.MINE, "notes") is None
        assert await bob.read(Tier.ORG, "house") == _body("house")
        for unsafe in ("../alpha", "a/b", ".hidden", "no such", ""):
            assert await alice.read(Tier.REPO, unsafe) is None, unsafe

    asyncio.run(scenario())


def test_a_writer_the_rule_refuses_leaves_every_tier_as_it_was(tree: list[str]) -> None:
    """Each refused write raises `SkillWriteRefused` before any row is touched."""
    backing = InMemoryStore()
    alice = SkillStore(backing, actor="u-alice", repo_dirs=tree)

    async def scenario() -> None:
        await alice.save(Tier.ORG, "house", _body("house"), writer=_ADMIN)
        held = await alice.versions("house")
        for tier, writer in (
            (Tier.REPO, _ADMIN),
            (Tier.ORG, _ALICE),
            (Tier.ORG, _TURN),
            (Tier.MINE, _BOB),
            (Tier.MINE, _TURN),
        ):
            with pytest.raises(SkillWriteRefused):
                await alice.save(tier, "x", _body("x"), writer=writer)
            with pytest.raises(SkillWriteRefused):
                await alice.remove(tier, "house", writer=writer)
        with pytest.raises(SkillWriteRefused):
            await alice.revert("house", held[0].content_hash, writer=_ALICE)

        assert await alice.names(Tier.ORG) == ["house"]
        assert await alice.names(Tier.MINE) == []
        assert await alice.versions("house") == held

    asyncio.run(scenario())


def test_the_organisation_keeps_history_and_a_chemist_does_not(tree: list[str]) -> None:
    """Replacing and retiring an org skill keeps its bodies revertable; a personal one is gone."""
    backing = InMemoryStore()
    alice = SkillStore(backing, actor="u-alice", repo_dirs=tree)

    async def scenario() -> None:
        await alice.save(Tier.ORG, "house", _body("house", "v1"), writer=_ADMIN)
        await alice.save(Tier.ORG, "house", _body("house", "v2"), writer=_ADMIN)
        first = (await alice.versions("house"))[-1]
        assert await alice.remove(Tier.ORG, "house", writer=_ADMIN) is True
        assert await alice.remove(Tier.ORG, "house", writer=_ADMIN) is False
        assert await alice.names(Tier.ORG) == []

        assert await alice.revert("house", first.content_hash, writer=_ADMIN) is True
        assert await alice.read(Tier.ORG, "house") == _body("house", "v1")
        assert await alice.revert("house", "absent", writer=_ADMIN) is False

        await alice.save(Tier.MINE, "notes", _body("notes"), writer=_ALICE)
        assert await alice.remove(Tier.MINE, "notes", writer=_ALICE) is True
        assert await alice.read(Tier.MINE, "notes") is None

    asyncio.run(scenario())


def test_a_name_the_other_tier_holds_is_refused_not_shadowed() -> None:
    """A personal skill cannot take a name the organisation publishes: it would never act."""
    backing = InMemoryStore()
    alice = SkillStore(backing, actor="u-alice")

    async def scenario() -> None:
        await alice.save(Tier.ORG, "house", _body("house"), writer=_ADMIN)
        with pytest.raises(SkillRefused) as refused:
            await alice.save(Tier.MINE, "house", _body("house"), writer=_ALICE)
        assert refused.value.conflict is True

    asyncio.run(scenario())


def test_the_stored_tiers_mount_as_narrowed_backends_and_the_reviewed_tree_does_not() -> None:
    """`mount` hands the graph a stored tier's read half; the reviewed tree is not its to mount."""
    alice = SkillStore(InMemoryStore(), actor="u-alice")

    for tier in (Tier.ORG, Tier.MINE):
        assert isinstance(alice.mount(tier, lambda _name: True), PermittedStoreBackend)
    with pytest.raises(ValueError, match="mounted per bundle"):
        alice.mount(Tier.REPO, lambda _name: True)


#: The ungated primitives `SkillStore`'s write methods wrap. Naming one outside `skills/` is a
#: second door into a tier that does not ask `may_write`.
_RAW_WRITERS = frozenset(
    {
        "save_local_skill",
        "delete_local_skill",
        "save_org_skill",
        "retire_org_skill",
        "activate_org_version",
        "store_writer",
    }
)


def _raw_writers_named(source: str) -> set[str]:
    """Every raw writer a module's source imports, accesses or calls by name."""
    import ast

    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom | ast.Import):
            found |= {alias.name.rsplit(".", 1)[-1] for alias in node.names}
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
    return found & _RAW_WRITERS


def test_no_module_outside_the_skills_package_names_a_raw_writer() -> None:
    """Routes, tools, the graph and the CLI reach the tiers through `SkillStore`, nothing else."""
    root = Path("src/chemclaw")
    offenders = {
        str(path): sorted(named)
        for path in sorted(root.rglob("*.py"))
        if path.relative_to(root).parts[0] != "skills"
        if (named := _raw_writers_named(path.read_text(encoding="utf-8")))
    }
    assert not offenders, f"ungated skill writers named outside skills/: {offenders}"


@pytest.mark.parametrize(
    "source",
    [
        "from chemclaw.skills.store import save_local_skill",
        "from chemclaw.skills import store\nstore.retire_org_skill(1, 'x', retired_by='y')",
        "import chemclaw.skills.store as s\ns.store_writer(None, ())",
        "def f(save_org_skill):\n    return save_org_skill",
    ],
)
def test_the_raw_writer_scan_sees_each_way_of_naming_one(source: str) -> None:
    """Negative controls: the scan above is not blind to an alias, an attribute or a parameter."""
    assert _raw_writers_named(source)


@pytest.mark.parametrize("tier", [Tier.ORG, Tier.MINE])
def test_the_backends_refusal_is_derived_from_may_write(
    tier: Tier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `may_write` ever admitted a model, the backends still refuse, and loudly."""
    from chemclaw.skills import backends

    backend = SkillStore(InMemoryStore(), actor="u-alice").mount(tier, lambda _name: True)
    with pytest.raises(SkillsReadOnlyRefusal):
        backend.write("/x/SKILL.md", "b")

    monkeypatch.setattr(backends, "may_write", lambda *_a, **_k: True)
    with pytest.raises(RuntimeError, match="admits a model turn"):
        backend.write("/x/SKILL.md", "b")


def test_a_refused_writer_that_reaches_a_route_is_a_403_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`SkillWriteRefused` has an HTTP mapping on both skill-writing route modules."""
    from fastapi.testclient import TestClient

    from chemclaw.api.app import create_app
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.api.routes import org_skills as org_routes
    from chemclaw.api.routes import skills as mine_routes

    backing = InMemoryStore()
    said = "the skills tier cannot be changed by this writer"

    async def _store() -> InMemoryStore:
        return backing

    async def _refuse(*_a: object, **_k: object) -> None:
        raise SkillWriteRefused(said)

    monkeypatch.setattr(mine_routes, "turn_store", _store)
    monkeypatch.setattr(org_routes, "turn_store", _store)
    monkeypatch.setattr(SkillStore, "save", _refuse)

    def _no_connectors(profile: str | None = None) -> list[object]:
        return []

    def _admin() -> Principal:
        return Principal(oid="u-admin", upn="admin@example.com", roles=frozenset())

    app = create_app(connector_factory=_no_connectors, graph_factory=lambda *a, **k: None)
    app.dependency_overrides[require_principal] = _admin

    for path in ("/skills/mine", "/skills/org"):
        response = TestClient(app).post(path, json={"body": _body("x")})
        assert response.status_code == 403, (path, response.text)
        assert response.json() == {"detail": said}
