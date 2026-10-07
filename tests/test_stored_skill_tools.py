"""The two stored skills tiers are narrowed by the questions that apply to them, and by no others.

`ToolScopedSkills` must read stored bodies' declarations, since it can answer for them;
`EnabledSkills` names shipped skills and must not empty a stored tier. The argument is in
`docs/decisions/D-2026-09-21-a-stored-tier-and-a-filed-tree-are-not-asked-the-same-question.md`.
"""

import asyncio
import logging
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.langgraph_agent import (
    _labelled,
    _skill_dirs,
    _skills_middleware,
    shipped_skill_names,
    skill_narrowing,
    skills_backend,
)
from chemclaw.agent.local_skills import (
    LOCAL_SKILLS_ROOT,
    local_skills_namespace,
    save_local_skill,
)
from chemclaw.agent.org_skills import ORG_SKILLS_ROOT, save_org_skill
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.scratchpad import scratchpad_backend
from chemclaw.agent.skill_access import skill_permits
from chemclaw.agent.skill_manifest import UNREADABLE_DECLARATION, declared_tools
from chemclaw.agent.skill_store import LISTING_PAGE, skill_key, store_writer
from chemclaw.agent.stored_skill_tools import StoredSkillTools, stored_skill_declarations
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity

_ACTOR = "alice-oid"


def _writer(store: Any) -> Any:
    """A writer over `_ACTOR`'s own tier, bypassing admission to plant a body as-is."""
    return store_writer(store, local_skills_namespace(_ACTOR))


#: A personal skill that declares two tools, so it has something for the capability gate to read.
_DECLARING = (
    "---\nname: my-workup\ndescription: how I work up a Suzuki\n"
    "tools: [compute_thermochemistry, sample_conformers]\n---\n\nQuench cold.\n"
)

#: A personal skill declaring nothing, which the conservative rule leaves visible. Kept in every
#: arm so a fix that hid the whole tier could not pass as a narrowing.
_BARE = "---\nname: my-notes\ndescription: what I always forget\n---\n\nLabel the flask.\n"

#: The organisation's tier gets the same treatment, which is why the row was one row and not two.
_ORG = (
    "---\nname: house-workup\ndescription: the house workup\n"
    "tools: [compute_thermochemistry]\n---\n\nQuench cold.\n"
)


@pytest.fixture
def store() -> InMemoryStore:
    """A store standing in for the deployment's `AsyncPostgresStore`, same `BaseStore` contract."""
    return InMemoryStore()


@pytest.fixture
def stocked(store: InMemoryStore) -> InMemoryStore:
    """One declaring and one bare personal skill, plus one declaring org skill."""
    asyncio.run(save_local_skill(store, _ACTOR, "my-workup", _DECLARING))
    asyncio.run(save_local_skill(store, _ACTOR, "my-notes", _BARE))
    asyncio.run(save_org_skill(store, "house-workup", _ORG, activated_by="admin-oid"))
    return store


def _mounted(
    store: Any,
    *,
    actor: str = _ACTOR,
    available: set[str] | None = None,
    profile: AgentProfile | None = None,
    stored: StoredSkillTools | None | bool = True,
) -> Any:
    """The backend a turn would be given, with the narrowing a turn really computes.

    Walks the real trees and declarations, since the narrowing is the subject here. `stored=False`
    withholds the stored declarations, the defect arm.
    """
    prof = profile or AgentProfile(name="default")
    tokens = set_current_identity(actor, frozenset())
    try:
        read = asyncio.run(stored_skill_declarations(store)) if stored is True else stored or None
        labelled = _labelled(_skill_dirs())
        permits = skill_narrowing(
            prof, [], labelled, available=available if available is not None else set(), stored=read
        )
        return scratchpad_backend(
            skills_backend(prof, [], labelled=labelled, permits=permits), store, permits=permits
        )
    finally:
        reset_current_identity(tokens)


