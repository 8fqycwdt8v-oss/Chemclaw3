"""Tests for the cross-source evidence gatherer (plan Phase 5b, generalized).

Proves gather_evidence unions the knowledge graph with reaction-fingerprint search in one
call, that every chunk is note-cited, and that the graph filters work — using a temp
knowledge dir and an in-memory reaction store (no database, no git).
"""

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import chemclaw.agent.research_tools as research_tools
from chemclaw.agent.research_tools import gather_evidence
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
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

    chunks = asyncio.run(gather_evidence("yield", reaction_smiles=_ESTER)).chunks

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

    chunks = asyncio.run(gather_evidence("yield", note_type="optimization-campaign")).chunks

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

    chunks = asyncio.run(gather_evidence("yield", since="2026-03-10")).chunks

    assert {c.source_note_id for c in chunks} == {"reaction-new"}


def test_an_undated_note_is_excluded_from_a_windowed_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It cannot be shown to fall in the window, and the unwindowed sweep still finds it."""
    _seed_dated_graph(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    windowed = asyncio.run(gather_evidence("yield", until="2026-03-31")).chunks
    everything = asyncio.run(gather_evidence("yield")).chunks

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

    assert asyncio.run(gather_evidence("no-such-term-xyz")).chunks == []


def test_sweep_is_capped_to_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broad match over many notes is truncated to the configured chunk budget (token-frugal)."""
    for i in range(10):
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 3)

    chunks = asyncio.run(gather_evidence("yield")).chunks

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

    chunks = asyncio.run(gather_evidence("yield")).chunks

    assert {c.source_note_id for c in chunks} == {"reaction-high", "reaction-mid"}


def test_a_mounted_share_is_not_starved_by_a_larger_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sixth source must survive the cap, and the only honest check is to count its chunks.

    `D-2026-08-01-a-cap-that-starves-a-source` is the reason: a flat union truncated in config
    order gave the lexical leg **zero** chunks in every default-mode answer, silently, on the
    success path. A file share is exactly the shape that regresses it — a graph of thirty notes
    matching a common word will out-produce it every time — so this counts per source rather than
    asserting the round-robin is still there.
    """
    from chemclaw.ingest.documents.binding import load_binding
    from chemclaw.ingest.documents.index import InMemoryDocumentIndex
    from chemclaw.ingest.documents.retriever import ShareDocumentRetriever
    from chemclaw.ingest.documents.sync import sync_share

    for i in range(30):
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
        )
    mount = tmp_path / "share" / "Docs"
    mount.mkdir(parents=True)
    for i in range(5):
        (mount / f"doc{i}.md").write_text(f"Report {i}: the yield was measured.", encoding="utf-8")

    binding = {
        "mount": str(tmp_path / "share"),
        "roots": [{"path": "Docs"}],
        "extensions": [".md"],
        # This test is about rank fusion, not the gate — so the share says "anyone" out loud rather
        # than leaving it unsaid, which a binding may no longer do.
        "public": True,
    }
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share("sharedrive", load_binding(binding), index))
    share = ShareDocumentRetriever(binding=binding, name="sharedrive", index=index)

    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 10)
    graph = research_tools._text_retrievers()
    monkeypatch.setattr(research_tools, "_text_retrievers", lambda: [*graph, share])

    chunks = asyncio.run(gather_evidence("yield")).chunks

    # Measured, not asserted in the abstract: round-robin gives each source half the budget, so a
    # graph six times the size of the share does not consume it. A flat cap in config order would
    # read `{"graph": 10}` here.
    assert Counter(chunk.retriever for chunk in chunks) == {"graph": 5, "sharedrive": 5}


def test_the_sweep_is_bounded_by_characters_and_not_only_by_a_chunk_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A count of chunks cannot bound what a sweep costs, because a chunk's cost is its content.

    `gather_evidence_max_chunks` counts chunks whose sizes differ by ~7.5x across sources — a
    note-backed chunk is excerpted to `note_excerpt_chars` (240) and a mounted share's is up to
    its binding's `chunk_chars` (1,800). Same finding as `agent_keep_last_conversation_groups`,
    where counting groups left a 300k-token thread at 180k against a 100k budget.
    """
    for i in range(20):
        body = f"yield noted. {'padding ' * 60}"
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\n{body}\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    # The graph leg is bounded by retrieval_top_k like every sibling; raised so the sweep-level
    # budgets under test are the binding ones.
    monkeypatch.setattr(settings, "retrieval_top_k", 40)
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 20)
    monkeypatch.setattr(settings, "gather_evidence_max_chars", 1_000)

    sweep = asyncio.run(gather_evidence("yield"))

    assert sweep.truncated_by == "chars", "the character budget must be able to bind first"
    assert len(sweep.chunks) < 20, "and it must actually cut the list"
    assert sweep.total_before_cap == 20, "while still saying how much there was"


