"""Concrete source retrievers — thin adapters over existing layers (plan step 5b.3).

Two real sources behind the one `SourceRetriever` contract, proving the harness core is
source-agnostic (a third — analytics, or external literature — is another adapter here, not a
core change): `GraphRetriever` reads the knowledge graph (Phase 2), `FingerprintReactionRetriever`
runs reaction-fingerprint search (Phase 3). Neither introduces a new store. Every chunk they
emit carries the id of the note it came from, so the harness can cite it (5b.2).
"""

import asyncio
import re
from datetime import date
from pathlib import Path
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.kg.conflicts import conflicts_by_note, find_conflicts
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import WIKILINK, Note, split_link
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.vector_index import IndexHit, NoteIndex, default_note_index, note_text
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions
from chemclaw.science.fingerprints.store import FingerprintError, FingerprintStore

# Words that carry no retrieval signal but do carry the difference between "biaryl" (three hits)
# and "the biaryl" (none) under a whole-phrase match. Deliberately tiny and English-only: this is
# not stemming or a language model, it is the handful of words a chemist puts around the term they
# actually mean. A longer list would start discarding real chemistry ("in situ", "on water").
_STOPWORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "is", "of", "on", "or", "our", "the", "to", "with"}
)
# Below this a term matches too much to be worth requiring; two characters is already `pd`.
_MIN_TERM_CHARS = 2


def _query_terms(query: str) -> list[str]:
    """The terms a note must contain to match `query` — lowercased, split on non-word characters.

    Punctuation splits rather than being stripped, because a chemist's query carries structure in
    it (`Pd(OAc)2`, `4-bromoanisole`, `reactants>>products`) and the parts are what a note's text
    holds. Falls back to the whole query when nothing survives filtering — a search for `the` is
    still a search, and returning "no terms, therefore everything" would be worse than literal.
    """
    terms = [
        term
        for term in re.split(r"[^0-9a-z]+", query.lower())
        if len(term) >= _MIN_TERM_CHARS and term not in _STOPWORDS
    ]
    return terms or [query.lower()]


def _excerpt(body: str) -> str:
    """A report-sized excerpt of a note body, with wikilink markup stripped.

    An excerpt must not carry a source note's `[[links]]` verbatim into the report body —
    that would add unintended (possibly dangling) graph edges — so the shared
    `chemclaw.kg.note.WIKILINK`
    brackets are stripped, keeping the link target as plain text.

    Strips to the *target*, via `chemclaw.kg.note.split_link`, not to the whole bracket contents:
    with typed
    edges a link may read `[[precursor-of:compound-x]]`, and substituting the raw group would drop
    `precursor-of:compound-x` into prose a person reads. One shared splitter, so the report layer
    and the graph indexer cannot disagree about what a link points at.
    """
    stripped = WIKILINK.sub(lambda match: split_link(match.group(1))[1], body.strip())
    return stripped[: settings.note_excerpt_chars]


async def _eligible_notes(directory: Path, filters: dict[str, Any]) -> dict[str, Note]:
    """Load the notes eligible as current evidence under `filters`, as an id→Note map.

    The one eligibility gate for every graph-backed retriever (graph, dense, lexical): the
    type/tag/date filters plus the currency check — a not-yet-valid or expired note is never
    served as current evidence (KM-7), whichever entry point found it. It stays in Git and
    reachable by explicit id; it is only dropped from current-evidence sweeps. Offloaded to a
    thread — `load_notes` is a synchronous full parse. Empty when the directory is absent.

    `since`/`until` window the notes by `valid_from` — for a reaction note, the day the
    experiment was run (D-157). "What have I tried on this step in the last two weeks" was
    unanswerable without it: the dates were on the notes and no filter could reach them.
    """
    if not directory.exists():
        return {}
    want_type = filters.get("type")
    want_tag = filters.get("tag")
    since = filters.get("since")
    until = filters.get("until")
    today = date.today()
    notes: dict[str, Note] = {}
    for note in await asyncio.to_thread(load_notes, directory):
        if not note.is_current(today):
            continue
        if want_type is not None and note.type != want_type:
            continue
        if want_tag is not None and want_tag not in note.tags:
            continue
        if not _in_window(note, since, until):
            continue
        notes[note.id] = note
    return notes


def _in_window(note: Note, since: date | None, until: date | None) -> bool:
    """Whether the note falls inside the requested date window (no window = everything).

    An undated note fails a windowed query rather than passing it. It cannot be *shown* to fall
    in the window, and the query that asks for one is asking what happened in a period — serving
    a note of unknown date would answer a question the caller did not ask. Unwindowed sweeps are
    unaffected, which is every existing call.
    """
    if since is None and until is None:
        return True
    if note.valid_from is None:
        return False
    return not (since is not None and note.valid_from < since) and not (
        until is not None and note.valid_from > until
    )


async def _conflict_index(directory: Path) -> dict[str, list[str]]:
    """Map each note id to the ids it is known or suspected to disagree with (KM-8).

    Computed over the *whole* current corpus rather than over the notes a query happened to match:
    a chunk must be flagged even when the note it conflicts with was not itself retrieved, which is
    exactly the case where a reader would otherwise see one side and assume it settled. Reads
    through the shared parsed-note cache, so this costs a stat scan on a warm query, not a parse.
    """
    if not directory.exists() or not settings.conflict_detection_enabled:
        return {}
    notes = await asyncio.to_thread(load_notes, directory)
    index = conflicts_by_note(find_conflicts(notes, as_of=date.today()))
    return {
        note_id: sorted(
            {
                conflict.other_id if conflict.note_id == note_id else conflict.note_id
                for conflict in conflicts
            }
        )
        for note_id, conflicts in index.items()
    }


