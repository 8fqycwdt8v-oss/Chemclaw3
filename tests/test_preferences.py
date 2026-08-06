"""Per-user working preferences (gap AGT-4).

Every memory layer was corpus-level — campaign/playbook/optimization/interaction notes all describe
the chemistry, shared by everyone. Nothing remembered *this chemist*: their project, their preferred
solvent system, the units they think in. The identity existed; only the layer did not.

The load-bearing design decision is what this is **not**: it is deliberately not a knowledge-graph
note. Routing "Anna prefers 2-MeTHF" through the PR-gate would ask a human to review noise, eroding
the seriousness of the gate that protects actual shared knowledge (D-005). The graph holds what the
organisation knows; this holds how one person works.
"""

import asyncio

import pytest

from chemclaw.agent.preferences import (
    Preference,
    PreferenceStore,
    recall_preferences,
    remember_preference,
)
from chemclaw.core.config import settings


def test_a_preference_round_trips_per_owner() -> None:
    """The point of the layer: the same key can differ per chemist."""
    store = PreferenceStore()
    asyncio.run(store.remember("anna", "preferred_solvent", "2-MeTHF"))
    asyncio.run(store.remember("ben", "preferred_solvent", "THF"))
    assert [p.value for p in asyncio.run(store.recall("anna"))] == ["2-MeTHF"]
    assert [p.value for p in asyncio.run(store.recall("ben"))] == ["THF"]


def test_setting_the_same_key_replaces_rather_than_accumulates() -> None:
    """A preference is current state, not a log — two answers to "which solvent" is not a state."""
    store = PreferenceStore()
    asyncio.run(store.remember("anna", "project", "PRJ-1"))
    asyncio.run(store.remember("anna", "project", "PRJ-2"))
    assert [(p.key, p.value) for p in asyncio.run(store.recall("anna"))] == [("project", "PRJ-2")]


def test_a_chemist_can_take_a_preference_back() -> None:
    """A preference nobody can retract would be worse than none — it would silently skew advice."""
    store = PreferenceStore()
    asyncio.run(store.remember("anna", "units", "mmol"))
    asyncio.run(store.forget("anna", "units"))
    assert asyncio.run(store.recall("anna")) == []


def test_recall_is_key_sorted_so_the_model_reads_a_stable_list() -> None:
    """Unstable ordering would churn the model's context between otherwise identical turns."""
    store = PreferenceStore()
    for key in ("units", "project", "base"):
        asyncio.run(store.remember("anna", key, "x"))
    assert [p.key for p in asyncio.run(store.recall("anna"))] == ["base", "project", "units"]


def test_an_unknown_chemist_has_no_preferences_rather_than_an_error() -> None:
    """Empty means "nothing recorded"; the tool docstring forbids inventing one from that."""
    assert asyncio.run(PreferenceStore().recall("nobody")) == []


def test_preferences_never_reach_the_pr_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The design decision, pinned: a personal preference must not become a reviewed graph note.

    If this ever routed through `propose_note`, reviewers would be asked to sign off on personal
    trivia — which is exactly how a gate stops being taken seriously.
    """
    import chemclaw.agent.preferences as module

    assert not hasattr(module, "propose_note")
    assert "propose_note" not in module.__doc__ or "not" in module.__doc__.lower()


def test_the_tools_are_scoped_to_the_calling_chemist(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner comes from the ambient identity, never from a model-supplied argument.

    A model-supplied owner would let one chemist read or overwrite another's preferences.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr("chemclaw.agent.preferences.require_actor", lambda: "anna")
    asyncio.run(remember_preference("project", "PRJ-9"))
    monkeypatch.setattr("chemclaw.agent.preferences.require_actor", lambda: "ben")
    assert asyncio.run(recall_preferences()) == []


def _postgres_mode_with_a_dead_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a Postgres deployment whose database refuses every connection."""
    monkeypatch.setattr(settings, "session_store", "postgres")

    def _explode(*_args: object, **_kwargs: object) -> object:
        raise ConnectionError("Postgres unreachable")

    monkeypatch.setattr("chemclaw.core.db.connection", _explode)


def test_remembering_reports_that_it_was_only_for_this_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preference that could not be persisted must not be confirmed as durable.

    The in-memory copy is written first and always succeeds, so the current session behaves
    correctly and the failure is invisible from outside — the tool answered "Remembered ... for
    this chemist" against a docstring promising "future turns and future sessions", while the row
    never reached Postgres.
    """
    _postgres_mode_with_a_dead_database(monkeypatch)
    store = PreferenceStore()
    assert asyncio.run(store.remember("u-1", "project", "PRJ-9")) is False
    # Still remembered *here*: swallowing is right, claiming durability is not.
    assert asyncio.run(store.recall("u-1")) == [Preference(key="project", value="PRJ-9")]


def test_forgetting_reports_that_the_preference_will_come_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worse direction: a deletion that did not persist reappears next session.

    The in-memory copy is dropped, so the preference looks removed for the rest of this session.
    A chemist who asked for something to be forgotten and was told it was must not find it back.
    """
    _postgres_mode_with_a_dead_database(monkeypatch)
    store = PreferenceStore()
    store._memory[("u-1", "project")] = "PRJ-9"
    assert asyncio.run(store.forget("u-1", "project")) is False


def test_an_unreadable_store_is_not_reported_as_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty fallback after a failed read must raise, not answer "no preferences".

    `recall_preferences` documents an empty list as "nothing has been recorded yet", so returning
    one after a failed read is an affirmatively wrong answer — and the chemist then restates
    preferences that also will not persist. A failed answer is better than a wrong one.
    """
    _postgres_mode_with_a_dead_database(monkeypatch)
    with pytest.raises(ConnectionError):
        asyncio.run(PreferenceStore().recall("u-nobody"))


def test_a_populated_memory_fallback_is_still_used_after_a_failed_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of that line: memory *with* content is this process's own valid view.

    Raising there would discard a correct answer, so the refusal above is specifically about an
    empty result being indistinguishable from "none recorded" — not about read failures generally.
    """
    _postgres_mode_with_a_dead_database(monkeypatch)
    store = PreferenceStore()
    store._memory[("u-2", "units")] = "kJ/mol"
    assert asyncio.run(store.recall("u-2")) == [Preference(key="units", value="kJ/mol")]