def test_hitting_the_chunk_count_says_so_rather_than_looking_like_a_small_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cut that does not announce itself reads as an honest absence.

    The rule `FingerprintSearch.hits_truncated` and `EvidenceChunk.conflicts_total` already follow.
    """
    for i in range(20):
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    # Raised past the fixture size so the sweep-level count cap is the truncation under test,
    # not the graph leg's own retrieval_top_k bound.
    monkeypatch.setattr(settings, "retrieval_top_k", 40)
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 5)

    sweep = asyncio.run(gather_evidence("yield"))

    assert (len(sweep.chunks), sweep.truncated_by, sweep.total_before_cap) == (5, "count", 20)


def test_one_oversized_chunk_is_returned_rather_than_reported_as_nothing_on_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty list means "nothing on file" here, so the budget may not produce one.

    The same clamp `KeepLastConversationGroupsEdit` makes, for the same reason.
    """
    (tmp_path / "n.md").write_text(
        f"---\nid: reaction-1\ntype: reaction\n---\nyield {'padding ' * 200}\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gather_evidence_max_chars", 10)

    sweep = asyncio.run(gather_evidence("yield"))

    assert len(sweep.chunks) == 1, "an over-budget first chunk still comes back"
    assert sweep.truncated_by is None, "and nothing was actually dropped, so nothing is claimed"


