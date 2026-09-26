"""The organisation's skills: read by every turn, written by no turn, revertible by an admin.

`D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius` puts this tier behind the privileged
role because it acts on everybody, and
`D-2026-09-20-a-revert-is-a-pointer-when-there-is-no-commit-to-revert` owes it the rollback property
`skills/` gets from being git-resident. These are those two claims driven rather than restated.
"""

import asyncio
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.langgraph_agent import skills_backend
from chemclaw.agent.local_skills import SkillRefused, save_local_skill
from chemclaw.agent.org_skills import (
    ORG_SKILLS_LABEL,
    ORG_SKILLS_ROOT,
    activate_org_version,
    content_hash,
    list_org_skills,
    list_org_versions,
    org_skills_backend,
    org_skills_namespace,
    read_org_skill,
    retire_org_skill,
    save_org_skill,
)
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.scratchpad import scratchpad_backend
from chemclaw.agent.skill_access import SkillNarrowing
from chemclaw.agent.skill_backend import SkillsReadOnlyRefusal
from chemclaw.agent.skill_store import PermittedStoreBackend
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity


def _body(name: str, description: str = "how this deployment does it") -> str:
    """One valid `SKILL.md` under `name`."""
    return f"---\nname: {name}\ndescription: {description}\n---\n\nDo it this way.\n"


_BODY = _body("house-workup")


@pytest.fixture
def store() -> InMemoryStore:
    """A store standing in for the deployment's `AsyncPostgresStore`, same `BaseStore` contract."""
    return InMemoryStore()


def _backend(store: Any) -> PermittedStoreBackend:
    """The organisation's tier as a backend, permitting every name."""
    return org_skills_backend(store, lambda _name: True)


def _mounted(store: Any, actor: str, permits: SkillNarrowing | None = None) -> Any:
    """The backend a turn for `actor` would be given, mounted as a turn mounts it."""
    tokens = set_current_identity(actor, frozenset())
    try:
        return scratchpad_backend(
            skills_backend(AgentProfile(name="default"), []),
            store,
            permits=permits or SkillNarrowing.permissive(),
        )
    finally:
        reset_current_identity(tokens)


def _sources(backend: Any, profile: AgentProfile) -> list[str]:
    """The source paths `SkillsMiddleware` would advertise, as strings.

    Upstream normalises a `(path, label)` pair into its own object, so the comparison is on the
    path — the same shape `tests/test_skill_access.py` reads for the tier beside this one.
    """
    from chemclaw.agent.langgraph_agent import _skills_middleware

    return [str(source) for source in _skills_middleware(backend, [], profile).sources]


def test_one_organisation_skill_reaches_every_chemists_turn(store: InMemoryStore) -> None:
    """The property that makes this a *tier* rather than a second personal one.

    Two actors, one namespace, one body — driven through the mount a turn really gets rather than
    through the writer, because "acts on everyone" is a claim about what a turn can read.
    """
    asyncio.run(save_org_skill(store, "house-workup", _BODY, activated_by="admin-oid"))

    for actor in ("alice-oid", "bob-oid"):
        read = _mounted(store, actor).read(f"{ORG_SKILLS_ROOT}house-workup/SKILL.md")
        assert read.error is None, actor
        assert "Do it this way." in str(read.file_data), actor


def test_the_tier_needs_a_store_and_not_an_actor(store: InMemoryStore) -> None:
    """Mounted for a turn with no ambient identity, unlike the two per-actor routes beside it.

    The organisation's judgment belongs to nobody, so there is no namespace to derive from an actor
    and no reason to withhold it from a turn that has none. `agent/scratchpad.py` mounts it outside
    the `and actor` branch precisely so this is true, and an absence is not a decision until
    something asserts it.
    """
    from chemclaw.agent.local_skills import LOCAL_SKILLS_ROOT
    from chemclaw.agent.scratchpad import MEMORY_ROOT

    anonymous = _mounted(store, "")
    assert ORG_SKILLS_ROOT in anonymous.routes
    assert LOCAL_SKILLS_ROOT not in anonymous.routes, "a personal tier needs somebody to own it"
    assert MEMORY_ROOT not in anonymous.routes

    skills = skills_backend(AgentProfile(name="default"), [])
    assert (
        ORG_SKILLS_ROOT
        not in scratchpad_backend(skills, None, permits=SkillNarrowing.permissive()).routes
    ), "with no store there is nowhere to keep one"


