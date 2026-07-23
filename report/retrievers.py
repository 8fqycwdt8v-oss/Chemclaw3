"""Concrete source retrievers — thin adapters over existing layers (plan step 5b.3).

Two real sources behind the one `SourceRetriever` contract, proving the harness core is
source-agnostic (a third — analytics, or external literature — is another adapter here, not a
core change): `GraphRetriever` reads the knowledge graph (Phase 2), `FingerprintReactionRetriever`
runs reaction-fingerprint search (Phase 3). Neither introduces a new store. Every chunk they
emit carries the id of the note it came from, so the harness can cite it (5b.2).
"""

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from agents.embedding_provider import embed_texts
from chemclaw.config import settings
from kg.graph import load_notes
from kg.note import WIKILINK, Note
from mcp_servers.fpstore import FingerprintError, FingerprintStore
from mcp_servers.rxnfp.search import find_similar_reactions
from report.evidence import EvidenceChunk
from report.vector_index import IndexHit, NoteIndex, note_text


def _excerpt(body: str) -> str:
    """A report-sized excerpt of a note body, with wikilink markup stripped.

    An excerpt must not carry a source note's `[[links]]` verbatim into the report body —
    that would add unintended (possibly dangling) graph edges — so the shared `kg.note.WIKILINK`
    brackets are stripped, keeping the link target as plain text.
    """
    return WIKILINK.sub(r"\1", body.strip())[: settings.note_excerpt_chars]


class GraphRetriever:
    """Retrieve evidence from the Markdown knowledge graph. A `SourceRetriever`."""

    name = "graph"

    def __init__(self, notes_dir: str | None = None) -> None:
        """Read notes from the given directory, or the configured `knowledge_dir`."""
        self._dir = Path(notes_dir if notes_dir is not None else settings.knowledge_dir)

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks from notes matching `query` (substring) under type/tag `filters`.

        Deterministic, case-insensitive substring match over a note's id, tags, and body. Each
        hit is a real, existing note (this reads the graph), so its citation always resolves;
        but substring matching is a coarse *candidate* filter — a short query can match
        incidentally (`ester` in `polyester`). The `development-report` skill judges relevance;
        this retriever only guarantees the note exists, not that it answers the question.
        """
        if not self._dir.exists():
            return []
        needle = query.lower()
        want_type = filters.get("type")
        want_tag = filters.get("tag")
        today = date.today()
        chunks: list[EvidenceChunk] = []
        # load_notes is a synchronous full disk parse — offload it so the event loop
        # (the report worker) is not blocked (same pattern as agents/graph_tools.py).
        for note in await asyncio.to_thread(load_notes, self._dir):
            # A report cites current evidence only: skip a not-yet-valid or expired note so it
            # cannot be quoted as current guidance (KM-7). It remains in Git, just not cited here.
            if not note.is_current(today):
                continue
            if want_type is not None and note.type != want_type:
                continue
            if want_tag is not None and want_tag not in note.tags:
                continue
            # Same haystack the dense/lexical index build from (`note_text`), so all three entry
            # points agree on what "the note's content" is and cannot drift.
            haystack = note_text(note).lower()
            if needle in haystack:
                # Score a matched note by its own confidence (KM-5): every returned note already
                # matched the query, so among candidates the more-trusted note survives truncation
                # first. A note with no confidence takes the configured neutral default.
                score = (
                    note.confidence
                    if note.confidence is not None
                    else settings.retrieval_default_confidence
                )
                chunks.append(
                    EvidenceChunk(
                        content=_excerpt(note.body) or note.id,
                        source_note_id=note.id,
                        retriever=self.name,
                        score=score,
                    )
                )
        return chunks


class FingerprintReactionRetriever:
    """Retrieve reactions structurally similar to a reaction-SMILES query. A `SourceRetriever`."""

    name = "reaction-fingerprint"

    def __init__(self, store: FingerprintStore) -> None:
        """Search the given reaction fingerprint store (injected for testability)."""
        self._store = store

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for reactions similar to `query` (a reaction SMILES), or none.

        A query that is not a valid reaction SMILES yields no evidence (not an error) — each
        retriever answers only what its source can, so prose queries simply return empty here.
        Each match cites the corresponding `reaction-<id>` note. Unlike the graph retriever, this
        cites from the fingerprint index, whose entries are written at ingestion while the note
        is merged separately (D-018): a reaction indexed but whose note is still pending review
        yields a citation the report PR's kg-validate flags as dangling — surfacing the pending
        note to the reviewer (the PR-gate working), not silently corrupting the graph. Reports
        are therefore run over the merged corpus, as campaigns are.
        """
        try:
            matches = await find_similar_reactions(self._store, query)
        except FingerprintError:
            return []
        return [
            EvidenceChunk(
                content=f"Similar reaction {match.label} (Tanimoto {match.similarity:.2f})",
                source_note_id=f"reaction-{match.id}",
                retriever=self.name,
                # Structural hits score by their Tanimoto similarity — a closer precedent survives
                # truncation first (KM-5). Clamped to [0, 1] to stay a valid chunk score.
                score=min(max(match.similarity, 0.0), 1.0),
            )
            for match in matches
        ]