def _read(store: Any | None, actor: str) -> StoredSkillTools:
    """The reader, under the identity a turn takes — it reads the ambient actor, not an argument.

    The reader and the mount must resolve one spelling of the actor.
    """
    tokens = set_current_identity(actor, frozenset())
    try:
        return asyncio.run(stored_skill_declarations(store))
    finally:
        reset_current_identity(tokens)


def _served(backend: Any, root: str, name: str) -> bool:
    """Whether a turn can read one skill's body through the mount."""
    return backend.read(f"{root}{name}/SKILL.md").error is None


def _listed(backend: Any, root: str) -> list[str]:
    """What one mount's `ls` offers, which is the other half of the gate."""
    return sorted(entry["path"] for entry in backend.ls(root).entries)


def test_a_stored_skill_about_tools_this_turn_cannot_reach_is_not_offered(
    stocked: InMemoryStore,
) -> None:
    """A stored skill about tools this turn cannot reach is not offered, on both tiers.

    A skill declaring nothing stays visible, so this is a narrowing and not a deletion.
    """
    at_zero = _mounted(stocked, available=set())

    assert not _served(at_zero, LOCAL_SKILLS_ROOT, "my-workup")
    assert not _served(at_zero, ORG_SKILLS_ROOT, "house-workup")
    assert _listed(at_zero, LOCAL_SKILLS_ROOT) == [f"{LOCAL_SKILLS_ROOT}my-notes/"], (
        "the bare skill must survive: `ToolScopedSkills` hides on *every* declared tool being "
        "absent, and a skill declaring none has no tools to be absent"
    )
    assert _listed(at_zero, ORG_SKILLS_ROOT) == []

    # The control arm: the same bodies, the same turn, one tool bound.
    with_tool = _mounted(stocked, available={"compute_thermochemistry"})
    assert _served(with_tool, LOCAL_SKILLS_ROOT, "my-workup")
    assert _served(with_tool, ORG_SKILLS_ROOT, "house-workup")

    # And the defect arm, so this test would have failed on the code it replaced rather than passing
    # for a reason nobody checked.
    before = _mounted(stocked, available=set(), stored=False)
    assert _served(before, LOCAL_SKILLS_ROOT, "my-workup"), (
        "with the stored declarations withheld the skill must come back, or this test is measuring "
        "something other than the declarations"
    )


def test_a_filed_skill_is_still_hidden_by_the_same_predicate(stocked: InMemoryStore) -> None:
    """The filed half is unchanged, which is what makes the arm above a comparison.

    Stated as a number off the live corpus rather than a skill name, so it keeps meaning as the tree
    grows: at zero bound tools, a skill that declares anything at all cannot survive.
    """
    directories = [directory for _label, directory in _labelled(_skill_dirs())]
    declared = declared_tools(directories)
    tokens = set_current_identity(_ACTOR, frozenset())
    try:
        permits = skill_narrowing(
            AgentProfile(name="default"), [], _labelled(_skill_dirs()), available=set()
        )
    finally:
        reset_current_identity(tokens)

    declaring = {name for name, tools in declared.items() if tools}
    assert declaring, "no filed skill declares a tool, so this asserts nothing"
    assert not [name for name in declaring if permits.filed(name)]


def test_an_enable_list_does_not_empty_a_stored_tier(stocked: InMemoryStore) -> None:
    """An enable-list does not empty a stored tier.

    `EnabledSkills` names shipped skills, and `skill-validate` forbids stored names in it, so
    applying it to a stored tier could only empty it.
    """
    every_tool = {"compute_thermochemistry", "sample_conformers"}
    original = settings.skills_enabled
    settings.skills_enabled = "development-report"
    try:
        backend = _mounted(stocked, available=every_tool)

        assert _served(backend, LOCAL_SKILLS_ROOT, "my-workup")
        assert _served(backend, ORG_SKILLS_ROOT, "house-workup")
        assert _listed(backend, ORG_SKILLS_ROOT) == [f"{ORG_SKILLS_ROOT}house-workup/"]
        # And the enable-list still does its job on the tier it is about, or the fix would be
        # "stopped applying a narrowing" rather than "applied it where it answers something".
        assert backend.read("/skills/deep-research/SKILL.md").error is not None
    finally:
        settings.skills_enabled = original


