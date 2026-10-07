"""Hybrid retrieval: the vector and lexical retrievers, RRF fusion, and `gather_evidence`'s modes.

Offline with an in-memory index and fake sources: the retrievers cite real notes and honour
filters, RRF rewards notes ranked by more than one source, and `gather_evidence` fuses in `hybrid`
mode and round-robins in `graph` mode (the default).
"""

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import chemclaw.agent.research_tools as research_tools
from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion
from chemclaw.retrieval.retrievers import LexicalRetriever, VectorRetriever
from chemclaw.retrieval.vector_index import InMemoryNoteIndex, reindex_notes


def _write_note(directory: Path, note_id: str, body: str, note_type: str = "reaction") -> None:
    (directory / f"{note_id}.md").write_text(
        f"---\nid: {note_id}\ntype: {note_type}\n---\n{body}\n", encoding="utf-8"
    )


async def _index_for(directory: Path) -> InMemoryNoteIndex:
    index = InMemoryNoteIndex()
    await reindex_notes(index, notes_dir=str(directory))
    return index


async def test_vector_retriever_cites_the_semantic_note(tmp_path: Path) -> None:
    """VectorRetriever returns a cited chunk for the note whose body matches the query's meaning."""
    _write_note(tmp_path, "note-001", "amide coupling with HATU gave epimerization")
    _write_note(tmp_path, "note-002", "distillation column reflux ratio study")
    index = await _index_for(tmp_path)
    retriever = VectorRetriever(index, notes_dir=str(tmp_path))
    chunks = await retriever.retrieve("epimerization during amide coupling", {})
    assert chunks and chunks[0].source_note_id == "note-001"
    assert chunks[0].retriever == "vector"


async def test_lexical_retriever_honors_type_filter(tmp_path: Path) -> None:
    """A type filter excludes a matching note of the wrong type (same contract as the graph one)."""
    _write_note(tmp_path, "rxn-1", "amide coupling", note_type="reaction")
    _write_note(tmp_path, "play-1", "amide coupling", note_type="playbook")
    index = await _index_for(tmp_path)
    retriever = LexicalRetriever(index, notes_dir=str(tmp_path))
    chunks = await retriever.retrieve("amide coupling", {"type": "playbook"})
    assert [c.source_note_id for c in chunks] == ["play-1"]


async def test_vector_and_lexical_retrievers_exclude_expired_notes(tmp_path: Path) -> None:
    """An expired note in the index is never served as current evidence (KM-7, all entry points)."""
    (tmp_path / "old.md").write_text(
        "---\nid: note-old\ntype: reaction\nvalid_to: 2000-01-01\n---\n"
        "amide coupling epimerization\n",
        encoding="utf-8",
    )
    _write_note(tmp_path, "note-new", "amide coupling epimerization")
    index = await _index_for(tmp_path)
    for retriever in (
        VectorRetriever(index, notes_dir=str(tmp_path)),
        LexicalRetriever(index, notes_dir=str(tmp_path)),
    ):
        chunks = await retriever.retrieve("amide coupling epimerization", {})
        assert [c.source_note_id for c in chunks] == ["note-new"]


async def test_index_hit_scores_survive_into_chunks(tmp_path: Path) -> None:
    """Vector chunks carry the index's own ranking score, not the neutral 0.5 default."""
    _write_note(tmp_path, "note-001", "amide coupling with HATU gave epimerization")
    _write_note(tmp_path, "note-002", "amide coupling")
    index = await _index_for(tmp_path)
    retriever = VectorRetriever(index, notes_dir=str(tmp_path))
    query = "epimerization during amide coupling"
    chunks = await retriever.retrieve(query, {})
    (query_embedding,) = embed_texts([query])
    hits = await index.search_dense(query_embedding, settings.retrieval_top_k)
    expected = {h.note_id: min(max(h.score, 0.0), 1.0) for h in hits}
    assert chunks and all(c.score == expected[c.source_note_id] for c in chunks)
    assert any(c.score != 0.5 for c in chunks)  # the index signal, not the default