class GraphRetriever:
    """Retrieve evidence from the Markdown knowledge graph. A `SourceRetriever`."""

    name = "graph"

    def __init__(self, notes_dir: str | None = None) -> None:
        """Read notes from the given directory, or the configured `knowledge_dir`."""
        self._dir = Path(notes_dir) if notes_dir is not None else settings.knowledge_path

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks from notes matching every term of `query`, ranked best first.

        Deterministic and case-insensitive over a note's id, tags, and body — the same haystack
        the dense and lexical indexes build from. Matching is per *term*, not on the query
        verbatim: a whole-phrase substring test only found a note that literally contained the
        sentence a chemist typed, so `biaryl` returned the campaign, the compound and the
        playbook while `the biaryl` returned nothing at all (D-138). That is the failure mode
        that matters here, and it was the opposite of the one the old docstring warned about:
        under-matching, silently, on ordinary phrasing. With the graph retriever the only source
        enabled by default, an empty result sent the agent back to the chemist asking for
        details the record already held.

        Every term must be present, so precision is unchanged for a query that used to work — a
        phrase match implies all its terms match. When nothing satisfies all of them the search
        widens to any term rather than answering "nothing known", and coverage then orders the
        result: a note matching three of four terms outranks one matching a single term.

        This remains a coarse *candidate* filter — a short term can still match incidentally
        (`ester` in `polyester`). The `development-report` skill judges relevance; this retriever
        only guarantees the note exists, not that it answers the question.
        """
        terms = _query_terms(query)
        conflicts = await _conflict_index(self._dir)
        scored: list[tuple[int, EvidenceChunk]] = []
        for note in (await _eligible_notes(self._dir, filters)).values():
            haystack = note_text(note).lower()
            coverage = sum(1 for term in terms if term in haystack)
            if not coverage:
                continue
            # Score a matched note by its own confidence (KM-5): among candidates the
            # more-trusted note survives truncation first. A note with no confidence takes the
            # configured neutral default.
            score = (
                note.confidence
                if note.confidence is not None
                else settings.retrieval_default_confidence
            )
            scored.append(
                (
                    coverage,
                    EvidenceChunk(
                        content=_excerpt(note.body) or note.id,
                        source_note_id=note.id,
                        retriever=self.name,
                        score=score,
                        conflicts_with=conflicts.get(note.id, []),
                    ),
                )
            )
        complete = [pair for pair in scored if pair[0] == len(terms)]
        # RRF reads each source's list as ranked best-first, so the list must be ordered by this
        # retriever's own relevance signal — disk order is not a ranking. Coverage leads only on
        # the widened search (on the complete one it is the same for every hit, so this reduces
        # to confidence exactly as before). Note id breaks ties deterministically.
        return [
            chunk
            for _, chunk in sorted(
                complete or scored,
                key=lambda pair: (-pair[0], -pair[1].score, pair[1].source_note_id),
            )
        ]


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
    hits: list[IndexHit],
    notes: dict[str, Note],
    retriever_name: str,
    conflicts: dict[str, list[str]] | None = None,
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
                conflicts_with=(conflicts or {}).get(note.id, []),
            )
        )
    return chunks


class VectorRetriever:
    """Retrieve notes by dense-embedding similarity to the query. A `SourceRetriever` (F10-A).

    An *entry point* into the graph, not a replacement (D-004): it surfaces notes semantically
    related to the query even when they share no substring or wikilink with it, which the agent
    then expands via `expand_note`. The index backend is injected for testability, and defaults to
    the production one so `sources/vector/datasource.yaml` can name this class directly — a
    manifest passes construction *config*, not pre-built objects.
    """

    name = "vector"

    def __init__(self, index: NoteIndex | None = None, notes_dir: str | None = None) -> None:
        """Search `index` (the production note index by default); excerpts from `notes_dir`."""
        self._index = index if index is not None else default_note_index()
        self._dir = Path(notes_dir) if notes_dir is not None else settings.knowledge_path

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
        return _chunks_from_hits(hits, notes, self.name, await _conflict_index(self._dir))


class LexicalRetriever:
    """Retrieve notes by full-text term match (Postgres FTS). A `SourceRetriever` (F10-A).

    The lexical/BM25-style entry point: a ranked term match that beats the graph retriever's plain
    substring test (which cannot rank, and matches incidental substrings). Also an entry point into
    the graph, not a replacement (D-004). The index backend is injected for testability, and
    defaults to the production one for the same reason as `VectorRetriever`.
    """

    name = "lexical"

    def __init__(self, index: NoteIndex | None = None, notes_dir: str | None = None) -> None:
        """Search `index` (the production note index by default); excerpts from `notes_dir`."""
        self._index = index if index is not None else default_note_index()
        self._dir = Path(notes_dir) if notes_dir is not None else settings.knowledge_path

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for the notes best matching `query`'s terms under the type/tag filters."""
        notes = await _eligible_notes(self._dir, filters)
        if not notes:
            return []
        # Scoped to the eligible notes for the same recall reason as the dense retriever.
        hits = await self._index.search_lexical(query, settings.retrieval_top_k, within=set(notes))
        return _chunks_from_hits(hits, notes, self.name, await _conflict_index(self._dir))