def test_the_control_arm_that_removes_every_skill_still_removes_both_stored_tiers(
    stocked: InMemoryStore,
) -> None:
    """The control arm that removes every skill still removes both stored tiers.

    `skill_names: frozenset()` means no skill at all; `ProfileScopedSkills` is the filed-basis
    narrowing that the stored tiers keep.
    """
    arm = AgentProfile(name="default", skill_names=frozenset())
    backend = _mounted(
        stocked, profile=arm, available={"compute_thermochemistry", "sample_conformers"}
    )

    assert _listed(backend, LOCAL_SKILLS_ROOT) == []
    assert _listed(backend, ORG_SKILLS_ROOT) == []
    assert not _served(backend, LOCAL_SKILLS_ROOT, "my-notes")


def test_a_stored_skill_under_a_shipped_name_is_never_served(store: InMemoryStore) -> None:
    """A stored skill under a shipped name is never served.

    The binding scenario is an enable-list omitting the name: `EnabledSkills` is filed-only, so only
    `UnreservedNames` hides the stored copy. A role gate would hide it either way and cannot tell
    the arms apart. The body is written through the tier's writer, standing in for a row stored
    before `validated_skill` refused the name.
    """
    contested = "deep-research"
    assert contested in shipped_skill_names(), (
        f"{contested} is not shipped, so this asserts nothing"
    )
    body = f"---\nname: {contested}\ndescription: my own digging\n---\n\nMine, not theirs.\n"
    asyncio.run(_writer(store).awrite(skill_key(contested), body))
    every_tool = set().union(
        *declared_tools([d for _label, d in _labelled(_skill_dirs())]).values()
    ) | {"compute_thermochemistry"}

    original = settings.skills_enabled
    try:
        # An enable-list naming some other shipped skill, so the reviewed `deep-research` leaves the
        # filed listing and the stored copy is the only claimant left.
        settings.skills_enabled = "development-report"
        offered = _paths_offered(_mounted(store, available=every_tool))
        assert contested not in offered, (
            "the personal copy took the reserved name: the model is served "
            f"{offered.get(contested)}"
        )
        assert not _served(_mounted(store, available=every_tool), LOCAL_SKILLS_ROOT, contested), (
            "hidden from the listing is only half the gate — the body must be unreadable too"
        )
        assert "development-report" in offered, (
            "the enable-list removed everything, so this arm does not show the reviewed copy "
            "leaving"
        )
    finally:
        settings.skills_enabled = original

    # Without the enable-list the reviewed copy wins, which is the precedence
    # `tests/test_local_skills.py::test_a_reviewed_skill_wins_a_name_a_personal_one_also_claims`
    # decides and this must not have changed.
    assert _paths_offered(_mounted(store, available=every_tool))[contested].startswith("/skills/")


def test_a_role_gate_alone_does_not_reach_the_reserved_name_case(store: InMemoryStore) -> None:
    """A role gate alone cannot distinguish the `UnreservedNames` arms.

    It hides the stored copy on its own; kept so this scenario is not written as the guard.
    """
    contested = "deep-research"
    body = f"---\nname: {contested}\ndescription: my own digging\n---\n\nMine.\n"
    asyncio.run(_writer(store).awrite(skill_key(contested), body))
    every_tool = set().union(
        *declared_tools([d for _label, d in _labelled(_skill_dirs())]).values()
    ) | {"compute_thermochemistry"}

    original = settings.skill_role_gates
    try:
        settings.skill_role_gates = {contested: ["Chemclaw.Admin"]}
        offered = _paths_offered(_mounted(store, available=every_tool))
    finally:
        settings.skill_role_gates = original

    assert contested not in offered, "the gate must hide both copies, which is the point here"


