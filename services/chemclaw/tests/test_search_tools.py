"""Tests for the agent structural-search tools (plan steps 3.3, 3.4).

Proves the fingerprint capabilities are reachable from the agent layer and return note-linked
hits, using in-memory stores (no database). The store factories are swapped via the module
seam, exactly as the eln-sync workflow swaps its stores in tests.
"""

import asyncio

import pytest

import agents.search_tools as search_tools
from agents.search_tools import (
    find_similar_molecules,
    find_similar_reactions,
    find_substructure_matches,
)
from mcp_servers.fpstore import InMemoryFingerprintStore
from mcp_servers.molfp.search import record_for
from mcp_servers.rxnfp.search import record_for_reaction


def _reaction_store() -> InMemoryFingerprintStore:
    store = InMemoryFingerprintStore()
    asyncio.run(store.add(record_for_reaction("rxn-1", "CCO.CC(=O)O>>CCOC(C)=O")))
    asyncio.run(
        store.add(record_for_reaction("rxn-2", "OB(O)c1ccccc1.Brc1ccccc1>>c1ccc(-c2ccccc2)cc1"))
    )
    return store


def _molecule_store() -> InMemoryFingerprintStore:
    store = InMemoryFingerprintStore()
    for smiles in ("CCO", "CCCO", "OB(O)c1ccccc1", "c1ccccc1"):
        asyncio.run(store.add(record_for(smiles, smiles)))
    return store


def test_find_similar_reactions_returns_note_linked_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reaction query returns hits whose ids map to reaction notes, best match first."""
    store = _reaction_store()
    monkeypatch.setattr(search_tools, "_reaction_store", lambda: store)
    hits = asyncio.run(find_similar_reactions("CCO.CC(=O)O>>CCOC(C)=O"))
    assert hits[0].reaction_note_id == "reaction-rxn-1"
    assert hits[0].similarity == 1.0  # identical reaction


def test_find_similar_molecules_ranks_by_similarity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A molecule query returns the closest analog above the identical match."""
    store = _molecule_store()
    monkeypatch.setattr(search_tools, "_molecule_store", lambda: store)
    hits = asyncio.run(find_similar_molecules("CCO"))
    assert hits[0].smiles == "CCO" and hits[0].similarity == 1.0
    assert "CCCO" in {h.smiles for h in hits}  # the propanol analog is retrieved


def test_find_substructure_matches_filters_by_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A substructure query returns only molecules containing the fragment (no score)."""
    store = _molecule_store()
    monkeypatch.setattr(search_tools, "_molecule_store", lambda: store)
    hits = asyncio.run(find_substructure_matches("OB(O)"))  # a boronic acid fragment
    assert {h.smiles for h in hits} == {"OB(O)c1ccccc1"}
    assert all(h.similarity is None for h in hits)