def test_no_turn_may_write_the_organisations_tier(store: InMemoryStore) -> None:
    """`SkillsReadOnlyRefusal` on all eight write verbs, sync and async alike.

    Eight rather than four because `StoreBackend`'s async verbs are native rather than `to_thread`
    wrappers — the asymmetry the personal tier was caught by, where three async holes sat beside one
    working refusal and a spot check passed.
    """
    backend = _backend(store)

    for verb, args in (
        ("write", ("/x/SKILL.md", "body")),
        ("edit", ("/x/SKILL.md", "a", "b")),
        ("delete", ("/x/SKILL.md",)),
        ("upload_files", (["/x/SKILL.md"],)),
    ):
        with pytest.raises(SkillsReadOnlyRefusal):
            getattr(backend, verb)(*args)

    for verb, args in (
        ("awrite", ("/x/SKILL.md", "body")),
        ("aedit", ("/x/SKILL.md", "a", "b")),
        ("adelete", ("/x/SKILL.md",)),
        ("aupload_files", (["/x/SKILL.md"],)),
    ):
        with pytest.raises(SkillsReadOnlyRefusal):
            asyncio.run(getattr(backend, verb)(*args))


def test_every_method_this_tier_exposes_is_either_a_read_or_a_refusal(
    store: InMemoryStore,
) -> None:
    """Derived from the surface, not from a written list, because the write half grows.

    The same derivation `tests/test_local_skills.py` runs over the other stored tier, and for its
    reason: deepagents 0.7 added `delete` to the protocol and the reviewed tree inherited a working
    one until a derived test caught it. Both tiers now share
    `agent/skill_store.PermittedStoreBackend`, so this is the second reader of one surface rather
    than a second list to keep in step — but it is run twice on purpose, because what each tier
    *inherits* is what a turn can reach and a subclass could diverge.
    """
    from deepagents.backends import StoreBackend
    from deepagents.backends.protocol import BackendProtocol

    backend = _backend(store)
    asyncio.run(save_org_skill(store, "house-workup", _BODY, activated_by="admin-oid"))

    reads: dict[str, Any] = {
        "ls": lambda: backend.ls("/"),
        "als": lambda: asyncio.run(backend.als("/")),
        "read": lambda: backend.read("/house-workup/SKILL.md"),
        "aread": lambda: asyncio.run(backend.aread("/house-workup/SKILL.md")),
        "glob": lambda: backend.glob("**/SKILL.md"),
        "aglob": lambda: asyncio.run(backend.aglob("**/SKILL.md")),
        "grep": lambda: backend.grep("Do it"),
        "agrep": lambda: asyncio.run(backend.agrep("Do it")),
        "download_files": lambda: backend.download_files(["/house-workup/SKILL.md"]),
        "adownload_files": lambda: asyncio.run(backend.adownload_files(["/house-workup/SKILL.md"])),
    }
    refusals: dict[str, Any] = {
        "write": lambda: backend.write("/x/SKILL.md", "b"),
        "awrite": lambda: asyncio.run(backend.awrite("/x/SKILL.md", "b")),
        "edit": lambda: backend.edit("/x/SKILL.md", "a", "b"),
        "aedit": lambda: asyncio.run(backend.aedit("/x/SKILL.md", "a", "b")),
        "delete": lambda: backend.delete("/x/SKILL.md"),
        "adelete": lambda: asyncio.run(backend.adelete("/x/SKILL.md")),
        "upload_files": lambda: backend.upload_files(["/x/SKILL.md"]),
        "aupload_files": lambda: asyncio.run(backend.aupload_files(["/x/SKILL.md"])),
    }

    declared = {
        name
        for source in (BackendProtocol, StoreBackend)
        for name in vars(source)
        if not name.startswith("_") and callable(getattr(source, name, None))
    }
    # `execute`/`aexecute` are withheld from every backend this repository builds
    # (`agent/scratchpad.scratchpad_tools`), and `id` is a property rather than a verb.
    untriaged = declared - set(reads) - set(refusals) - {"execute", "aexecute", "id"}
    assert not untriaged, (
        "upstream exposes a verb this tier has not triaged into a read or a refusal: "
        f"{sorted(untriaged)}"
    )

    for name, refuse in refusals.items():
        with pytest.raises(SkillsReadOnlyRefusal):
            refuse()
        assert name in declared

    for name, probe in reads.items():
        probe()
        assert name in declared