def test_a_stored_requires_narrows_the_stored_tier(store: InMemoryStore) -> None:
    """`requires:` on a stored body narrows the stored tier as it does a filed one.

    Separate from `tools:`, since the quantifiers differ; asserted on visibility, not on the map.
    """
    body = (
        "---\nname: my-scan\ndescription: how I scan\n"
        "tools: [compute_thermochemistry, sample_conformers]\n"
        "requires: [sample_conformers]\n---\n\nScan wide.\n"
    )
    asyncio.run(save_local_skill(store, _ACTOR, "my-scan", body))

    read = _read(store, _ACTOR)
    assert read.required["my-scan"] == frozenset({"sample_conformers"})

    # `tools:` alone would keep this visible — `compute_thermochemistry` is bound, and the declared
    # rule survives on *any* reachable tool. `requires:` is what takes it away.
    partial = _mounted(store, available={"compute_thermochemistry"})
    assert not _served(partial, LOCAL_SKILLS_ROOT, "my-scan"), (
        "a required tool this turn cannot reach must hide the skill even though a declared one is "
        "bound; the stored `requires:` map is not reaching the narrowing"
    )
    both = _mounted(store, available={"compute_thermochemistry", "sample_conformers"})
    assert _served(both, LOCAL_SKILLS_ROOT, "my-scan")


def test_a_stored_declaration_cannot_hide_the_reviewed_skill_of_that_name(
    store: InMemoryStore,
) -> None:
    """A stored declaration cannot hide the reviewed skill of the same name.

    One map feeds both predicates, so the filed entry wins a name both hold; a stored declaration
    for a shipped name describes a body no turn reads.
    """
    contested = "deep-research"
    filed = declared_tools([d for _label, d in _labelled(_skill_dirs())])
    assert filed[contested], f"{contested} declares nothing, so this asserts nothing"
    body = f"---\nname: {contested}\ndescription: mine\ntools: [a_tool_nothing_binds]\n---\nMine.\n"
    asyncio.run(_writer(store).awrite(skill_key(contested), body))

    offered = _paths_offered(_mounted(store, available=set(filed[contested])))

    assert offered[contested].startswith("/skills/"), (
        f"the reviewed {contested} is gone from the listing: served {offered.get(contested)}"
    )


def test_the_reserved_rule_does_not_reach_a_name_no_tree_ships(stocked: InMemoryStore) -> None:
    """`UnreservedNames` closes only a collision: personal names no tree ships survive it."""
    assert shipped_skill_names(), "no shipped skills, so the reserved set narrows nothing here"
    backend = _mounted(stocked, available={"compute_thermochemistry", "sample_conformers"})

    assert _served(backend, LOCAL_SKILLS_ROOT, "my-workup")
    assert _served(backend, LOCAL_SKILLS_ROOT, "my-notes")
    assert _served(backend, ORG_SKILLS_ROOT, "house-workup")


def test_the_reserved_rule_is_asked_of_stored_tiers_only() -> None:
    """Asked of a filed tree it would hide every shipped skill from itself.

    Stated on the predicate rather than through a mount, because the mount cannot express the
    mistake: the argument is that the *partition* is what makes this narrowing safe to add at all.
    """
    narrowing = skill_permits(
        enabled=None,
        declared={},
        available=[],
        gates=None,
        reserved=frozenset({"deep-research"}),
    )

    assert narrowing.filed("deep-research")
    assert not narrowing.stored("deep-research")
    assert narrowing.stored("my-workup")


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        pytest.param({"names": frozenset()}, "anything", id="profile-scoped"),
        pytest.param(
            {"declared": {"x": frozenset({"absent_tool"})}, "available": []}, "x", id="tool-scoped"
        ),
        pytest.param({"gates": {"x": ["a-role-nobody-holds"]}}, "x", id="role-scoped"),
    ],
)
def test_a_narrowing_that_applies_to_both_tiers_answers_both_the_same(
    kwargs: dict[str, Any], name: str
) -> None:
    """The shared narrowings are the same objects in both halves, so they cannot drift.

    `SkillNarrowing` builds `filed` and `stored` from one tuple; this fails if one is rebuilt.
    """
    base: dict[str, Any] = {"enabled": None, "declared": {}, "available": [], "gates": None}
    narrowing = skill_permits(**{**base, **kwargs})

    assert narrowing.filed(name) == narrowing.stored(name) is False


