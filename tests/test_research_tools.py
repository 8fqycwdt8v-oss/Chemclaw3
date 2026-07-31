"""Tests for the cross-source evidence gatherer (plan Phase 5b, generalized).

Proves gather_evidence unions the knowledge graph with reaction-fingerprint search in one
call, that every chunk is note-cited, and that the graph filters work — using a temp
knowledge dir and an in-memory reaction store (no database, no git).
"""

import asyncio
from pathlib import Path

import pytest

import chemclaw.agent.research_tools as research_tools
from chemclaw.agent.research_tools import gather_evidence
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore

_ESTER = "CCO.CC(=O)O>>CCOC(C)=O"


def _seed_graph(tmp_path: Path) -> None:
    (tmp_path / "opt.md").write_text(
        "---\nid: optimization-ester\ntype: optimization-campaign\n---\n"
        "Yield improved to 92% at higher temperature. [[reaction-rxn-1]]\n",
        encoding="utf-8",
    )
    (tmp_path / "rxn.md").write_text(
        "---\nid: reaction-rxn-1\ntype: reaction\n---\nEthyl acetate, yield 85%.\n",
        encoding="utf-8",
    )


def _seed_store() -> InMemoryFingerprintStore:
    store = InMemoryFingerprintStore()
    asyncio.run(store.add(record_for_reaction("rxn-1", _ESTER)))
    return store


def test_gather_unions_graph_and_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One call returns cited evidence from the graph *and* structurally similar reactions."""
    _seed_graph(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    store = _seed_store()
    monkeypatch.setattr(research_tools, "_reaction_store", lambda: store)

    chunks = asyncio.run(gather_evidence("yield", reaction_smiles=_ESTER))

    assert {c.source_note_id for c in chunks} >= {"optimization-ester", "reaction-rxn-1"}
    assert {c.retriever for c in chunks} == {"graph", "reaction-fingerprint"}
    assert all(c.source_note_id for c in chunks)  # every chunk is citable


def test_type_filter_scopes_the_graph_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note_type filter restricts the graph source; no anchor means no fingerprint hits."""
    _seed_graph(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    store = _seed_store()
    monkeypatch.setattr(research_tools, "_reaction_store", lambda: store)

    chunks = asyncio.run(gather_evidence("yield", note_type="optimization-campaign"))

    assert {c.source_note_id for c in chunks} == {"optimization-ester"}


def _seed_dated_graph(tmp_path: Path) -> None:
    """Two runs a fortnight apart, and one whose date nobody recorded."""
    for note_id, valid_from in (("reaction-old", "2026-03-01"), ("reaction-new", "2026-03-15")):
        (tmp_path / f"{note_id}.md").write_text(
            f"---\nid: {note_id}\ntype: reaction\nvalid_from: {valid_from}\n---\nyield noted.\n",
            encoding="utf-8",
        )
    (tmp_path / "undated.md").write_text(
        "---\nid: reaction-undated\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
    )


def test_date_window_scopes_the_sweep_to_a_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What have I tried in the last fortnight — unanswerable until the dates were filterable."""
    _seed_dated_graph(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    chunks = asyncio.run(gather_evidence("yield", since="2026-03-10"))

    assert {c.source_note_id for c in chunks} == {"reaction-new"}


def test_an_undated_note_is_excluded_from_a_windowed_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It cannot be shown to fall in the window, and the unwindowed sweep still finds it."""
    _seed_dated_graph(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    windowed = asyncio.run(gather_evidence("yield", until="2026-03-31"))
    everything = asyncio.run(gather_evidence("yield"))

    assert "reaction-undated" not in {c.source_note_id for c in windowed}
    assert "reaction-undated" in {c.source_note_id for c in everything}


def test_a_malformed_date_argument_says_what_was_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad tool argument is a prompt-level mistake: the message has to be actionable."""
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    with pytest.raises(ValueError, match="since must be an ISO date"):
        asyncio.run(gather_evidence("yield", since="last Tuesday"))


def test_empty_when_nothing_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A query with no hits returns nothing — silence, never invented evidence."""
    _seed_graph(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    store = _seed_store()
    monkeypatch.setattr(research_tools, "_reaction_store", lambda: store)

    assert asyncio.run(gather_evidence("no-such-term-xyz")) == []


def test_sweep_is_capped_to_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broad match over many notes is truncated to the configured chunk budget (token-frugal)."""
    for i in range(10):
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 3)

    chunks = asyncio.run(gather_evidence("yield"))

    assert len(chunks) == 3


def test_sweep_ranks_by_confidence_before_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated sweep keeps the most-confident notes, not an arbitrary disk slice (KM-5)."""
    # Three notes all match "yield"; only confidence distinguishes them. Filenames sort
    # high<low<mid, so an *unranked* cap-2 would keep {high, low}; ranking must keep {high, mid}.
    (tmp_path / "high.md").write_text(
        "---\nid: reaction-high\ntype: reaction\nconfidence: 0.9\n---\nyield.\n", encoding="utf-8"
    )
    (tmp_path / "low.md").write_text(
        "---\nid: reaction-low\ntype: reaction\nconfidence: 0.1\n---\nyield.\n", encoding="utf-8"
    )
    (tmp_path / "mid.md").write_text(
        "---\nid: reaction-mid\ntype: reaction\nconfidence: 0.5\n---\nyield.\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 2)

    chunks = asyncio.run(gather_evidence("yield"))

    assert {c.source_note_id for c in chunks} == {"reaction-high", "reaction-mid"}