def test_a_profile_that_narrows_to_nothing_reaches_no_body(store: InMemoryStore) -> None:
    """The gate is at the backend, not only in the prompt — the hole this tier shipped without.

    `docs/planning/BACKLOG.md` recorded the personal tier narrowed in the prompt alone: `ls`
    returned the names and `read_file` returned the bodies to a profile that advertised none. A
    tier acting on *everyone* with the same gap would make the `skills-removed.yaml` control arm
    meaningless for every deployment that published one, so this drives the predicate rather than
    the listing.
    """
    asyncio.run(save_org_skill(store, "house-workup", _BODY, activated_by="admin-oid"))
    backend = org_skills_backend(store, lambda _name: False)

    assert backend.read("/house-workup/SKILL.md").error is not None
    assert not backend.ls("/").entries
    assert not backend.glob("**/SKILL.md").matches
    assert not backend.download_files(["/house-workup/SKILL.md"])
    assert asyncio.run(backend.aread("/house-workup/SKILL.md")).error is not None


def test_a_refused_read_does_not_say_whether_the_skill_exists(store: InMemoryStore) -> None:
    """Otherwise the gate is an enumeration oracle over the organisation's own configuration."""
    asyncio.run(save_org_skill(store, "house-workup", _BODY, activated_by="admin-oid"))
    backend = org_skills_backend(store, lambda _name: False)

    real = backend.read("/house-workup/SKILL.md")
    imagined = backend.read("/no-such-skill/SKILL.md")
    assert real.error == imagined.error


def test_the_row_cap_is_refused_rather_than_evicted(store: InMemoryStore) -> None:
    """Judgment an administrator published may not vanish because somebody added one more.

    The opposite of the version cap below, and the asymmetry is the decision: this bounds the prompt
    every chemist pays for, so the answer is "retire one first" rather than a silent drop.
    """
    original = settings.agent_org_skills_max
    settings.agent_org_skills_max = 2
    try:
        asyncio.run(save_org_skill(store, "first", _body("first"), activated_by="admin-oid"))
        asyncio.run(save_org_skill(store, "second", _body("second"), activated_by="admin-oid"))
        with pytest.raises(SkillRefused) as refused:
            asyncio.run(save_org_skill(store, "third", _body("third"), activated_by="admin-oid"))
        assert refused.value.conflict
        assert sorted(asyncio.run(list_org_skills(store))) == ["first", "second"]

        # Replacing one already held is not a new row, so it is allowed at the cap — otherwise
        # nobody could correct the very skills the cap is full of.
        asyncio.run(
            save_org_skill(store, "first", _body("first", "corrected"), activated_by="admin-oid")
        )
        assert "corrected" in (asyncio.run(read_org_skill(store, "first")) or "")
    finally:
        settings.agent_org_skills_max = original