def test_an_unreadable_stored_body_is_scoped_to_nothing(store: InMemoryStore) -> None:
    """An unreadable stored body is scoped to nothing (fail closed).

    A missing entry reads as "declares nothing", so dropping it would widen visibility. The writer
    stands in for a body stored before a validation rule tightened.
    """
    asyncio.run(_writer(store).awrite(skill_key("broken"), "---\ntools: {not: a list}\n---\nx"))
    asyncio.run(_writer(store).awrite(skill_key("nameless"), "no frontmatter at all"))

    read = _read(store, _ACTOR)

    assert read.declared["broken"] == UNREADABLE_DECLARATION
    assert read.required["broken"] == UNREADABLE_DECLARATION
    assert read.declared["nameless"] == UNREADABLE_DECLARATION
    assert not _served(
        _mounted(store, available={"compute_thermochemistry"}), LOCAL_SKILLS_ROOT, "broken"
    )


def test_a_stored_body_and_a_filed_one_are_read_by_one_function(tmp_path: Any) -> None:
    """`declared_triple` is the single parse, so a `tools:` key means one thing in both tiers.

    The same failure shapes are driven through both doors and must give the same answer.
    """
    import frontmatter

    from chemclaw.agent.skill_manifest import _declared_pair, declared_triple

    shapes = [
        "---\nname: ok\ntools: [a, b]\nrequires: [a]\n---\nbody",
        "---\nname: '   '\ntools: [a]\n---\nbody",
        "---\nname: scalar\ntools: a\n---\nbody",
        "---\nname: mapping\ntools: {a: b}\n---\nbody",
        "---\nname: nothing\n---\nbody",
    ]
    for index, text in enumerate(shapes):
        directory = tmp_path / f"s{index}"
        directory.mkdir()
        (directory / "SKILL.md").write_text(text)
        filed = _declared_pair(directory / "SKILL.md")
        try:
            stored = declared_triple(frontmatter.loads(text).metadata)
        except Exception:
            # The filed path turns a raise into the fail-closed pair keyed by the directory, which
            # is the one difference: the fallback name is the caller's to supply.
            assert filed == (directory.name, UNREADABLE_DECLARATION, UNREADABLE_DECLARATION)
            continue
        assert filed == stored, f"the two doors disagree about {text!r}"


def test_a_tier_is_read_exactly_where_it_is_mounted(store: InMemoryStore) -> None:
    """A tier is read exactly where it is mounted, in both directions."""
    asyncio.run(save_local_skill(store, _ACTOR, "my-workup", _DECLARING))
    asyncio.run(save_org_skill(store, "house-workup", _ORG, activated_by="admin-oid"))

    assert not _read(None, _ACTOR).declared
    actorless = _read(store, "")
    assert sorted(actorless.declared) == ["house-workup"], (
        "an actorless turn mounts the organisation's tier and not the chemist's, so it must read "
        "exactly one of them"
    )
    both = _read(store, _ACTOR)
    assert sorted(both.declared) == ["house-workup", "my-workup"]


