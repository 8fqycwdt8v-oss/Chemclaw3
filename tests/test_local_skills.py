"""A chemist's own skills: read by their turns, written by no turn, and visible to them.

The tier is exempt from review on conditions stated as requirements; these tests drive them.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.langgraph_agent import skills_backend
from chemclaw.agent.local_skills import (
    LOCAL_SKILLS_LABEL,
    LOCAL_SKILLS_ROOT,
    delete_local_skill,
    list_local_skills,
    local_skills_backend,
    local_skills_namespace,
    local_skills_prefix,
    read_local_skill,
    save_local_skill,
)
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.scratchpad import memory_namespace, scratchpad_backend
from chemclaw.agent.skill_access import SkillNarrowing
from chemclaw.agent.skill_backend import SkillsReadOnlyRefusal
from chemclaw.agent.skill_store import PermittedStoreBackend
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity

_BODY = "---\nname: my-workup\ndescription: how I work up a Suzuki\n---\n\nQuench cold.\n"


@pytest.fixture
def store() -> InMemoryStore:
    """A store standing in for the deployment's `AsyncPostgresStore`, same `BaseStore` contract."""
    return InMemoryStore()


def _backend(store: Any, actor: str = "alice-oid") -> PermittedStoreBackend:
    """One chemist's tier as a backend, permitting every name.

    `permits` is required, not defaulted, so it is stated permissive here for tests about the
    read/write split.
    """
    return local_skills_backend(store, actor, lambda _name: True)


def _mounted(store: Any, actor: str) -> Any:
    """The backend a turn for `actor` would be given, with the tier mounted as a turn mounts it."""
    tokens = set_current_identity(actor, frozenset())
    try:
        return scratchpad_backend(
            skills_backend(AgentProfile(name="default"), []),
            store,
            permits=SkillNarrowing.permissive(),
        )
    finally:
        reset_current_identity(tokens)


def test_a_chemists_own_skill_reaches_their_turn(store: InMemoryStore) -> None:
    """A chemist's own skill reaches their turn, driven through the mount rather than the store.

    `scratchpad_backend` is where the store and the turn's actor meet, so reading the store directly
    would not prove a turn can reach the rows.
    """
    asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY))

    backend = _mounted(store, "alice-oid")

    assert LOCAL_SKILLS_ROOT in backend.routes
    result = backend.read(f"{LOCAL_SKILLS_ROOT}my-workup/SKILL.md")
    assert result.error is None
    assert "Quench cold." in str(result.file_data)


def test_one_chemists_skill_never_reaches_anothers_turn(store: InMemoryStore) -> None:
    """One chemist's skill never reaches another's turn, structurally.

    The namespace closes over the turn's own actor, so bob's turn is mounted on a namespace that
    does not contain alice's skill; there is no gate to misconfigure.
    """
    asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY))

    assert asyncio.run(list_local_skills(store, "alice-oid")) == ["my-workup"]
    assert asyncio.run(list_local_skills(store, "bob-oid")) == []
    bobs = _mounted(store, "bob-oid").read(f"{LOCAL_SKILLS_ROOT}my-workup/SKILL.md")
    assert bobs.error is not None