def test_a_bad_org_skill_is_one_call_away_from_the_bytes_that_stood_before(
    store: InMemoryStore,
) -> None:
    """The rollback property `skills/` gets from git, driven on the tier that has no commits.

    **Byte-identity is the assertion that matters.** A test checking only that the name resolves
    after a revert passes on a re-authoring — an administrator retyping last week's text — which is
    precisely what the version namespace exists to make unnecessary.
    """
    good = _body("house-workup", "the one that worked")
    bad = _body("house-workup", "the one that did not")
    asyncio.run(save_org_skill(store, "house-workup", good, activated_by="admin-oid"))
    asyncio.run(save_org_skill(store, "house-workup", bad, activated_by="admin-oid"))

    assert asyncio.run(read_org_skill(store, "house-workup")) == bad
    versions = asyncio.run(list_org_versions(store, "house-workup"))
    assert {version.content_hash for version in versions} == {
        content_hash(good),
        content_hash(bad),
    }
    assert {version.activated_by for version in versions} == {"admin-oid"}

    reverted = asyncio.run(
        activate_org_version(store, "house-workup", content_hash(good), activated_by="other-admin")
    )
    assert reverted
    assert asyncio.run(read_org_skill(store, "house-workup")) == good, "not byte-identical"

    # The body that was rolled back is still held, so the revert is itself reversible.
    assert content_hash(bad) in {
        version.content_hash for version in asyncio.run(list_org_versions(store, "house-workup"))
    }


def test_a_revert_cannot_name_a_document_that_was_never_active(store: InMemoryStore) -> None:
    """The pointer can only point at history, which is the difference from an ordinary write."""
    asyncio.run(save_org_skill(store, "house-workup", _BODY, activated_by="admin-oid"))

    assert not asyncio.run(
        activate_org_version(
            store, "house-workup", content_hash("never activated"), activated_by="admin-oid"
        )
    )
    assert asyncio.run(read_org_skill(store, "house-workup")) == _BODY


def test_retiring_an_org_skill_leaves_its_history(store: InMemoryStore) -> None:
    """Retiring and reverting are different acts, and only the first one is a delete.

    Removing the active body takes the deployment to *no* judgment rather than to last week's, so
    this stays reversible: the versions survive and any of them can be brought back.
    """
    asyncio.run(save_org_skill(store, "house-workup", _BODY, activated_by="admin-oid"))
    assert asyncio.run(retire_org_skill(store, "house-workup", retired_by="admin-oid"))

    assert asyncio.run(list_org_skills(store)) == []
    assert asyncio.run(read_org_skill(store, "house-workup")) is None
    assert asyncio.run(list_org_versions(store, "house-workup")), "the history went with it"

    assert asyncio.run(
        activate_org_version(store, "house-workup", content_hash(_BODY), activated_by="admin-oid")
    )
    assert asyncio.run(read_org_skill(store, "house-workup")) == _BODY


def test_the_version_history_is_evicted_rather_than_refused(store: InMemoryStore) -> None:
    """A cap on history must never stop a fix being published — the opposite of the row cap.

    Oldest-activated goes, which is `scratchpad.BoundedStoreBackend`'s tiebreak taken for its
    reason: it is the only ordering the store carries, and the version anybody reverts to is a
    recent one.
    """
    original = settings.agent_org_skill_versions_max
    settings.agent_org_skill_versions_max = 2
    try:
        bodies = [_body("house-workup", f"revision {n}") for n in range(3)]
        for body in bodies:
            asyncio.run(save_org_skill(store, "house-workup", body, activated_by="admin-oid"))

        held = {
            version.content_hash
            for version in asyncio.run(list_org_versions(store, "house-workup"))
        }
        assert len(held) == 2, "the cap did not bind"
        assert content_hash(bodies[0]) not in held, "the oldest activation should have gone"
        assert content_hash(bodies[2]) in held, "the newest must stay revertible"
    finally:
        settings.agent_org_skill_versions_max = original


def test_a_personal_skill_may_not_take_an_organisation_name(store: InMemoryStore) -> None:
    """Refused in the writer, so both doors into the personal tier have it.

    A personal skill by a name the organisation publishes would never act — `_skills_middleware`
    puts `/org` after `/mine` and upstream resolves a collision last-source-wins — so writing one is
    writing judgment that silently does nothing.
    """
    asyncio.run(save_org_skill(store, "house-workup", _BODY, activated_by="admin-oid"))

    with pytest.raises(SkillRefused) as refused:
        asyncio.run(save_local_skill(store, "alice-oid", "house-workup", _BODY))
    assert refused.value.conflict


