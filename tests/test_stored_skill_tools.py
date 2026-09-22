"""The two stored skills tiers are narrowed by the questions that apply to them, and by no others.

Every assertion here was driven failing first against the state this replaced, where the four
narrowings landed on the stored tiers almost exactly backwards: the three whose basis is a list of
*filed* names all applied — and `EnabledSkills` emptied both tiers outright, which both tiers' own
module docstrings said it would, as their reason for believing it did not — while
`ToolScopedSkills`, the one that asks a question a stored body can answer, ran and could not narrow,
because the declaration map it reads is built by globbing a directory.

`docs/decisions/D-2026-09-21-a-stored-tier-and-a-filed-tree-are-not-asked-the-same-question.md`
carries the argument. This file carries the measurements.
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
    _key,
    _writer,
    save_local_skill,
)
from chemclaw.agent.org_skills import ORG_SKILLS_ROOT, save_org_skill
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.scratchpad import scratchpad_backend
from chemclaw.agent.skill_access import skill_permits
from chemclaw.agent.skill_manifest import UNREADABLE_DECLARATION, declared_tools
from chemclaw.agent.skill_store import LISTING_PAGE
from chemclaw.agent.stored_skill_tools import StoredSkillTools, stored_skill_declarations
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity

_ACTOR = "alice-oid"

#: A personal skill that declares two tools, so it has something for the capability gate to read.
_DECLARING = (
    "---\nname: my-workup\ndescription: how I work up a Suzuki\n"
    "tools: [compute_thermochemistry, sample_conformers]\n---\n\nQuench cold.\n"
)

#: A personal skill declaring nothing, which the conservative rule leaves visible to everyone —
#: `ToolScopedSkills` hides on "every declared tool absent", and an empty declaration has no tools
#: to be absent. Kept in every arm below so a fix that hid the whole tier could not pass as a
#: narrowing.
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

    **Not `permits=lambda _: True`, which is what makes this file different from the other two.**
    `tests/test_local_skills.py` and `tests/test_org_skills.py` mount with a permissive narrowing
    because their subject is the read/write split; the subject here *is* the narrowing, so this
    walks the real trees and reads the real declarations.

    `stored=False` is the defect arm: it builds the same turn with the stored declarations withheld,
    which is the state before `agent/stored_skill_tools.py` existed.
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
    """The reader, under the identity a turn takes — it reads the ambient rather than an argument.

    Stamped rather than passed, because that is the fix for the defect a review found here: the
    reader resolving the personal namespace from a *different* spelling of one actor than the mount
    does. A test that could still pass an actor would be a test of a parameter that no longer
    exists.
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
    """The row, on both tiers, with the arm that proves it is a narrowing and not a deletion.

    Driven before the fix: the declaring skill was **served and listed** in a turn binding zero
    tools, while `declared_tools` reports 34 of 39 filed skills hidden by that same predicate in
    that same turn. A narrowing that runs and cannot narrow is worse than an absent one, because the
    prose says it is applied.
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
    """`EnabledSkills` names *shipped* skills, so applying it to a stored tier only empties it.

    Both stored tiers' module docstrings stated this as a fact about the code. Driven before the fix
    with `CHEMCLAW_SKILLS_ENABLED=development-report`: `ls('/mine/')` and `ls('/org/')` were both
    empty — on a tier that acts on everybody's turns, in a deployment that had asked for nothing of
    the kind. `make skill-validate` checks every name in that setting against the *discovered*
    trees, so no stored name can legally appear in it and there is no configuration that works
    around this.
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
    """`ProfileScopedSkills` stays in the stored narrowing, and that is decided rather than left.

    `skill_names: frozenset()` is a profile author writing down that this agent reaches no skill at
    all — the `skills-removed.yaml` control arm — and a tier escaping it would make every A/B result
    measured against that arm a comparison with a system that still had skills. It is the one of the
    three filed-basis narrowings that is kept, so it gets its own test rather than riding on
    `tests/test_org_skills.py`, which cannot see this partition.
    """
    arm = AgentProfile(name="default", skill_names=frozenset())
    backend = _mounted(
        stocked, profile=arm, available={"compute_thermochemistry", "sample_conformers"}
    )

    assert _listed(backend, LOCAL_SKILLS_ROOT) == []
    assert _listed(backend, ORG_SKILLS_ROOT) == []
    assert not _served(backend, LOCAL_SKILLS_ROOT, "my-notes")


