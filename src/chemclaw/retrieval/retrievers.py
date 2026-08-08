"""Concrete source retrievers — thin adapters over existing layers (plan step 5b.3).

Two real sources behind the one `SourceRetriever` contract, proving the harness core is
source-agnostic (a third — analytics, or external literature — is another adapter here, not a
core change): `GraphRetriever` reads the knowledge graph (Phase 2), `FingerprintReactionRetriever`
runs reaction-fingerprint search (Phase 3). Neither introduces a new store. Every chunk they
emit carries the id of the note it came from, so the harness can cite it (5b.2).
"""

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.kg.conflicts import NoteConflicts, conflict_index
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import WIKILINK, Note, note_id_for_reaction, split_link
from chemclaw.kg.search import query_terms, term_coverage
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.vector_index import IndexHit, NoteIndex, default_note_index
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions
from chemclaw.science.fingerprints.store import FingerprintError, FingerprintStore, Match

log = logging.getLogger(__name__)


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
    experiment was run (D-162). "What have I tried on this step in the last two weeks" was
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
    if since is not None and note.valid_from < since:
        return False
    return not (until is not None and note.valid_from > until)


async def _conflict_index(directory: Path) -> dict[str, NoteConflicts]:
    """Map each note id to what it is known or suspected to disagree with (KM-8).

    The whole computation goes to a worker thread, not only the note load: the scan over the corpus
    is the expensive half (measured at 1,525 ms on a 2,000-note programme-shaped corpus), and it
    used to run on the event loop, where it stalled every other concurrent turn on the worker.
    `chemclaw.kg.conflicts.conflict_index` caches the result behind the same stat fingerprint the
    parsed notes and the assembled graph are cached behind, so the three note-backed retrievers of
    one sweep now compute it once between them instead of once each.
    """
    return await asyncio.to_thread(conflict_index, directory, date.today())


class GraphRetriever:
    """Retrieve evidence from the Markdown knowledge graph. A `SourceRetriever`."""

    def __init__(self, notes_dir: str | None = None, name: str = "graph") -> None:
        """Read notes from the given directory, or the configured `knowledge_dir`.

        `name` is the data-source name this retriever is cited under, passed by
        `chemclaw.ingest.sources.registry` from the manifest. It defaults to the name of the
        folder that ships this retriever, so direct construction in a test or a script keeps
        working without repeating it.
        """
        self._dir = Path(notes_dir) if notes_dir is not None else settings.knowledge_path
        self.name = name

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks from notes matching every term of `query`, ranked best first.

        Deterministic and case-insensitive over `chemclaw.kg.search.search_text` — the note's id,
        type, structure, tags and body, the one haystack the dense index, the lexical index,
        `find_notes` and the digest all read. Until that was made one function this retriever
        searched a *narrower* text than the agent's own `find_notes`, so a note the model had just
        found by its type or its SMILES could not then be cited in a report.

        Matching is per *term*, not on the query
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
        terms = query_terms(query)
        conflicts = await _conflict_index(self._dir)
        scored: list[tuple[int, EvidenceChunk]] = []
        for note in (await _eligible_notes(self._dir, filters)).values():
            coverage = term_coverage(note, terms)
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
            scored.append((coverage, _chunk_for(note, self.name, score, conflicts.get(note.id))))
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


# The filter keys `_eligible_notes` understands. Named here so "did the caller ask to narrow this?"
# is one check rather than four, and so a filter added to the gate is added in one place.
_NOTE_FILTERS = ("type", "tag", "since", "until")


