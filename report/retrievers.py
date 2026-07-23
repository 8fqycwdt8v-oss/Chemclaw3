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


async def _eligible_notes(directory: Path, filters: dict[str, Any]) -> dict[str, Note]:
    """Load the notes eligible as current evidence under `filters`, as an id→Note map.

    The one eligibility gate for every graph-backed retriever (graph, dense, lexical): the type/tag
    filters plus the currency check — a not-yet-valid or expired note is never served as current
    evidence (KM-7), whichever entry point found it. It stays in Git and reachable by explicit id;
    it is only dropped from current-evidence sweeps. Offloaded to a thread — `load_notes` is a
    synchronous full parse. Empty when the directory is absent.
    """
    if not directory.exists():
        return {}
    want_type = filters.get("type")
    want_tag = filters.get("tag")
    today = date.today()
    notes: dict[str, Note] = {}
    for note in await asyncio.to_thread(load_notes, directory):
        if not note.is_current(today):
            continue
        if want_type is not None and note.type != want_type:
            continue
        if want_tag is not None and want_tag not in note.tags:
            continue
        notes[note.id] = note
    return notes


class GraphRetriever:
    """Retrieve evidence from the Markdown knowledge graph. A `SourceRetriever`."""

    name = "graph"

    def __init__(self, notes_dir: str | None = None) -> None:
        """Read notes from the given directory, or the configured `knowledge_dir`."""
        self._dir = Path(notes_dir if notes_dir is not None else settings.knowledge_dir)

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks from notes matching `query` (substring), ranked best first.

        Deterministic, case-insensitive substring match over a note's id, tags, and body. Each
        hit is a real, existing note (this reads the graph), so its citation always resolves;
        but substring matching is a coarse *candidate* filter — a short query can match
        incidentally (`ester` in `polyester`). The `development-report` skill judges relevance;
        this retriever only guarantees the note exists, not that it answers the question.
        """
        needle = query.lower()
        chunks: list[EvidenceChunk] = []
        for note in (await _eligible_notes(self._dir, filters)).values():
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
        # RRF reads each source's list as ranked best-first, so the list must be ordered by this
        # retriever's own relevance signal — disk order is not a ranking. Note id breaks ties
        # deterministically.
        chunks.sort(key=lambda chunk: (-chunk.score, chunk.source_note_id))
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


def _chunks_from_hits(
    hits: list[IndexHit], notes: dict[str, Note], retriever_name: str
) -> list[EvidenceChunk]:
    """Map index hits to cited evidence chunks, dropping any hit whose note no longer loads.

    A derived index can hold a stale row for a note deleted from disk (or a backend may ignore
    the `within` scope); any hit not in `notes` is dropped — the graph on disk stays authoritative
    and a citation never dangles. The hit's own score (cosine similarity / `ts_rank`) survives
    into the chunk so downstream ranking keeps the index's ordering signal; it is clamped to the
    chunk's [0, 1] score domain because `ts_rank` is not bounded by 1.
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
                score=min(max(hit.score, 0.0), 1.0),
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
        # Scope the index query to the eligible notes so the top-k slots are spent on notes the
        # filters allow — filtering after a global top-k would silently lose eligible matches
        # whenever the nearest neighbors are ineligible.
        hits = await self._index.search_dense(
            query_embedding, settings.retrieval_top_k, within=set(notes)
        )
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
        # Scoped to the eligible notes for the same recall reason as the dense retriever.
        hits = await self._index.search_lexical(query, settings.retrieval_top_k, within=set(notes))
        return _chunks_from_hits(hits, notes, self.name)