def test_a_stored_skill_under_a_shipped_name_is_never_served(store: InMemoryStore) -> None:
    """The read side agrees with the write side's *discovered* basis — the row's own invariant.

    **The scenario is an enable-list, and that matters: the role gate the row named does not reach
    this.** Driven all four ways, with `UnreservedNames` on and off:

    - a `skill_role_gates` entry hides the stored copy **either way**, because `RoleScopedSkills` is
      in the stored narrowing too — so the row's own measurement was already closed, silently, when
      the stored mounts gained a backend predicate;
    - an enable-list that omits the name gives `/mine/deep-research/SKILL.md` with the rule off and
      nothing with it on. `EnabledSkills` is the one narrowing this change makes `filed`-only, so
      the stored tiers now survive an enable-list — which is exactly what re-opens the collision,
      and why these two fixes belong in one commit.

    An earlier version of this test used the role gate and therefore stayed green with
    `UnreservedNames` removed from the composition *and* with `reserved=` emptied at the production
    call site. A mutation review found both; this asserts the mechanism that actually binds.

    The body is written through the tier's own writer rather than `save_local_skill`, because that
    is the case: `validated_skill` refuses this name *now*, and what is left is a row stored before
    it did, or a name a later commit moved into `skills/`. Those are exactly the two the write door
    cannot reach.
    """
    contested = "deep-research"
    assert contested in shipped_skill_names(), (
        f"{contested} is not shipped, so this asserts nothing"
    )
    body = f"---\nname: {contested}\ndescription: my own digging\n---\n\nMine, not theirs.\n"
    asyncio.run(_writer(store, _ACTOR).awrite(_key(contested), body))
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
    """The row's stated measurement, driven and recorded as *not* the mechanism.

    Kept as its own test rather than deleted, because the next reader of `UnreservedNames` will
    reach for the scenario the backlog row named, and a green test saying "this one does not
    distinguish the arms" is what stops it being written as the guard a third time.
    `RoleScopedSkills` is in the stored narrowing, so the gate removes the personal copy on its own.
    """
    contested = "deep-research"
    body = f"---\nname: {contested}\ndescription: my own digging\n---\n\nMine.\n"
    asyncio.run(_writer(store, _ACTOR).awrite(_key(contested), body))
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
    """`requires:` on a stored body behaves as it does on a filed one — the other half of R6.

    Separate from the `tools:` case because the two quantifiers differ and only one of them was
    driven: `tools:` hides when *every* declared tool is absent, `requires:` hides when *any*
    required one is. A mutation review found that dropping `stored.required` from the merge entirely
    left the whole file green, because the existing assertions read the reader's map rather than the
    visibility it buys.
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
    """The filed entry wins a name held by both tiers, because one map feeds both predicates.

    The merge was written the other way first, with a comment claiming the collision could not
    happen because `UnreservedNames` removes it — true of the *stored* predicate, and silent about
    the filed one. Driven at that revision: a grandfathered `/mine/deep-research` declaring one tool
    nothing binds made the **reviewed** `deep-research` invisible in a turn that bound all twelve
    tools it declares. A stored declaration for a shipped name describes a body no turn can read, so
    it must not describe the body a turn does read.
    """
    contested = "deep-research"
    filed = declared_tools([d for _label, d in _labelled(_skill_dirs())])
    assert filed[contested], f"{contested} declares nothing, so this asserts nothing"
    body = f"---\nname: {contested}\ndescription: mine\ntools: [a_tool_nothing_binds]\n---\nMine.\n"
    asyncio.run(_writer(store, _ACTOR).awrite(_key(contested), body))

    offered = _paths_offered(_mounted(store, available=set(filed[contested])))

    assert offered[contested].startswith("/skills/"), (
        f"the reviewed {contested} is gone from the listing: served {offered.get(contested)}"
    )


def test_the_reserved_rule_does_not_reach_a_name_no_tree_ships(stocked: InMemoryStore) -> None:
    """A chemist's own vocabulary is theirs, and `UnreservedNames` must only close a collision.

    The defect arm of the test above would be a fix that hid the personal tier whenever a gate was
    configured, or whenever any name was reserved — so this pins the other side: the reserved set is
    every shipped name on every turn, and the personal skills here survive it.
    """
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
    """The three shared narrowings are the *same objects* in both halves, so they cannot drift.

    A derived guard rather than three hand-written cases: `SkillNarrowing` builds `filed` and
    `stored` from one tuple precisely so there is no second composition to keep in step, and this is
    what turns a future edit that rebuilt one of them into a red test rather than a divergence
    nobody measures.
    """
    base: dict[str, Any] = {"enabled": None, "declared": {}, "available": [], "gates": None}
    narrowing = skill_permits(**{**base, **kwargs})

    assert narrowing.filed(name) == narrowing.stored(name) is False


def test_an_unreadable_stored_body_is_scoped_to_nothing(store: InMemoryStore) -> None:
    """Fail closed, because a declaration may only ever cost a skill its visibility.

    `ToolScopedSkills` reads a *missing* entry as "declares nothing" and leaves the skill visible to
    everyone, so dropping an unparseable body would make it a **widening** — the defect
    `skill_manifest._declared_pair`'s own `except` arm exists to refuse, arriving through the stored
    door. Both write doors run `validated_skill`, so what makes this reachable is a body stored
    before a rule tightened; the writer is used directly to produce exactly that row.
    """
    asyncio.run(_writer(store, _ACTOR).awrite(_key("broken"), "---\ntools: {not: a list}\n---\nx"))
    asyncio.run(_writer(store, _ACTOR).awrite(_key("nameless"), "no frontmatter at all"))

    read = _read(store, _ACTOR)

    assert read.declared["broken"] == UNREADABLE_DECLARATION
    assert read.required["broken"] == UNREADABLE_DECLARATION
    assert read.declared["nameless"] == UNREADABLE_DECLARATION
    assert not _served(
        _mounted(store, available={"compute_thermochemistry"}), LOCAL_SKILLS_ROOT, "broken"
    )


def test_a_stored_body_and_a_filed_one_are_read_by_one_function(tmp_path: Any) -> None:
    """`declared_triple` is the single parse, so a `tools:` key means one thing in both tiers.

    Two readers would be two opinions about a declaration, in a filter whose whole contract is that
    a declaration can only ever cost a skill its visibility — so this drives the same five shapes
    through both doors and requires the same answer. The list comes from `_declared_pair`'s own
    docstring, which names what it was driven over.
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
    """The reader's two conditions mirror the mount's, or a gate narrows by an unreachable body.

    Asking about a tier a turn has no mount for would hide a skill for no reason the model could
    ever see; not asking about one it *does* mount is the gap this closes. So both directions are
    pinned.
    """
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
    """A padded actor spelling must not give the gate a different namespace from the mount.

    `core/identity_context.get_current_actor` returns the value **stripped**, "because the reader
    every gate and every namespace shares normalizes rather than leaving each of them to", and
    `scratchpad_backend` resolves the personal mount through it. The reader took an `actor` argument
    and `api/runner.py` passed the request's raw value, so `' alice-oid '` resolved two namespaces:
    the reader found nothing, the mount found the skill, and a *missing* declaration reads as
    "declares nothing" — the whole `/mine` tier unscoped, silently, which is the pre-change state.

    Driven through the mount rather than on the namespaces, because the namespaces agreeing is the
    mechanism and the tier being scoped is the property. The reader takes no actor now, so the two
    spellings cannot diverge; this is what turns re-introducing the parameter into a red test.
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
    """The **organisation's** body wins; this test asserted the opposite until a review drove it.

    Upstream resolves a collision *last-source-wins* and `_skills_middleware` orders its sources by
    ascending review depth — `/mine`, `/org`, then the reviewed trees — so the organisation's body
    is what a turn reads. `local_skills.save_local_skill` says so in the course of refusing the
    other direction: a personal skill under an org name "would never act, since `_skills_middleware`
    puts `/org` after `/mine`".

    Keyed the other way, one person's private document decided the visibility of a skill acting on
    everybody's turns. The collision is reachable in one direction and by design: `save_local_skill`
    refuses an existing org name, while `POST /skills/org` may publish over a name somebody already
    keeps privately, because the alternative is a deployment-wide publication blocked by one
    person's private vocabulary.

    **The served path is asserted beside the declaration**, which is the part whose absence let this
    pass: a test that reads only the map cannot notice that the map disagrees with the listing.
    """
    shared = "house-workup"
    asyncio.run(save_org_skill(store, shared, _ORG, activated_by="admin-oid"))
    mine = f"---\nname: {shared}\ndescription: my version\ntools: [sample_conformers]\n---\nMine.\n"
    asyncio.run(_writer(store, _ACTOR).awrite(_key(shared), mine))

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
    """More rows than one page, because an un-paged read answers ten and reads as the whole tier.

    `BaseStore.asearch` defaults to `limit=10`; the personal tier was measured listing ten of a
    chemist's twelve skills, with the two beyond the page undeletable through the route that exists
    to remove them. Three callers now share `skill_store.paged_items`, and a narrowing that saw one
    page would silently leave every skill past it unscoped — which is the fail-*open* direction.
    """
    rows = LISTING_PAGE + 7
    writer = _writer(store, _ACTOR)
    for index in range(rows):
        body = f"---\nname: s{index}\ndescription: d\ntools: [absent_tool]\n---\nbody"
        asyncio.run(writer.awrite(_key(f"s{index}"), body))

    read = _read(store, _ACTOR)

    assert len(read.declared) == rows
    assert all(tools == frozenset({"absent_tool"}) for tools in read.declared.values())


def test_no_stored_skill_name_reaches_the_narrowing_log(
    stocked: InMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A personal skill's name is a person's own words, and this line is written on every turn.

    `local_skills._count_a_local_load` refuses to put one in a metric label for exactly this reason
    — "a label would mint a series per private project name in a shared exposition that no erasure
    reaches" — and `_log_narrowing`'s `skills=` field is the same hazard at DEBUG. The narrowing now
    reads both maps; the log keeps counting the filed one, which is also what "discovered" means.
    """
    caplog.set_level(logging.DEBUG, logger="chemclaw.agent.langgraph_agent")

    _mounted(stocked, available={"compute_thermochemistry", "sample_conformers"})

    assert "offers" in caplog.text, "the narrowing line was not written, so this asserts nothing"
    for private in ("my-workup", "my-notes", "house-workup"):
        assert private not in caplog.text, f"{private} reached a log line"


def _paths_offered(backend: Any) -> dict[str, str]:
    """`{skill name: the path the model is told to read}` — the listing the prompt actually carries.

    Through `_skills_middleware` rather than off a mount's `ls`, because the collision this file's
    R7 case is about is resolved by upstream's *listing*: two mounts holding one name produce one
    entry, and which one is the whole question.
    """
    labelled = _labelled(_skill_dirs())
    loaded = _skills_middleware(backend, labelled, AgentProfile(name="default")).before_agent(
        {}, None, None
    )
    return {skill["name"]: skill["path"] for skill in loaded["skills_metadata"]}