class FingerprintReactionRetriever:
    """Retrieve reactions structurally similar to a reaction-SMILES query. A `SourceRetriever`."""

    name = "reaction-fingerprint"

    def __init__(self, store: FingerprintStore, notes_dir: str | None = None) -> None:
        """Search the given reaction fingerprint store, resolving notes from `notes_dir`.

        The store is injected for testability; the directory is the corpus a metadata filter is
        resolved against, and is only read when a filter is actually given.
        """
        self._store = store
        self._dir = Path(notes_dir) if notes_dir is not None else settings.knowledge_path

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

        **`type`/`tag`/`since`/`until` narrow the result** when given (D-170). The fingerprint index
        holds bits and a label and knows nothing about note metadata, so the filter cannot go into
        its SQL: this searches *deeper* than the requested page and applies the corpus's own
        eligibility gate to the neighbours, then truncates. Filtering the page instead would let a
        single unwanted neighbour cost a wanted one, and a filtered search would return fewer hits
        the *better* the index got at surfacing near-duplicates.

        With no filter the behaviour is byte-for-byte what it was, pending-note citation included.
        """
        wanted = {key: filters[key] for key in _NOTE_FILTERS if filters.get(key) is not None}
        page = settings.fingerprint_top_k
        try:
            # `.hits` here and not the whole search: a retriever's contract is evidence chunks, and
            # an unbuilt index yields none of those. The distinction the search now carries is for
            # the *conversational* tools, where a chemist reads "nothing similar" as an answer; a
            # report leg that contributes no chunks is visible to its reviewer as a missing source.
            matches = (
                await find_similar_reactions(
                    self._store, query, top_k=self._depth(page) if wanted else None
                )
            ).hits
        except FingerprintError:
            return []
        if wanted:
            matches = await self._eligible(matches, wanted, page)
        return [
            EvidenceChunk(
                content=f"Similar reaction {match.label} (Tanimoto {match.similarity:.2f})",
                source_note_id=note_id_for_reaction(match.id),
                retriever=self.name,
                # Structural hits score by their Tanimoto similarity — a closer precedent survives
                # truncation first (KM-5). Clamped to [0, 1] to stay a valid chunk score.
                score=min(max(match.similarity, 0.0), 1.0),
            )
            for match in matches
        ]

    @staticmethod
    def _depth(page: int) -> int:
        """How many neighbours to ask the index for when a filter will thin them afterwards.

        Bounded by `fingerprint_max_top_k`, the same ceiling every other caller of the index is
        clamped to: the over-fetch may not become a way around the one cap on how much of the
        index a single query can pull into memory.
        """
        return min(page * settings.retrieval_filter_overfetch, settings.fingerprint_max_top_k)

    async def _eligible(
        self, matches: list[Match], wanted: dict[str, Any], page: int
    ) -> list[Match]:
        """Keep the neighbours whose note passes `wanted`, most similar first, truncated to `page`.

        A match whose note is not on disk is dropped here, which is the one place the pending-note
        citation above does *not* apply — and deliberately. A filter says "only notes that are X";
        a note nobody can read cannot be shown to be X, so serving it would answer a narrowed
        question with an unnarrowed hit. Same rule as `_in_window`'s undated note, for the same
        reason. An unfiltered sweep never reaches this and still surfaces the pending note.
        """
        eligible = await _eligible_notes(self._dir, wanted)
        kept = [match for match in matches if note_id_for_reaction(match.id) in eligible]
        if len(matches) >= self._depth(page) and len(kept) < page:
            # The deeper search was itself exhausted and still did not fill a page, so there may be
            # matching reactions further down the ranking that were never looked at. Said out loud
            # rather than returning a short list that reads as "this is all there is".
            log.warning(
                "filtered reaction search returned %d of %d wanted hits after scanning the "
                "%d-neighbour limit; raise CHEMCLAW_RETRIEVAL_FILTER_OVERFETCH to look deeper",
                len(kept),
                page,
                len(matches),
            )
        return kept[:page]


def _chunk_for(
    note: Note, retriever_name: str, score: float, conflicts: NoteConflicts | None
) -> EvidenceChunk:
    """Build one evidence chunk from a note, carrying its provenance (D-160).

    One builder for every note-backed retriever, so provenance cannot be attached on the graph
    path and forgotten on the index path — which is precisely the drift that would produce a
    partially-provenanced evidence list, the worst of the three possible states.
    """
    return EvidenceChunk(
        content=_excerpt(note.body) or note.id,
        source_note_id=note.id,
        retriever=retriever_name,
        score=score,
        conflicts_with=conflicts.ids if conflicts else [],
        conflicts_total=conflicts.total if conflicts else 0,
        created_by=note.created_by,
        source=note.source or "",
        confidence=note.confidence,
    )


def _chunks_from_hits(
    hits: list[IndexHit],
    notes: dict[str, Note],
    retriever_name: str,
    conflicts: dict[str, NoteConflicts] | None = None,
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
            _chunk_for(
                note,
                retriever_name,
                min(max(hit.score, 0.0), 1.0),
                (conflicts or {}).get(note.id),
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

    def __init__(
        self,
        index: NoteIndex | None = None,
        notes_dir: str | None = None,
        name: str = "vector",
    ) -> None:
        """Search `index` (the production note index by default); excerpts from `notes_dir`.

        `name` is the data-source name, passed by the registry from the manifest — see
        `GraphRetriever.__init__` for why every retrieve half takes one.
        """
        self._index = index if index is not None else default_note_index()
        self._dir = Path(notes_dir) if notes_dir is not None else settings.knowledge_path
        self.name = name

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

    def __init__(
        self,
        index: NoteIndex | None = None,
        notes_dir: str | None = None,
        name: str = "lexical",
    ) -> None:
        """Search `index` (the production note index by default); excerpts from `notes_dir`.

        `name` is the data-source name, passed by the registry from the manifest — see
        `GraphRetriever.__init__` for why every retrieve half takes one.
        """
        self._index = index if index is not None else default_note_index()
        self._dir = Path(notes_dir) if notes_dir is not None else settings.knowledge_path
        self.name = name

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for the notes best matching `query`'s terms under the type/tag filters."""
        notes = await _eligible_notes(self._dir, filters)
        if not notes:
            return []
        # Scoped to the eligible notes for the same recall reason as the dense retriever.
        hits = await self._index.search_lexical(query, settings.retrieval_top_k, within=set(notes))
        return _chunks_from_hits(hits, notes, self.name, await _conflict_index(self._dir))
