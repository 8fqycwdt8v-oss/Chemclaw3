"""Hybrid retrieval: the vector/lexical retrievers, RRF fusion, and gather_evidence's mode switch.

Offline with an in-memory index and fake sources — proves the new retrievers cite real notes and
honor filters, that Reciprocal Rank Fusion rewards notes ranked by more than one source, and that
`gather_evidence` fuses in `hybrid` mode while keeping the flat union in `graph` mode (the default).
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

import agents.research_tools as research_tools
from chemclaw.config import settings
from chemclaw.embeddings import embed_texts
from report.evidence import EvidenceChunk
from report.hybrid import reciprocal_rank_fusion
from report.retrievers import LexicalRetriever, VectorRetriever
from report.vector_index import InMemoryNoteIndex, reindex_notes


def _write_note(directory: Path, note_id: str, body: str, note_type: str = "reaction") -> None:
    (directory / f"{note_id}.md").write_text(
        f"---\nid: {note_id}\ntype: {note_type}\n---\n{body}\n", encoding="utf-8"
    )


async def _index_for(directory: Path) -> InMemoryNoteIndex:
    index = InMemoryNoteIndex()
    await reindex_notes(index, notes_dir=str(directory))
    return index


def test_vector_retriever_cites_the_semantic_note(tmp_path: Path) -> None:
    """VectorRetriever returns a cited chunk for the note whose body matches the query's meaning."""

    async def _run() -> None:
        _write_note(tmp_path, "note-001", "amide coupling with HATU gave epimerization")
        _write_note(tmp_path, "note-002", "distillation column reflux ratio study")
        index = await _index_for(tmp_path)
        retriever = VectorRetriever(index, notes_dir=str(tmp_path))
        chunks = await retriever.retrieve("epimerization during amide coupling", {})
        assert chunks and chunks[0].source_note_id == "note-001"
        assert chunks[0].retriever == "vector"

    asyncio.run(_run())


def test_lexical_retriever_honors_type_filter(tmp_path: Path) -> None:
    """A type filter excludes a matching note of the wrong type (same contract as the graph one)."""

    async def _run() -> None:
        _write_note(tmp_path, "rxn-1", "amide coupling", note_type="reaction")
        _write_note(tmp_path, "play-1", "amide coupling", note_type="playbook")
        index = await _index_for(tmp_path)
        retriever = LexicalRetriever(index, notes_dir=str(tmp_path))
        chunks = await retriever.retrieve("amide coupling", {"type": "playbook"})
        assert [c.source_note_id for c in chunks] == ["play-1"]

    asyncio.run(_run())


def test_vector_and_lexical_retrievers_exclude_expired_notes(tmp_path: Path) -> None:
    """An expired note in the index is never served as current evidence (KM-7, all entry points)."""

    async def _run() -> None:
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

    asyncio.run(_run())


def test_index_hit_scores_survive_into_chunks(tmp_path: Path) -> None:
    """Vector chunks carry the index's own ranking score, not the neutral 0.5 default."""

    async def _run() -> None:
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

    asyncio.run(_run())


def test_type_filter_keeps_recall_past_global_top_k(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filtered query finds the eligible note even when the global top-k are all wrong-type."""

    async def _run() -> None:
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

    asyncio.run(_run())


def test_retriever_drops_a_stale_index_hit(tmp_path: Path) -> None:
    """A hit whose note is not on disk (stale derived row) is dropped, never cited."""

    async def _run() -> None:
        _write_note(tmp_path, "note-001", "amide coupling epimerization")
        index = await _index_for(tmp_path)
        # Delete the note from disk after indexing → the index row is now stale.
        (tmp_path / "note-001.md").unlink()
        retriever = VectorRetriever(index, notes_dir=str(tmp_path))
        assert await retriever.retrieve("amide coupling epimerization", {}) == []

    asyncio.run(_run())


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
    out = asyncio.run(research_tools.gather_evidence("q"))
    assert [c.source_note_id for c in out] == ["b", "a", "c"]


def test_gather_evidence_graph_mode_is_flat_union(monkeypatch: pytest.MonkeyPatch) -> None:
    """In the default graph mode gather_evidence keeps the flat, dedup'd union (unchanged)."""
    _wire_two_sources(monkeypatch)
    monkeypatch.setattr(settings, "retrieval_mode", "graph")
    out = asyncio.run(research_tools.gather_evidence("q"))
    assert [c.source_note_id for c in out] == ["a", "b", "c"]