async def test_type_filter_keeps_recall_past_global_top_k(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filtered query finds the eligible note even when the global top-k are all wrong-type."""
    _write_note(tmp_path, "rxn-1", "amide coupling epimerization", note_type="reaction")
    _write_note(tmp_path, "rxn-2", "amide coupling epimerization study", note_type="reaction")
    _write_note(tmp_path, "play-1", "amide coupling workup", note_type="playbook")
    index = await _index_for(tmp_path)
    monkeypatch.setattr(settings, "retrieval_top_k", 1)
    for retriever in (
        VectorRetriever(index, notes_dir=str(tmp_path)),
        LexicalRetriever(index, notes_dir=str(tmp_path)),
    ):
        chunks = await retriever.retrieve("amide coupling epimerization", {"type": "playbook"})
        assert [c.source_note_id for c in chunks] == ["play-1"]


async def test_retriever_drops_a_stale_index_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit whose note is not on disk (a stale derived row) is dropped, never cited.

    Pins `graph_cache_ttl_seconds = 0` so the disk scan runs deterministically; the TTL window is
    seconds against an index whose staleness is minutes to hours.
    """
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)

    _write_note(tmp_path, "note-001", "amide coupling epimerization")
    # A second, unrelated note stays on disk: an *entirely* empty tree is a declared skip
    # (`RetrieverSkip`), and this test is about a stale row over a live corpus.
    _write_note(tmp_path, "note-002", "unrelated workup detail")
    index = await _index_for(tmp_path)
    # Delete the note from disk after indexing → the index row is now stale.
    (tmp_path / "note-001.md").unlink()
    retriever = VectorRetriever(index, notes_dir=str(tmp_path))
    hits = await retriever.retrieve("amide coupling epimerization", {})
    assert "note-001" not in {chunk.source_note_id for chunk in hits}


def _chunk(note_id: str) -> EvidenceChunk:
    return EvidenceChunk(content=note_id, source_note_id=note_id, retriever="src")


def test_rrf_rewards_notes_ranked_by_multiple_sources() -> None:
    """A note appearing in two sources outranks notes appearing in only one."""
    a, b, c = _chunk("a"), _chunk("b"), _chunk("c")
    fused = reciprocal_rank_fusion([[a, b], [b, c]], k=60)
    assert [x.source_note_id for x in fused] == ["b", "a", "c"]


def test_rrf_keeps_one_chunk_per_note() -> None:
    """The same note from two sources collapses to a single representative chunk."""
    a = _chunk("a")
    fused = reciprocal_rank_fusion([[a], [a]], k=60)
    assert [x.source_note_id for x in fused] == ["a"]


class _FakeSource:
    """A retriever returning a fixed ranked list, to drive gather_evidence deterministically."""

    def __init__(self, name: str, chunks: list[EvidenceChunk]) -> None:
        self.name = name
        self._chunks = chunks

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        return self._chunks


def _wire_two_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b, c = _chunk("a"), _chunk("b"), _chunk("c")
    monkeypatch.setattr(
        research_tools,
        "_text_retrievers",
        lambda: [_FakeSource("s1", [a, b]), _FakeSource("s2", [b, c])],
    )


def test_gather_evidence_hybrid_mode_fuses_rankings(monkeypatch: pytest.MonkeyPatch) -> None:
    """In hybrid mode gather_evidence returns the RRF order (the shared note first)."""
    _wire_two_sources(monkeypatch)
    monkeypatch.setattr(settings, "retrieval_mode", "hybrid")
    out = asyncio.run(research_tools.gather_evidence("q")).chunks
    assert [c.source_note_id for c in out] == ["b", "a", "c"]


def test_gather_evidence_graph_mode_round_robins_the_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In graph mode `gather_evidence` merges the sources by rank, de-duplicating.

    Each source's best hit comes before any source's second.
    """
    _wire_two_sources(monkeypatch)
    monkeypatch.setattr(settings, "retrieval_mode", "graph")
    out = asyncio.run(research_tools.gather_evidence("q")).chunks
    assert [c.source_note_id for c in out] == ["a", "b", "c"]


# --- the cap is fair across sources ------------------------------------------------------------


def _ranked(prefix: str, retriever: str, scores: list[float]) -> list[EvidenceChunk]:
    """One source's ranked hit-list, best first, with its own score scale."""
    return [
        EvidenceChunk(
            content=f"{prefix}{i}", source_note_id=f"{prefix}{i}", retriever=retriever, score=score
        )
        for i, score in enumerate(scores)
    ]