def test_no_turn_may_write_its_owners_skills(store: InMemoryStore) -> None:
    """No turn may write its owner's skills: `SkillsReadOnlyRefusal` holds for `StoreBackend` too.

    The shared tree gets the refusal from a `FilesystemBackend`; this stored tier must hold it as
    well or "no agent path writes a skill" would be true of only one tier.
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


def test_every_method_this_tier_exposes_is_either_a_read_or_a_refusal(store: InMemoryStore) -> None:
    """Every method this tier exposes is either a read or a refusal, derived from the surface.

    An upstream addition must be triaged into one half before this passes; a hand-written list would
    miss it. Derived from `StoreBackend` as well as `BackendProtocol`, since `PermittedStoreBackend`
    inherits methods the protocol may not declare.
    """
    from deepagents.backends import StoreBackend
    from deepagents.backends.protocol import BackendProtocol

    backend = _backend(store)
    asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY))

    reads: dict[str, Any] = {
        "ls": lambda: backend.ls("/"),
        "read": lambda: backend.read("/my-workup/SKILL.md"),
        "glob": lambda: backend.glob("**/*"),
        "grep": lambda: backend.grep("Quench"),
        "download_files": lambda: backend.download_files(["/my-workup/SKILL.md"]),
        "als": lambda: backend.als("/"),
        "aread": lambda: backend.aread("/my-workup/SKILL.md"),
        "aglob": lambda: backend.aglob("**/*"),
        "agrep": lambda: backend.agrep("Quench"),
        "adownload_files": lambda: backend.adownload_files(["/my-workup/SKILL.md"]),
    }
    writes: dict[str, Any] = {
        "write": lambda: backend.write("/x/SKILL.md", "body"),
        "edit": lambda: backend.edit("/x/SKILL.md", "a", "b"),
        "delete": lambda: backend.delete("/x/SKILL.md"),
        "upload_files": lambda: backend.upload_files([]),
        "awrite": lambda: backend.awrite("/x/SKILL.md", "body"),
        "aedit": lambda: backend.aedit("/x/SKILL.md", "a", "b"),
        "adelete": lambda: backend.adelete("/x/SKILL.md"),
        "aupload_files": lambda: backend.aupload_files([]),
    }

    surface = {
        name for name in (*dir(BackendProtocol), *dir(StoreBackend)) if not name.startswith("_")
    }
    unclassified = surface - set(reads) - set(writes)
    assert not unclassified, (
        f"{sorted(unclassified)} is neither probed as a read nor refused as a write on this tier; "
        "upstream added a method and it needs triaging before this file means anything"
    )

    # Every read probe is invoked: classifying a name proves nothing, and only calling it shows that
    # a verb in `reads` has not become a write.
    for name, call in reads.items():
        try:
            outcome = call()
            if asyncio.iscoroutine(outcome):
                outcome = asyncio.run(outcome)
        except SkillsReadOnlyRefusal:  # pragma: no cover - the failure this loop exists to catch
            pytest.fail(f"{name} is classified as a read and refuses like a write")
        assert outcome is not None, f"{name} answered nothing"
        assert name in surface, f"{name} is not on the surface any more"

    # Every write verb refuses, async twins included. **Eight overrides rather than four**, because
    # `StoreBackend`'s async verbs are native against the store rather than `to_thread` wrappers —
    # the shared tree's inheritance argument is about `FilesystemBackend` and does not travel.
    for name, call in writes.items():
        with pytest.raises(SkillsReadOnlyRefusal):
            result = call()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        assert name in surface, f"{name} is not on the surface any more"


def test_the_tier_is_advertised_only_when_it_is_mounted(store: InMemoryStore) -> None:
    """The tier is advertised only when it is mounted.

    An advertised `/mine` with no route resolves to the composite's default `StateBackend`, an empty
    directory the model would be told about every turn; the middleware derives sources from routes.
    """
    with_store = _mounted(store, "alice-oid")
    without_store = _mounted(None, "alice-oid")
    actorless = _mounted(store, "")

    assert LOCAL_SKILLS_ROOT in with_store.routes
    assert LOCAL_SKILLS_ROOT not in without_store.routes
    assert LOCAL_SKILLS_ROOT not in actorless.routes


def test_the_two_store_tiers_do_not_share_a_namespace() -> None:
    """Separately erasable and separately countable, so a bug in one cannot serve the other's rows.

    Asserted on the first component rather than the whole tuple, because the digest is the same
    function for both and the *prefix* is what `agent/leaver.py` deletes by.
    """
    memories = memory_namespace("alice-oid")
    own = local_skills_namespace("alice-oid")

    assert memories[0] != own[0]
    assert memories[1] == own[1], "the actor digest should be the one function, not two"
    assert local_skills_prefix("alice-oid") == ".".join(own)
    assert not local_skills_prefix("alice-oid").startswith(".".join(memories))


def test_a_departing_chemists_own_skills_are_erased_with_their_memories() -> None:
    """A departing chemist's own skills are erased with their memories.

    `store` has no actor column, so a column-derived completeness check cannot see these rows; the
    namespace is how they are found. Asserted through `store_prefixes`, which `erase_actor` calls.
    """
    from chemclaw.agent.leaver import store_prefixes

    prefixes = store_prefixes(["alice-oid", "unverified:alice"])

    for actor in ("alice-oid", "unverified:alice"):
        assert local_skills_prefix(actor) in prefixes, f"{actor}'s own skills are not erased"
        assert ".".join(memory_namespace(actor)) in prefixes, f"{actor}'s memories are not erased"


def test_a_skill_is_replaced_rather_than_versioned(store: InMemoryStore) -> None:
    """What is acting on my turns must be a question with one answer per name."""
    asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY))
    asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY.replace("cold", "warm")))

    assert asyncio.run(list_local_skills(store, "alice-oid")) == ["my-workup"]
    kept = asyncio.run(read_local_skill(store, "alice-oid", "my-workup"))
    assert kept is not None and "warm" in kept and "cold" not in kept


def test_a_chemist_can_remove_what_is_acting_on_them(store: InMemoryStore) -> None:
    """The half that makes inspection worth having.

    An inspectable behaviour change nobody can withdraw is the worse bargain of the two: the person
    has learned something is acting on them and still cannot stop it.
    """
    asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY))

    assert asyncio.run(delete_local_skill(store, "alice-oid", "my-workup")) is True
    assert asyncio.run(delete_local_skill(store, "alice-oid", "my-workup")) is False
    assert asyncio.run(list_local_skills(store, "alice-oid")) == []
    assert _mounted(store, "alice-oid").read(f"{LOCAL_SKILLS_ROOT}my-workup/SKILL.md").error


def test_the_size_bound_is_larger_than_anything_this_repository_ships() -> None:
    """A bound derived from the shared tree rather than picked, and checked against it.

    If a skill lands in `skills/` that this tier could not hold, the bound is wrong — a chemist
    should be able to write judgment as substantial as anything reviewed into the shared tree.
    """
    from pathlib import Path

    from chemclaw.core.config import settings

    shipped = [path.stat().st_size for path in Path("skills").glob("*/SKILL.md")]

    assert shipped, "no shipped skills were found, so this asserts nothing"
    assert settings.agent_local_skill_max_chars > max(shipped)


def test_the_listing_answers_for_a_tier_larger_than_one_page(
    store: InMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing answers for a tier larger than one page.

    `store.asearch(namespace)` without `limit` returns 10, so an unpaged listing would show a short
    answer and hide skills from the only route that deletes them. Driven past several pages and
    asserted against the mount as well, since the two must agree.
    """
    # The row cap lives in the writer now, and this test is about the *listing* rather than about
    # the cap — so it is lifted deliberately here instead of the corpus being shrunk to fit, which
    # would take the test below one page and stop exercising the walk at all.
    monkeypatch.setattr(settings, "agent_local_skills_max", 1_000)
    names = [f"skill-{index:03d}" for index in range(250)]
    for name in names:
        asyncio.run(save_local_skill(store, "alice-oid", name, _BODY.replace("my-workup", name)))

    listed = asyncio.run(list_local_skills(store, "alice-oid"))

    assert listed == sorted(names)
    mounted = _mounted(store, "alice-oid")
    for name in (names[0], names[11], names[-1]):
        assert mounted.read(f"{LOCAL_SKILLS_ROOT}{name}/SKILL.md").error is None
        assert asyncio.run(delete_local_skill(store, "alice-oid", name)) is True