def test_an_organisation_skill_may_take_a_name_a_chemist_already_uses(
    store: InMemoryStore,
) -> None:
    """The asymmetry stated in the other direction, and the collision resolved the stated way.

    An administrator cannot see one person's private vocabulary and must not be blocked by it, so
    the publication succeeds — and because the order is ascending review depth, it is the
    organisation's body a turn is handed.
    """
    mine = _body("house-workup", "what I do")
    theirs = _body("house-workup", "what we all do")
    asyncio.run(save_local_skill(store, "alice-oid", "house-workup", mine))
    asyncio.run(save_org_skill(store, "house-workup", theirs, activated_by="admin-oid"))

    backend = _mounted(store, "alice-oid")
    served = backend.read(f"{ORG_SKILLS_ROOT}house-workup/SKILL.md")
    assert "what we all do" in str(served.file_data)
    # Her own document is untouched and still visible on the route that lists it.
    from chemclaw.agent.local_skills import read_local_skill

    assert asyncio.run(read_local_skill(store, "alice-oid", "house-workup")) == mine


def test_the_tier_is_advertised_only_when_it_is_mounted(store: InMemoryStore) -> None:
    """A source with no route publishes an empty tier to the model on every turn.

    `_skills_middleware` derives its sources from `backend.routes` for exactly this reason, and the
    condition differs from the personal tier's — a store alone, with no actor.
    """
    with_store = _sources(_mounted(store, "alice-oid"), AgentProfile(name="default"))
    assert f"/{ORG_SKILLS_LABEL}" in with_store

    skills = skills_backend(AgentProfile(name="default"), [])
    without = _sources(
        scratchpad_backend(skills, None, permits=SkillNarrowing.permissive()),
        AgentProfile(name="default"),
    )
    assert f"/{ORG_SKILLS_LABEL}" not in without


def test_the_control_arm_that_removes_skills_removes_this_tier_too(store: InMemoryStore) -> None:
    """`skill_names: frozenset()` is a profile author writing down that this agent reaches none.

    The personal tier used to escape that and the A/B arms it contaminated were reported anyway.
    A tier acting on everybody escaping it would be the same defect over a larger blast radius.
    """
    narrowed = AgentProfile(name="default", skill_names=frozenset())
    assert f"/{ORG_SKILLS_LABEL}" not in _sources(_mounted(store, "alice-oid"), narrowed)


def test_the_namespaces_do_not_collide(store: InMemoryStore) -> None:
    """Three tiers, three first components, so each is separately countable and erasable.

    The organisation's carries no actor at all — which is what makes it everybody's, and what keeps
    the system prefix byte-identical between two sessions.
    """
    from chemclaw.agent.local_skills import local_skills_namespace
    from chemclaw.agent.scratchpad import memory_namespace

    firsts = {
        org_skills_namespace()[0],
        local_skills_namespace("alice-oid")[0],
        memory_namespace("alice-oid")[0],
    }
    assert len(firsts) == 3, firsts
    assert len(org_skills_namespace()) == 1, "an actor component would make it somebody's"


def test_a_key_that_names_no_skill_lists_as_nothing_in_either_tier(store: InMemoryStore) -> None:
    """Both listings go through one strict parse, so `/SKILL.md` is not a skill named `""`.

    The two tiers each sliced `key[1:-len("/SKILL.md")]` off anything ending in the suffix, so a
    body at `/SKILL.md` listed as an empty name — a row a route shows and no route can address —
    while `stored_skill_tools` already refused the same key. One parse now answers for all three.
    """
    from chemclaw.agent.local_skills import list_local_skills, local_skills_namespace

    for namespace in (org_skills_namespace(), local_skills_namespace("alice-oid")):
        store.put(namespace, "/SKILL.md", {"content": "stray", "encoding": "utf-8"})
        store.put(namespace, "/real/SKILL.md", {"content": "body", "encoding": "utf-8"})
    assert asyncio.run(list_org_skills(store)) == ["real"]
    assert asyncio.run(list_local_skills(store, "alice-oid")) == ["real"]
    assert asyncio.run(read_org_skill(store, "real")) == "body"
