"""Hybrid retrieval: the vector/lexical retrievers, RRF fusion, and gather_evidence's mode switch.

Offline with an in-memory index and fake sources — proves the new retrievers cite real notes and
honor filters, that Reciprocal Rank Fusion rewards notes ranked by more than one source, and that
`gather_evidence` fuses in `hybrid` mode while keeping the flat union in `graph` mode (the default).
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


def test_retriever_drops_a_stale_index_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit whose note is not on disk (stale derived row) is dropped, never cited.

    Pins `graph_cache_ttl_seconds = 0` because this asserts the *disk-authoritative* guard, and
    the TTL window (DA-5) deliberately skips the disk scan. That window does not weaken the guard
    in production: it exists to compensate for a derived index rebuilt by a background job, whose
    staleness is minutes-to-hours — against that, seconds are noise. The test needs the scan to
    run to be deterministic.
    """
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)

    async def _run() -> None:
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
    out = asyncio.run(research_tools.gather_evidence("q")).chunks
    assert [c.source_note_id for c in out] == ["b", "a", "c"]


def test_gather_evidence_graph_mode_round_robins_the_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the default graph mode gather_evidence merges the sources by rank, de-duplicating.

    Named for what it does rather than "flat union": the merge takes each source's best hit before
    any source's second, which for these two two-hit sources is the same answer the old
    concatenation gave, and is not the same answer once one source is longer than the cap.
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
    """No enabled source is starved by the cap, whatever scale its scores happen to use.

    The measured defect, with the measured numbers. `EvidenceChunk.score` is a note's `confidence`
    from the graph, a `ts_rank` from Postgres FTS, a cosine from the dense index and a Tanimoto
    from the fingerprint store — comparable *within* a source and meaningless across them, which
    the field's own docstring states. `graph` mode nonetheless concatenated the lists and sorted
    the union by that number, so the cap kept a prefix of whichever scale ran highest.

    Against this fixture (45 graph hits at the notes' 0.8 confidence, 8 lexical at ts_rank
    0.02-0.09, 7 dense at cosine 0.60-0.85, 40-chunk cap) the flat union returned 38 graph /
    0 lexical / 2 vector; with the sort removed it returned 40 / 0 / 0, so the concatenation order
    alone starved the later sources and the sort was mitigating rather than causing it. The lexical
    leg contributed nothing an agent could read either way, which is the entire reason a deployment
    enables it. Round-robin gives every source its best hit before any source gets its second.
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
    """The merge never re-ranks a source, because the source already ranked itself.

    Concretely the case the union's score sort broke: on a widened search `GraphRetriever` orders
    by term *coverage* first and confidence only within it, so a note matching three of four terms
    leads one matching a single term. Re-sorting the union by score alone discarded that and put
    the confident near-miss on top. The default deployment runs exactly one text source, so this is
    the ordering most sweeps actually get.
    """
    ranked = _ranked("n", "graph", [0.2, 0.9, 0.5])  # the retriever's order, not score order
    monkeypatch.setattr(research_tools, "_text_retrievers", lambda: [_FakeSource("graph", ranked)])
    monkeypatch.setattr(settings, "retrieval_mode", "graph")

    out = asyncio.run(research_tools.gather_evidence("q")).chunks

    assert [chunk.source_note_id for chunk in out] == ["n0", "n1", "n2"]


# --- the tier knob may not starve a leg --------------------------------------------------------


def test_an_allowed_source_weight_cannot_starve_a_leg_to_zero() -> None:
    """The knob meant to tune the merge could reintroduce the defect the merge exists to prevent.

    `reciprocal_rank_fusion` divides the rank by the weight, so a source at weight `w` fuses its
    rank-1 hit at *effective* rank `1/w`. Measured over six sources of eight hits each against the
    shipped 40-chunk cap, with every other weight at the default 1.0:

    | lexical weight | its best hit's fused index (0-based, of 48) | lexical chunks kept |
    | --- | --- | --- |
    | 1.0   |  1 | 7 |
    | 0.5   |  6 | 4 |
    | 0.2   | 21 | 1 |
    | 0.125 | 36 | 1 |
    | 0.1   | 40 | **0** |

    At 0.1 the leg's own best hit sits behind all five other sources' complete eight-hit tails and
    the cap ends before it — "one leg contributing nothing at all", which is exactly what
    `D-2026-08-01-a-cap-that-starves-a-source` is about and what `hybrid.py` claimed no weight
    could do. The config is where that is refused, because the fusion cannot know the cap.
    """
    from pydantic import ValidationError

    from chemclaw.core.config.retrieval import RetrievalSettings

    with pytest.raises(ValidationError, match="0.5"):
        RetrievalSettings(retrieval_source_weights={"lexical": 0.1})
    with pytest.raises(ValidationError, match="2"):
        RetrievalSettings(retrieval_source_weights={"graph": 8.0})
    # The ENV comment's own worked example stays expressible — the bound is what makes its
    # documented property true, not a narrowing of what a deployment wanted to say.
    assert RetrievalSettings(
        retrieval_source_weights={"graph": 1.5, "vector": 0.8}
    ).retrieval_source_weights


def test_no_allowed_weighting_pushes_a_source_behind_four_of_anyone_else() -> None:
    """The bound the range buys, measured at both of its extremes.

    With every weight in `[1/W, W]`, a source's own best hit fuses at effective rank at most `W`,
    and another source's rank `r` fuses at effective rank at least `r / W` — so it can precede only
    while `r <= W²` (equality included, because a tie breaks on the note id and may break either
    way). At `W = 2` that is four chunks per other source, so a sweep of `S` sources keeps every
    leg's best hit inside any cap above `4 (S - 1)`; at the shipped 40-chunk cap that holds to
    eleven sources.

    Asserted at the worst case the range can express: one source at the floor against five at the
    ceiling. Measured, the floored leg's best hit lands at fused index 16 of 48 — inside the
    bound, and inside the cap.
    """
    names = ("graph", "vector", "lexical", "share", "warehouse", "vendored")
    lists = [_ranked(f"{name}-", name, [0.5] * 8) for name in names]
    weights = dict.fromkeys(["graph", "vector", "share", "warehouse", "vendored"], 2.0)
    weights["lexical"] = 0.5

    fused = reciprocal_rank_fusion(lists, k=settings.retrieval_fusion_k, weights=weights)
    order = [chunk.retriever for chunk in fused]

    for name in weights:
        first = order.index(name)
        assert first <= 4 * (len(lists) - 1), f"{name}'s best hit is at fused position {first}"
    kept = Counter(order[: settings.gather_evidence_max_chunks])
    assert all(kept[name] > 0 for name in weights), f"a leg was starved: {dict(kept)}"