def test_the_label_carries_no_identity() -> None:
    """The mount path carries no identity.

    It appears in every turn's system prompt and logs; the namespace carries the actor digest, but a
    digest in the path would be a stable identifier for a person in the prompt.
    """
    assert LOCAL_SKILLS_ROOT == f"/{LOCAL_SKILLS_LABEL}/"
    assert local_skills_namespace("alice-oid")[1] not in LOCAL_SKILLS_ROOT


def test_a_write_outside_the_tool_chain_is_not_a_write_without_a_record(
    store: InMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A write through the skills route leaves a record, so it is not a silent store write.

    The route is called by a person, not a turn, so the tool-chain controls do not apply, but a
    direct store write must still not happen without a record. Both lifecycle halves are checked.
    """
    import logging as _logging

    with caplog.at_level(_logging.INFO):
        asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY))
        asyncio.run(delete_local_skill(store, "alice-oid", "my-workup"))

    assert "local_skill.saved" in caplog.text
    assert "local_skill.removed" in caplog.text
    assert "my-workup" in caplog.text
    # The body never reaches the log — only that a skill by that name changed, and how large it was.
    assert "Quench cold." not in caplog.text


def test_a_reviewed_skill_wins_a_name_a_personal_one_also_claims(store: InMemoryStore) -> None:
    """A reviewed skill wins a name a personal one also claims.

    Upstream resolves collisions last-source-wins. `POST /skills/mine` refuses a collision it can
    see, but not a shipped skill added later; reviewed judgment winning is the safer silence.
    Asserted on the order of sources the middleware is given, since an `append` would reverse the
    outcome.
    """
    from chemclaw.agent.langgraph_agent import (
        _labelled,
        _skill_dirs,
        _skills_middleware,
        shipped_skill_names,
    )

    middleware = _skills_middleware(
        _mounted(store, "alice-oid"), _labelled(_skill_dirs()), AgentProfile(name="default")
    )
    # Upstream normalises a `(path, label)` source to a bare path when the label it would derive
    # matches, so the shape is read rather than assumed.
    paths = [source if isinstance(source, str) else source[0] for source in middleware.sources]

    assert paths[0] == f"/{LOCAL_SKILLS_LABEL}", (
        "the chemist's own tier must come first so a reviewed skill wins a name collision; "
        f"the sources are {paths}"
    )
    assert len(paths) > 1, "only one source, so this asserts nothing about precedence"

    # The outcome itself, since the order is only the mechanism. The contested name is taken from
    # what the shared tier actually serves, not `shipped_skill_names()`: a discovered name narrowed
    # out of the listing has no reviewed copy to win, so it would measure the narrowing, not
    # precedence. Nothing is saved yet, so every entry here is shared.
    listed = middleware.before_agent({}, None, None)["skills_metadata"]
    contested = sorted(skill["name"] for skill in listed)[0]
    assert contested in shipped_skill_names(), (
        f"{contested} is listed but is not a shipped skill, so the collision below would be "
        "between two personal documents and would assert nothing"
    )
    asyncio.run(
        save_local_skill(store, "alice-oid", contested, _BODY.replace("my-workup", contested))
    )
    loaded = _skills_middleware(
        _mounted(store, "alice-oid"), _labelled(_skill_dirs()), AgentProfile(name="default")
    ).before_agent({}, None, None)
    surviving = {skill["name"]: skill["path"] for skill in loaded["skills_metadata"]}

    assert contested in surviving, f"{contested} vanished from the listing entirely"
    assert not surviving[contested].startswith(LOCAL_SKILLS_ROOT), (
        f"a personal skill displaced the reviewed {contested}: the model is served "
        f"{surviving[contested]}"
    )


def test_the_outer_permission_rules_deny_a_write_under_this_root_too() -> None:
    """The outer permission rules also deny a write under this root.

    `ReadOnlyStoreBackend` refuses every write; these rules are the outer layer, evaluated before
    any backend. This root is covered by absence (only `/scratch/**` and `/memories/**` are
    allowed), so a future allow could widen it unnoticed.
    """
    from chemclaw.agent.scratchpad import MEMORY_ROOT, SCRATCH_ROOT, filesystem_permissions

    rules = filesystem_permissions()
    allowed = [path for rule in rules if rule.mode == "allow" for path in rule.paths]

    assert not any(path.startswith(LOCAL_SKILLS_ROOT) for path in allowed), (
        f"a write is allowed under {LOCAL_SKILLS_ROOT}, so the outer half of this tier's "
        f"read-only property is gone; the allows are {allowed}"
    )
    assert sorted(allowed) == sorted([f"{SCRATCH_ROOT}**", f"{MEMORY_ROOT}**"]), (
        "the allow-list changed, so re-derive whether this root is still covered by the deny that "
        "closes the surface behind it"
    )
    assert rules[-1].mode == "deny" and rules[-1].paths == ["/**"], (
        "the blanket deny is no longer last, and these rules are first-match-wins"
    )


def test_a_local_skill_load_is_counted_and_carries_no_persons_words(store: InMemoryStore) -> None:
    """A local skill load is counted on a bare counter that carries no person's words.

    `chemclaw_skill_loads_total` lives on `NarrowedSkillsBackend` and does not see this tier. A
    local skill's name is a person's own words, so a label would mint a series per private name; the
    operator's question is only whether the tier is used.
    """
    from chemclaw.core.metrics import METRICS

    asyncio.run(save_local_skill(store, "alice-oid", "a-private-project", _BODY))
    backend = _mounted(store, "alice-oid")
    path = f"{LOCAL_SKILLS_ROOT}a-private-project/SKILL.md"

    before = METRICS.render()
    assert backend.read(path).error is None
    assert asyncio.run(backend.aread(path)).error is None
    # A failed read delivered no body, so it is not a load.
    assert backend.read(f"{LOCAL_SKILLS_ROOT}not-a-skill/SKILL.md").error is not None
    after = METRICS.render()

    def counted(rendered: str) -> int:
        for line in rendered.splitlines():
            if line.startswith("chemclaw_local_skill_loads_total "):
                return int(float(line.rsplit(maxsplit=1)[1]))
        return 0

    assert counted(after) - counted(before) == 2, (
        "two delivered bodies — one sync, one async — must each count, and the failed read must "
        "not; the async path is the one an agent takes and is native rather than a thread wrapper"
    )
    assert "a-private-project" not in after, (
        "a chemist's own skill name reached the exposition: that is a per-person identifier on a "
        "shared metric, minting a series per private project name"
    )


async def test_a_name_that_could_never_have_been_written_reads_as_absent() -> None:
    r"""A name that could never have been written reads as absent (404), not a 500.

    The route takes the name off the URL, and Postgres rejects a NUL byte in a text field. Both the
    in-memory and Postgres stores are driven, since they must agree.
    """
    from chemclaw.agent.skill_store import storable_name

    assert not storable_name("a\x00b")
    assert not storable_name("two words")
    assert storable_name("cold-quench")

    store = InMemoryStore()
    assert await read_local_skill(store, "alice", "a\x00b") is None
    assert await delete_local_skill(store, "alice", "a\x00b") is False


def test_a_directory_whose_manifest_is_broken_still_occupies_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewed directory whose `SKILL.md` declares nothing still occupies its name.

    `skill_manifest._declared_pair` keys an unreadable manifest by its directory, and
    `shipped_skill_names` includes it, so `POST /skills/mine` and `propose_skill` refuse that name.
    Kept deliberately: `skill-validate` requires directory and `name` to agree, so this is the name
    the skill will hold once fixed, and leaving it free would let a personal skill shadow it.
    """
    from chemclaw.agent.langgraph_agent import shipped_skill_names
    from chemclaw.agent.skill_manifest import _declared_tools

    broken = tmp_path / "half-written-skill"
    broken.mkdir()
    (broken / "SKILL.md").write_text("---\nname: '   '\ndescription: d\n---\n\nbody\n")
    monkeypatch.setattr(settings, "skills_dir", str(tmp_path))
    _declared_tools.cache_clear()
    try:
        occupied = shipped_skill_names()
    finally:
        _declared_tools.cache_clear()

    assert "half-written-skill" in occupied, (
        "a reviewed directory whose manifest cannot be read leaves its name free, so a personal "
        "skill can take it and then be shadowed the moment the frontmatter is fixed"
    )