def test_the_character_budget_does_not_starve_a_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`D-2026-08-01-a-cap-that-starves-a-source` in the new currency.

    That ADR is about the *shape* of a cut, not its size: a flat cut in config order gave one leg
    **zero** chunks on every default-mode answer. A second cap applied the old way would
    reintroduce exactly that, so this counts per source under a character budget the way the
    sibling test counts under a chunk count.
    """
    from chemclaw.ingest.documents.binding import load_binding
    from chemclaw.ingest.documents.index import InMemoryDocumentIndex
    from chemclaw.ingest.documents.retriever import ShareDocumentRetriever
    from chemclaw.ingest.documents.sync import sync_share

    for i in range(30):
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
        )
    mount = tmp_path / "share" / "Docs"
    mount.mkdir(parents=True)
    for i in range(5):
        (mount / f"doc{i}.md").write_text(f"Report {i}: the yield was measured.", encoding="utf-8")
    binding = {
        "mount": str(tmp_path / "share"),
        "roots": [{"path": "Docs"}],
        "extensions": [".md"],
        "public": True,
    }
    index = InMemoryDocumentIndex()
    asyncio.run(sync_share("sharedrive", load_binding(binding), index))
    share = ShareDocumentRetriever(binding=binding, name="sharedrive", index=index)

    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 100)
    monkeypatch.setattr(settings, "gather_evidence_max_chars", 1_200)
    graph = research_tools._text_retrievers()
    monkeypatch.setattr(research_tools, "_text_retrievers", lambda: [*graph, share])

    sweep = asyncio.run(gather_evidence("yield"))

    assert sweep.truncated_by == "chars", "the fixture must actually exercise the character cap"
    surviving = Counter(chunk.retriever for chunk in sweep.chunks)
    # **Both directions, and measured against the mutants rather than assumed.** Spending the
    # budget in config order — the original D-2026-08-01 shape — gives `{"graph": 12}` here, the
    # share starved to zero, and this fails. Asserting only that the share survives would have
    # missed the mirror image: the share's RRF-derived 1.0 outranks a note's 0.5 confidence, so a
    # score-re-sorted cut starves the *graph* instead.
    #
    # What this pins is the **currency** change specifically. A score-re-sorted cut still passes
    # against this fixture, because at 1,200 characters both legs happen to survive it; that shape
    # is guarded by `test_a_mounted_share_is_not_starved_by_a_larger_graph` above and by the
    # cross-source sort being gone from `_interleave_dedup` — said out loud rather than left for
    # someone to discover this test was weaker than it reads.
    assert surviving["sharedrive"] > 0 and surviving["graph"] > 0, (
        f"a source was starved by the character budget: {dict(surviving)} — "
        "which is D-2026-08-01 reintroduced in a new currency"
    )


def test_the_budget_charges_the_whole_chunk_and_not_only_its_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`content` is only part of what reaches the model, and charging it alone under-counts badly.

    Measured on one realistic chunk carrying conflicts and provenance: 300 characters of content
    against 569 serialized — a 47% under-count, so a 60,000-character budget really spent about
    114,000. The assertion is on the *cut moving* when only non-content fields grow, because that
    is the property; a fixed expected length would pin the serializer instead.
    """
    for i in range(12):
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 12)
    monkeypatch.setattr(settings, "gather_evidence_max_chars", 1_500)

    lean = asyncio.run(gather_evidence("yield"))

    # Same corpus, same content, but every chunk now carries a long provenance label. Nothing about
    # `content` changed, so a content-only budget would keep exactly as many chunks as before.
    real_chunks = research_tools._interleave_dedup

    def _padded(ranked_lists: Any) -> Any:
        return [
            chunk.model_copy(update={"source": "warehouse:" + "x" * 400})
            for chunk in real_chunks(ranked_lists)
        ]

    monkeypatch.setattr(research_tools, "_interleave_dedup", _padded)
    padded = asyncio.run(gather_evidence("yield"))

    assert lean.chunks, "sanity: the corpus answers at all"
    assert len(padded.chunks) < len(lean.chunks), (
        "growing a non-content field did not cost the budget anything, so the budget is measuring "
        f"content alone: {len(lean.chunks)} chunks before, {len(padded.chunks)} after"
    )


def test_the_sweep_reports_what_each_source_contributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sources` carries the per-branch counts the fan-out used to drop at this boundary.

    Without them the model could not tell "the share found nothing", "the share isn't
    configured" and "the share declined" apart — three different answers rendered identically.
    """
    (tmp_path / "reaction").mkdir()
    (tmp_path / "reaction" / "reaction-a.md").write_text(
        "---\nid: reaction-a\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    sweep = asyncio.run(gather_evidence("yield"))

    assert sweep.sources.get("graph") == 1
    assert sweep.sources_skipped == {}


def test_gather_evidence_records_the_kept_half_of_the_source_metric_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`chemclaw_evidence_source_kept_total` must come from the real sweep, not only a direct call.

    `retrieval.fanout.record_kept_chunks` is what makes the D-2026-08-01-a-cap-that-starves-a-
    source shape alertable — a leg that hands over chunks and survives the merge with none — but
    the metric had a function and no producer: nothing on the one production path it exists to
    watch (`gather_evidence`) ever called it, so `tests/test_datapath_observability.py` exercising
    the function directly was the only thing keeping the series alive.
    """
    (tmp_path / "reaction").mkdir()
    (tmp_path / "reaction" / "reaction-a.md").write_text(
        "---\nid: reaction-a\ntype: reaction\n---\nyield noted.\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    before = METRICS.value("chemclaw_evidence_source_kept_total")
    sweep = asyncio.run(gather_evidence("yield"))
    after = METRICS.value("chemclaw_evidence_source_kept_total")

    assert sweep.chunks, "sanity: the sweep actually found something to keep"
    assert after > before, (
        "gather_evidence did not move chemclaw_evidence_source_kept_total — the metric has no "
        "producer on its one production path"
    )