async def _eligible_notes(directory: Path, filters: dict[str, Any]) -> dict[str, Note]:
    """Load notes under the type/tag `filters` into an id→Note map (empty if the dir is absent).

    Shared by the dense and lexical retrievers so both honor the same `type`/`tag` filters the
    graph retriever does, and both resolve a hit's excerpt from the same on-disk note (the source
    of truth). Offloaded to a thread — `load_notes` is a synchronous full parse.
    """
    if not directory.exists():
        return {}
    want_type = filters.get("type")
    want_tag = filters.get("tag")
    notes: dict[str, Note] = {}
    for note in await asyncio.to_thread(load_notes, directory):
        if want_type is not None and note.type != want_type:
            continue
        if want_tag is not None and want_tag not in note.tags:
            continue
        notes[note.id] = note
    return notes


def _chunks_from_hits(
    hits: list[IndexHit], notes: dict[str, Note], retriever_name: str
) -> list[EvidenceChunk]:
    """Map index hits to cited evidence chunks, dropping any hit whose note no longer loads.

    A derived index can hold a stale row for a note deleted from disk, or one filtered out by
    type/tag; either way the note is not in `notes`, so the hit is dropped — the graph on disk
    stays authoritative and a citation never dangles.
    """
    chunks: list[EvidenceChunk] = []
    for hit in hits:
        note = notes.get(hit.note_id)
        if note is None:
            continue
        chunks.append(
            EvidenceChunk(
                content=_excerpt(note.body) or note.id,
                source_note_id=note.id,
                retriever=retriever_name,
            )
        )
    return chunks


class VectorRetriever:
    """Retrieve notes by dense-embedding similarity to the query. A `SourceRetriever` (F10-A).

    An *entry point* into the graph, not a replacement (D-004): it surfaces notes semantically
    related to the query even when they share no substring or wikilink with it, which the agent
    then expands via `expand_note`. The index backend is injected for testability.
    """

    name = "vector"

    def __init__(self, index: NoteIndex, notes_dir: str | None = None) -> None:
        """Search `index`; resolve excerpts from the given notes dir or `knowledge_dir`."""
        self._index = index
        self._dir = Path(notes_dir if notes_dir is not None else settings.knowledge_dir)

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for the notes most cosine-similar to `query` under the type/tag filters."""
        notes = await _eligible_notes(self._dir, filters)
        if not notes:
            return []
        query_embedding = (await asyncio.to_thread(embed_texts, [query]))[0]
        hits = await self._index.search_dense(query_embedding, settings.retrieval_top_k)
        return _chunks_from_hits(hits, notes, self.name)


class LexicalRetriever:
    """Retrieve notes by full-text term match (Postgres FTS). A `SourceRetriever` (F10-A).

    The lexical/BM25-style entry point: a ranked term match that beats the graph retriever's plain
    substring test (which cannot rank, and matches incidental substrings). Also an entry point into
    the graph, not a replacement (D-004). The index backend is injected for testability.
    """

    name = "lexical"

    def __init__(self, index: NoteIndex, notes_dir: str | None = None) -> None:
        """Search `index`; resolve excerpts from the given notes dir or `knowledge_dir`."""
        self._index = index
        self._dir = Path(notes_dir if notes_dir is not None else settings.knowledge_dir)

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for the notes best matching `query`'s terms under the type/tag filters."""
        notes = await _eligible_notes(self._dir, filters)
        if not notes:
            return []
        hits = await self._index.search_lexical(query, settings.retrieval_top_k)
        return _chunks_from_hits(hits, notes, self.name)
