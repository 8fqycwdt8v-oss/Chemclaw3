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

from agents.preferences import PreferenceStore, recall_preferences, remember_preference
from chemclaw.config import settings


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
    import agents.preferences as module

    assert not hasattr(module, "propose_note")
    assert "propose_note" not in module.__doc__ or "not" in module.__doc__.lower()


def test_the_tools_are_scoped_to_the_calling_chemist(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner comes from the ambient identity, never from a model-supplied argument.

    A model-supplied owner would let one chemist read or overwrite another's preferences.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr("agents.preferences.require_actor", lambda: "anna")
    asyncio.run(remember_preference("project", "PRJ-9"))
    monkeypatch.setattr("agents.preferences.require_actor", lambda: "ben")
    assert asyncio.run(recall_preferences()) == []
