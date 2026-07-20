"""Concrete source retrievers — thin adapters over existing layers (plan step 5b.3).

Two real sources behind the one `SourceRetriever` contract, proving the harness core is
source-agnostic (a third — analytics, or external literature — is another adapter here, not a
core change): `GraphRetriever` reads the knowledge graph (Phase 2), `FingerprintReactionRetriever`
runs reaction-fingerprint search (Phase 3). Neither introduces a new store. Every chunk they
emit carries the id of the note it came from, so the harness can cite it (5b.2).
"""

from pathlib import Path
from typing import Any

from chemclaw.config import settings
from kg.graph import load_notes
from mcp_servers.fpstore import FingerprintError, FingerprintStore
from mcp_servers.rxnfp.search import find_similar_reactions
from report.evidence import EvidenceChunk

# How much of a note's body to carry as an evidence excerpt (keeps the report readable).
_EXCERPT_CHARS = 240


class GraphRetriever:
    """Retrieve evidence from the Markdown knowledge graph. A `SourceRetriever`."""

    name = "graph"

    def __init__(self, notes_dir: str | None = None) -> None:
        """Read notes from the given directory, or the configured `knowledge_dir`."""
        self._dir = Path(notes_dir if notes_dir is not None else settings.knowledge_dir)

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks from notes matching `query` (substring) under type/tag `filters`.

        Deterministic, case-insensitive substring match over a note's id, tags, and body —
        the graph is the source of truth, so a match is a real, citable note, never a guess.
        """
        if not self._dir.exists():
            return []
        needle = query.lower()
        want_type = filters.get("type")
        want_tag = filters.get("tag")
        chunks: list[EvidenceChunk] = []
        for note in load_notes(self._dir):
            if want_type is not None and note.type != want_type:
                continue
            if want_tag is not None and want_tag not in note.tags:
                continue
            haystack = f"{note.id} {' '.join(note.tags)} {note.body}".lower()
            if needle in haystack:
                chunks.append(
                    EvidenceChunk(
                        content=note.body.strip()[:_EXCERPT_CHARS] or note.id,
                        source_note_id=note.id,
                        retriever=self.name,
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
        Each match cites the corresponding `reaction-<id>` note.
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
            )
            for match in matches
        ]