def test_truncation_is_fair_across_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """No enabled source is starved by the cap, whatever scale its scores use.

    `EvidenceChunk.score` is comparable only within a source (confidence, `ts_rank`, cosine,
    Tanimoto). With 45 graph, 8 lexical and 7 dense hits against a 40-chunk cap, concatenation or a
    score sort starves the later legs; round-robin gives every source its best hit first.
    """
    sources = [
        _FakeSource("graph", _ranked("g", "graph", [0.8] * 45)),
        _FakeSource("lexical", _ranked("l", "lexical", [0.09 - 0.01 * i for i in range(8)])),
        _FakeSource("vector", _ranked("v", "vector", [0.85 - 0.04 * i for i in range(7)])),
    ]
    monkeypatch.setattr(research_tools, "_text_retrievers", lambda: sources)
    monkeypatch.setattr(settings, "retrieval_mode", "graph")
    monkeypatch.setattr(settings, "gather_evidence_max_chunks", 40)

    out = asyncio.run(research_tools.gather_evidence("q")).chunks

    counts = Counter(chunk.retriever for chunk in out)
    assert len(out) == settings.gather_evidence_max_chunks
    assert counts == {"graph": 25, "lexical": 8, "vector": 7}


def test_a_single_source_keeps_its_own_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merge never re-ranks a single source, because the source already ranked itself.

    On a widened search `GraphRetriever` orders by term coverage first; re-sorting by score would
    put a confident near-miss on top. The default deployment runs one text source.
    """
    ranked = _ranked("n", "graph", [0.2, 0.9, 0.5])  # the retriever's order, not score order
    monkeypatch.setattr(research_tools, "_text_retrievers", lambda: [_FakeSource("graph", ranked)])
    monkeypatch.setattr(settings, "retrieval_mode", "graph")

    out = asyncio.run(research_tools.gather_evidence("q")).chunks

    assert [chunk.source_note_id for chunk in out] == ["n0", "n1", "n2"]


# --- the fused order and the number beside it -------------------------------------------------


class _FixedRetriever:
    """A source that returns a prepared ranked list, scored on its own scale."""

    def __init__(self, name: str, scored: list[tuple[str, float]]) -> None:
        """Answer with `scored` — `(note id, this source's own score)`, best first."""
        self.name = name
        self._scored = scored

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return the prepared list, ignoring the query."""
        return [
            EvidenceChunk(content=f"{note} body", source_note_id=note, retriever=self.name, score=s)
            for note, s in self._scored
        ]


def _three_incomparable_legs() -> list[tuple[str, Any]]:
    """Three sources whose scores live on three scales, as the real ones do.

    A note's `confidence`, a `ts_rank` and a cosine are not comparable and never were; that is why
    the fused list needs a number of its own rather than whichever finder got there first.
    """
    return [
        ("graph", _FixedRetriever("graph", [("a", 0.30), ("b", 0.40)])),
        ("lexical", _FixedRetriever("lexical", [("c", 0.02), ("a", 0.09)])),
        ("vector", _FixedRetriever("vector", [("d", 0.60), ("c", 0.95)])),
    ]


def test_hybrid_mode_reports_the_fused_rank_not_the_finders_own_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hybrid mode reports the fused rank, not the finder's own score.

    After fusion the order is a summed reciprocal rank, and each finder's score is on its own scale,
    so the reported number would contradict the delivered order.
    """
    monkeypatch.setattr(settings, "retrieval_mode", "hybrid", raising=False)
    monkeypatch.setattr(research_tools, "_sources", lambda _anchor: _three_incomparable_legs())

    sweep = asyncio.run(research_tools.gather_evidence("anything"))
    scores = [chunk.score for chunk in sweep.chunks]

    assert scores == sorted(scores, reverse=True), (
        f"the score column is not monotone with the order it sits beside: {scores}"
    )
    assert scores[0] == 1.0
    assert all(0.0 < score <= 1.0 for score in scores)


def test_graph_mode_reports_the_merged_position_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Graph mode reports the merged position too.

    The model sees one interleaved column, where confidences, `ts_rank`s and cosines are not
    comparable. The score already did its work inside each retriever, and the note's confidence has
    its own field, `EvidenceChunk.confidence`.
    """
    monkeypatch.setattr(settings, "retrieval_mode", "graph", raising=False)
    monkeypatch.setattr(research_tools, "_sources", lambda _anchor: _three_incomparable_legs())

    sweep = asyncio.run(research_tools.gather_evidence("anything"))
    scores = [chunk.score for chunk in sweep.chunks]

    assert scores == sorted(scores, reverse=True), (
        f"the score column is not monotone with the order it sits beside: {scores}"
    )
    assert scores[0] == 1.0
    assert not {chunk.score for chunk in sweep.chunks} & {0.30, 0.40, 0.02, 0.09, 0.60, 0.95}, (
        "a finder's own scale reached the model beside an order it does not explain"
    )
    # Confidence has a field of its own, which makes restating `score` lossless. Asserted against
    # the model, since these fixture chunks set no confidence.
    assert "confidence" in EvidenceChunk.model_fields