def test_the_reader_and_the_mount_resolve_one_actor_to_one_namespace(store: InMemoryStore) -> None:
    """A padded actor spelling resolves the reader and the mount to one namespace.

    `get_current_actor` strips the value; a raw spelling would leave the reader finding nothing and
    the `/mine` tier unscoped. Driven through the mount, since the tier being scoped is the
    property.
    """
    asyncio.run(save_local_skill(store, _ACTOR, "my-workup", _DECLARING))

    padded = _mounted(store, actor=f"  {_ACTOR}  ", available=set())
    plain = _mounted(store, actor=_ACTOR, available=set())

    assert not _served(plain, LOCAL_SKILLS_ROOT, "my-workup"), "the control arm must be scoped"
    assert not _served(padded, LOCAL_SKILLS_ROOT, "my-workup"), (
        "a padded actor spelling left the personal tier unscoped, so the gate and the mount "
        "disagreed about which namespace this turn's tier lives in"
    )


def test_a_name_both_tiers_hold_is_scoped_by_the_body_the_turn_reads(store: InMemoryStore) -> None:
    """A name both stored tiers hold is scoped by the organisation's body, the one a turn reads.

    Upstream resolves collisions last-source-wins and `/org` comes after `/mine`. The served path is
    asserted beside the declaration, so the map and the listing must agree.
    """
    shared = "house-workup"
    asyncio.run(save_org_skill(store, shared, _ORG, activated_by="admin-oid"))
    mine = f"---\nname: {shared}\ndescription: my version\ntools: [sample_conformers]\n---\nMine.\n"
    asyncio.run(_writer(store).awrite(skill_key(shared), mine))

    read = _read(store, _ACTOR)
    offered = _paths_offered(_mounted(store, available={"compute_thermochemistry"}))

    assert offered[shared] == f"{ORG_SKILLS_ROOT}{shared}/SKILL.md", (
        "the mount order changed, so re-derive which body the declaration must come from"
    )
    assert read.declared[shared] == frozenset({"compute_thermochemistry"}), (
        "the declaration was read from the personal body, which is not the one the line "
        "above shows the turn is served"
    )
    # And the consequence, driven rather than argued: the org skill's own tools decide its
    # visibility, so a private document declaring something unbindable cannot take it away.
    assert _served(_mounted(store, available={"compute_thermochemistry"}), ORG_SKILLS_ROOT, shared)


def test_the_reader_pages_like_the_listing_it_shares_a_walk_with(store: InMemoryStore) -> None:
    """The reader pages like the listing, over more rows than one page.

    `BaseStore.asearch` defaults to `limit=10`; skills past the first page would otherwise be
    unscoped, the fail-open direction.
    """
    rows = LISTING_PAGE + 7
    writer = _writer(store)
    for index in range(rows):
        body = f"---\nname: s{index}\ndescription: d\ntools: [absent_tool]\n---\nbody"
        asyncio.run(writer.awrite(skill_key(f"s{index}"), body))

    read = _read(store, _ACTOR)

    assert len(read.declared) == rows
    assert all(tools == frozenset({"absent_tool"}) for tools in read.declared.values())


def test_no_stored_skill_name_reaches_the_narrowing_log(
    stocked: InMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """No stored skill name reaches the per-turn narrowing log.

    A personal skill's name is a person's own words; the log counts the filed map only.
    """
    caplog.set_level(logging.DEBUG, logger="chemclaw.agent.langgraph_agent")

    _mounted(stocked, available={"compute_thermochemistry", "sample_conformers"})

    assert "offers" in caplog.text, "the narrowing line was not written, so this asserts nothing"
    for private in ("my-workup", "my-notes", "house-workup"):
        assert private not in caplog.text, f"{private} reached a log line"


def _paths_offered(backend: Any) -> dict[str, str]:
    """`{skill name: the path the model is told to read}` — the listing the prompt actually carries.

    Through `_skills_middleware`, since upstream's listing resolves which mount wins a name.
    """
    labelled = _labelled(_skill_dirs())
    loaded = _skills_middleware(backend, labelled, AgentProfile(name="default")).before_agent(
        {}, None, None
    )
    return {skill["name"]: skill["path"] for skill in loaded["skills_metadata"]}
