"""A chemist's own skills: read by their turns, written by no turn, and visible to them.

`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` §3 grants this tier its exemption from
review on conditions it states as requirements rather than preferences, and these are those
conditions driven rather than restated.
"""

import asyncio
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.langgraph_agent import skills_backend
from chemclaw.agent.local_skills import (
    LOCAL_SKILLS_LABEL,
    LOCAL_SKILLS_ROOT,
    MAX_LOCAL_SKILL_CHARS,
    ReadOnlyStoreBackend,
    delete_local_skill,
    list_local_skills,
    local_skills_namespace,
    local_skills_prefix,
    read_local_skill,
    save_local_skill,
)
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.scratchpad import memory_namespace, scratchpad_backend
from chemclaw.agent.skill_backend import SkillsReadOnlyRefusal
from chemclaw.core.identity_context import reset_current_identity, set_current_identity

_BODY = "---\nname: my-workup\ndescription: how I work up a Suzuki\n---\n\nQuench cold.\n"


@pytest.fixture
def store() -> InMemoryStore:
    """A store standing in for the deployment's `AsyncPostgresStore`, same `BaseStore` contract."""
    return InMemoryStore()


def _mounted(store: Any, actor: str) -> Any:
    """The backend a turn for `actor` would be given, with the tier mounted as a turn mounts it."""
    tokens = set_current_identity(actor, frozenset())
    try:
        return scratchpad_backend(skills_backend(AgentProfile(name="default"), []), store)
    finally:
        reset_current_identity(tokens)


def test_a_chemists_own_skill_reaches_their_turn(store: InMemoryStore) -> None:
    """The tier's whole purpose, driven through the mount rather than the store.

    Through `scratchpad_backend` because that is where the two conditions are decided — a store and
    a turn with an actor — and a test that read the store directly would prove the rows exist
    without proving a turn can reach them.
    """
    asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY))

    backend = _mounted(store, "alice-oid")

    assert LOCAL_SKILLS_ROOT in backend.routes
    result = backend.read(f"{LOCAL_SKILLS_ROOT}my-workup/SKILL.md")
    assert result.error is None
    assert "Quench cold." in str(result.file_data)


def test_one_chemists_skill_never_reaches_anothers_turn(store: InMemoryStore) -> None:
    """Never a source of shared truth — and structural, not a predicate that could be unconfigured.

    The namespace closes over the turn's own actor, so bob's turn is not *refused* alice's skill;
    it is mounted on a namespace that does not contain it. That is a stronger property than a gate,
    and it is why this tier applies none of the shared tree's four narrowings.
    """
    asyncio.run(save_local_skill(store, "alice-oid", "my-workup", _BODY))

    assert asyncio.run(list_local_skills(store, "alice-oid")) == ["my-workup"]
    assert asyncio.run(list_local_skills(store, "bob-oid")) == []
    bobs = _mounted(store, "bob-oid").read(f"{LOCAL_SKILLS_ROOT}my-workup/SKILL.md")
    assert bobs.error is not None


def test_no_turn_may_write_its_owners_skills(store: InMemoryStore) -> None:
    """`SkillsReadOnlyRefusal` is unchanged, stated against the other base class.

    The shared tree gets this from `NarrowedSkillsBackend`, a `FilesystemBackend`; this tier is
    stored, so the same refusal has to hold against `StoreBackend` or the invariant would be true
    of one tier and false of the other — which is worse than false of both, because the prose says
    "no agent path writes a skill" without qualification.
    """
    backend = ReadOnlyStoreBackend(
        namespace=lambda _runtime: local_skills_namespace("alice-oid"), store=store
    )

    for verb, args in (
        ("write", ("/x/SKILL.md", "body")),
        ("edit", ("/x/SKILL.md", "a", "b")),
        ("delete", ("/x/SKILL.md",)),
        ("upload_files", (["/x/SKILL.md"],)),
    ):
        with pytest.raises(SkillsReadOnlyRefusal):
            getattr(backend, verb)(*args)


def test_every_method_this_tier_exposes_is_either_a_read_or_a_refusal(store: InMemoryStore) -> None:
    """Derived from the surface, not from a written list, because the write half grows.

    deepagents 0.7 added `delete` to the protocol and the shared tree inherited a working one until
    a *derived* test caught it — a hand-written list is a list of what upstream declared the week it
    was written. So the probes and the refusals must together cover every public method, and an
    upstream addition has to be triaged into one or the other before this file passes.

    `StoreBackend` as well as `BackendProtocol`: what a turn can reach is what
    `ReadOnlyStoreBackend` *inherits*, and a method upstream adds to the concrete class alone would
    be invisible to a protocol-only derivation.
    """
    from deepagents.backends import StoreBackend
    from deepagents.backends.protocol import BackendProtocol

    backend = ReadOnlyStoreBackend(
        namespace=lambda _runtime: local_skills_namespace("alice-oid"), store=store
    )
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

    # Every write verb refuses, async twins included — they dispatch through the sync overrides,
    # which is the behaviour that makes four overrides cover eight names.
    for name, call in writes.items():
        with pytest.raises(SkillsReadOnlyRefusal):
            result = call()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        assert name in surface, f"{name} is not on the surface any more"


def test_the_tier_is_advertised_only_when_it_is_mounted(store: InMemoryStore) -> None:
    """A source naming a path with no route would publish an empty tier on every turn.

    The middleware derives its sources from the backend's own routes for exactly this reason: the
    tier is mounted on two conditions `_skills_middleware` cannot see, and an advertised `/mine`
    with no route resolves to the composite's default `StateBackend` — an empty directory the model
    is told about every turn.
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
    """The sweep's prefix list holds both tiers, built by the functions each writer writes under.

    `store` has no actor column — the reason
    `D-2026-08-10-basestore-is-not-where-this-systems-memory-lives` gives for rejecting `BaseStore`
    — so a completeness check derived from column names passes while a departing person's rows
    remain. The namespace is the answer to that, and this is where the answer is spent: a sweep
    that built only the memory prefixes would leave behind the one kind of row this system lets a
    person author about themselves.

    Asserted through `store_prefixes`, the function `erase_actor` actually calls, so this cannot
    pass by the symbol merely being imported.
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

    shipped = [path.stat().st_size for path in Path("skills").glob("*/SKILL.md")]

    assert shipped, "no shipped skills were found, so this asserts nothing"
    assert MAX_LOCAL_SKILL_CHARS > max(shipped)


def test_the_label_carries_no_identity() -> None:
    """The mount path appears in the system prompt of every turn and in every log line quoting one.

    The namespace carries the actor digest because it must; the *path* must not, since it is per
    actor by construction and a digest there would be a stable identifier for a person in the
    prompt.
    """
    assert LOCAL_SKILLS_ROOT == f"/{LOCAL_SKILLS_LABEL}/"
    assert local_skills_namespace("alice-oid")[1] not in LOCAL_SKILLS_ROOT


def test_a_write_outside_the_tool_chain_is_not_a_write_without_a_record(
    store: InMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The concern `test_no_first_party_module_writes_to_a_store_directly` is actually about.

    That guard's stated mechanism — the audit row, the authz gate, the dry-run refusal and the
    repeat guard a `write_file` tool call crosses — has no subject here: this write comes from an
    HTTP route a person calls, and three of the four controls need a turn to mean anything. But its
    stated *reason* does apply, and in its own words: a direct write "would do so silently: nothing
    fails, the memory is simply written with no record that it was."

    So both halves of this tier's lifecycle leave one, and this is what makes "not silent" a
    property rather than a sentence.
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
