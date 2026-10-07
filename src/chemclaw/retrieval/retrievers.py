"""Concrete source retrievers — thin adapters over existing layers.

Four retrievers behind the one `SourceRetriever` contract: `GraphRetriever` reads the knowledge
graph, `FingerprintReactionRetriever` runs reaction-fingerprint search, and
`VectorRetriever`/`LexicalRetriever` read the derived note index. None introduces a new store, and
every chunk carries the id of the note it came from so the harness can cite it.

The graph leg overlaps the lexical one (both lexical over the same notes) and is kept because it is
the only note leg that needs no derived index: the index is rebuilt only where `lexical` or `vector`
is in `CHEMCLAW_DATA_SOURCES`, and never on a write, so a note just recorded is visible here
immediately.
"""

import asyncio
import logging
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.kg.conflicts import NoteConflicts, conflict_index
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import Note, note_id_for_reaction, strip_links
from chemclaw.kg.search import (
    matched_terms,
    query_terms,
    term_coverage,
    term_frequencies,
)
from chemclaw.retrieval.evidence import EvidenceChunk, Hits, RetrieverSkip
from chemclaw.retrieval.vector_index import IndexHit, NoteIndex, default_note_index
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions
from chemclaw.science.fingerprints.store import FingerprintInputError, FingerprintStore, Match

log = logging.getLogger(__name__)


def _excerpt(body: str, terms: Sequence[str] = ()) -> str:
    """A report-sized excerpt of a note body, windowed on the match, with wikilinks stripped.

    Links are stripped so an excerpt cannot add (possibly dangling) graph edges to a report; the
    report strips them again for chunks that never reach this function. The window follows the match
    because a reviewer has to see what the note was retrieved for. With no `terms`, or a match not
    in the body (found by id, type, tags or structure), this is a plain prefix.
    """
    stripped = strip_links(body.strip())
    window = settings.note_excerpt_chars
    if len(stripped) <= window:
        return stripped
    start = _window_start(stripped, terms, window)
    if start == 0:
        return stripped[:window]
    # The leading marker is inside the budget, not added to it, and marks an excerpt that does not
    # start at the note's opening.
    return "…" + stripped[start : start + window - 1]


def _window_start(text: str, terms: Sequence[str], window: int) -> int:
    """Where to start the excerpt so it shows as much of the query as one window can. `0` = head.

    Picks the candidate window (one per matched term, plus the head) that shows the most of the
    query, each term weighted by `1/count` in this note: a term occurring once points at a place,
    one occurring often points nowhere. Rarity is measured inside the note rather than against the
    corpus so every leg uses the same rule. A third of the budget precedes the match so the sentence
    it sits in is visible, and the start is moved to the next word boundary. Cost is bounded by the
    query's length, not the body's.
    """
    lowered = text.casefold()
    # Matched terms with their counts: both the weight and the membership test. A note found by its
    # id, type, tags or SMILES has no candidate and falls through to the head.
    folded = dict.fromkeys(term.casefold() for term in terms)
    counts = {term: count for term in folded if (count := lowered.count(term))}
    if not counts:
        return 0
    limit = max(0, len(text) - window)
    candidates = {0} | {min(max(0, lowered.find(term) - window // 3), limit) for term in counts}

    def shown(start: int) -> float:
        """The weight of the query visible in the window opening at `start`."""
        visible = lowered[start : start + window]
        return sum(1.0 / count for term, count in counts.items() if term in visible)

    # Ties go to the earliest start, so the head wins whenever it shows as much as anywhere else
    # and an excerpt only moves when moving it buys something.
    start = max(candidates, key=lambda candidate: (shown(candidate), -candidate))
    if start == 0:  # the head already shows as much of the query as any window can
        return 0
    # Push forward to a word boundary, but never past the first term the chosen window shows —
    # otherwise the nudge that keeps the excerpt from opening mid-word cuts off what it opened for.
    anchor = min(
        offset
        for term in counts
        if (offset := lowered.find(term, start)) >= 0 and offset + len(term) <= start + window
    )
    space = text.find(" ", start)
    return start if space < 0 or space >= anchor else space + 1


async def _eligible_notes(directory: Path, filters: dict[str, Any]) -> dict[str, Note]:
    """Load the notes eligible as current evidence under `filters`, as an id→Note map.

    The one eligibility gate for every graph-backed retriever: type/tag/date filters plus currency —
    a not-yet-valid or expired note is never served as current evidence, though it stays reachable
    by explicit id. An unwindowed sweep judges currency against today; a windowed one asks what was
    true then (see `_eligible_sync`). `since`/`until` window by `valid_from`. Load and filter both
    run in a worker thread so the O(corpus) loop never stalls the shared event loop. Empty when the
    directory is absent.
    """
    return await asyncio.to_thread(_eligible_sync, directory, filters, date.today())


def _is_windowed(filters: dict[str, Any]) -> bool:
    """Whether this sweep names a period, which decides *both* date rules it applies.

    One definition for two consumers that must agree: `_eligible_sync` skips the currency check for
    a windowed sweep, and `_conflict_index` must scan the same set, or retired notes inside the
    window come back with a structurally empty `conflicts_with`.
    """
    return filters.get("since") is not None or filters.get("until") is not None


def _eligible_sync(directory: Path, filters: dict[str, Any], today: date) -> dict[str, Note]:
    """The synchronous body of `_eligible_notes`: load, then filter, in one worker thread.

    Separate so `GraphRetriever` can reuse it inside its own single thread hop. `today` is passed in
    so every note in one sweep is judged against the same date; it governs the unwindowed sweep
    only.
    """
    want_type = filters.get("type")
    want_tag = filters.get("tag")
    since = filters.get("since")
    until = filters.get("until")
    notes: dict[str, Note] = {}
    # The directory check runs in the worker thread with the load, keeping even the `stat` off the
    # event loop.
    windowed = _is_windowed(filters)
    for note in _load_if_present(directory):
        if want_type is not None and note.type != want_type:
            continue
        if want_tag is not None and want_tag not in note.tags:
            continue
        if not _in_window(note, since, until):
            continue
        # Currency is judged as of the period asked about: today with no window (a superseded note
        # is
        # not an answer to "what now"), and not at all with one, since a note `_in_window` admits
        # was
        # current somewhere inside the period by construction (`valid_to >= valid_from`).
        if not windowed and not note.is_current(today):
            continue
        notes[note.id] = note
    return notes


def _rank_by_terms(
    directory: Path, filters: dict[str, Any], terms: Sequence[str], today: date
) -> tuple[list[tuple[int, float, float, Note]], int]:
    """`GraphRetriever`'s whole search, synchronously: eligible notes, scored, ranked and cut.

    One function so the whole O(corpus) pass runs in a single worker-thread hop.

    Returns:
        `(chosen, found)` — `(coverage, relevance, confidence, note)` for the best `retrieval_top_k`
        matches, best first, and the pre-cut total, so a cut does not look like a corpus.
    """
    frequencies: list[tuple[int, dict[str, int], Note]] = []
    for note in _eligible_sync(directory, filters, today).values():
        # Membership is `term_coverage` (substring, so `ester` finds `polyester`); weight is
        # `term_frequencies`. Gating on the frequency dict would silently narrow recall.
        coverage = term_coverage(note, terms)
        if not coverage:
            continue
        frequencies.append((coverage, term_frequencies(note, terms), note))
    # Document frequency over the matched population equals that over the eligible one: a note
    # containing the term matched.
    population = len(frequencies)
    document_frequency: dict[str, int] = {}
    for _, counts, _ in frequencies:
        for term in counts:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    scored: list[tuple[int, float, float, Note]] = []
    for coverage, counts, note in frequencies:
        # Confidence is a trust signal: it breaks ties between equally relevant notes, never decides
        # relevance. A note with none takes the configured neutral default.
        confidence = (
            note.confidence
            if note.confidence is not None
            else settings.retrieval_default_confidence
        )
        # `coverage`, not `len(counts)`: widening is keyed on substring matches, while `counts`
        # holds
        # only whole-token matches and is the weight.
        scored.append(
            (coverage, _relevance(counts, document_frequency, population), confidence, note)
        )
    complete = [entry for entry in scored if entry[0] == len(terms)]
    # RRF reads each list as best-first, so order by this leg's relevance; note id breaks ties.
    # Rank, then cut to `retrieval_top_k`, then materialize, so this leg is bounded like its
    # siblings
    # and does not crowd the merge.
    ranked = sorted(
        complete or scored,
        key=lambda entry: (-entry[0], -entry[1], -entry[2], entry[3].id),
    )
    return ranked[: settings.retrieval_top_k], len(ranked)


def _load_if_present(directory: Path) -> list[Note]:
    """Every note under `directory`, raising `RetrieverSkip` when there are none at all.

    A tree with zero parseable notes is a deployment fact (mis-pointed `knowledge_path`, unmounted
    volume), not a corpus answer. Filters that exclude everything are the legitimate empty answer
    and do not come through here.
    """
    notes = load_notes(directory) if directory.exists() else []
    if not notes:
        raise RetrieverSkip(f"no notes found under {directory}")
    return notes


def _in_window(note: Note, since: date | None, until: date | None) -> bool:
    """Whether the note falls inside the requested date window (no window = everything).

    An undated note fails a windowed query: it cannot be shown to fall in the period asked about.
    """
    if since is None and until is None:
        return True
    if note.valid_from is None:
        return False
    if since is not None and note.valid_from < since:
        return False
    return not (until is not None and note.valid_from > until)


async def _conflict_index(directory: Path, filters: dict[str, Any]) -> dict[str, NoteConflicts]:
    """Map each note id to what it is known or suspected to disagree with.

    Runs entirely in a worker thread; `kg.conflicts.conflict_index` caches by corpus fingerprint and
    `as_of`, so the note-backed retrievers of one sweep share one computation. `as_of` follows the
    sweep's date rule: today when unwindowed, `None` (whole corpus) when windowed, matching what
    `_eligible_sync` admits.
    """
    as_of = None if _is_windowed(filters) else date.today()
    return await asyncio.to_thread(conflict_index, directory, as_of)


class GraphRetriever:
    """Retrieve evidence from the Markdown knowledge graph. A `SourceRetriever`."""

    def __init__(self, notes_dir: str | None = None, name: str = "graph") -> None:
        """Read notes from the given directory, or the configured `knowledge_dir`.

        `name` is the data-source name this retriever is cited under, passed by the registry from
        the manifest.
        """
        self._dir = Path(notes_dir) if notes_dir is not None else settings.knowledge_path
        self.name = name

    async def retrieve(self, query: str, filters: dict[str, Any]) -> Hits:
        """Return chunks from notes matching every term of `query`, ranked best first.

        Case-insensitive over `kg.search.search_text`, the same haystack the indexes and
        `find_notes` read. Matching is per term: every term must be present; when nothing satisfies
        all of them the search widens to any term and coverage orders the result. This is a coarse
        candidate filter (`ester` matches `polyester`); relevance is judged downstream.
        """
        terms = query_terms(query)
        # Load, filter, score, rank and cut in one worker thread: each step is O(corpus) pure Python
        # and would otherwise stall the event loop shared by every concurrent turn.
        chosen, found = await asyncio.to_thread(
            _rank_by_terms, self._dir, filters, terms, date.today()
        )
        conflicts = await _conflict_index(self._dir, filters)
        # `found` is the pre-cut total, so a per-leg cut is reported rather than read as the corpus.
        return Hits(
            (
                _chunk_for(note, self.name, confidence, conflicts.get(note.id), terms)
                for _, _, confidence, note in chosen
            ),
            found=found,
        )


# BM25's saturation constant: bounds how much a repeated term is worth, so mentioning the query
# often cannot out-rank a note about it.
_TERM_SATURATION = 1.2


def _relevance(
    counts: dict[str, int], document_frequency: dict[str, int], population: int
) -> float:
    """A matched note's within-corpus relevance: saturating term frequency, weighted by rarity.

    BM25 without document-length normalisation, because a note's length tracks how much it records
    rather than padding. Confidence is a trust signal and only breaks ties between equally relevant
    notes.
    """
    return sum(
        math.log(1 + population / (1 + document_frequency[term]))
        * (count / (count + _TERM_SATURATION))
        for term, count in counts.items()
    )


# The filter keys `_eligible_notes` understands. Named here so "did the caller ask to narrow this?"
# is one check rather than four, and so a filter added to the gate is added in one place.
_NOTE_FILTERS = ("type", "tag", "since", "until")


@runtime_checkable
class ReactionMetadata(Protocol):
    """The one question this package asks of the ELN transcription tier.

    Declared as a Protocol because `ingest` depends on `retrieval`; importing `ReactionRecordStore`
    here would make a cycle.
    """

    async def eligible(self, reaction_ids: Sequence[str], filters: dict[str, Any]) -> set[str]:
        """Which of `reaction_ids` pass `filters` and are current."""
        ...

    async def structurally_withheld(self, refs: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        """Which of `refs` — `(ingest_source, reaction_id)` — no structure search may serve.

        Withdrawn by the source, or citation-only (a record naming a species without its structure).
        """
        ...


class FingerprintReactionRetriever:
    """Retrieve reactions structurally similar to a reaction-SMILES query. A `SourceRetriever`."""

    name = "reaction-fingerprint"

    def __init__(self, store: FingerprintStore, records: ReactionMetadata) -> None:
        """Search `store`, resolving a metadata filter against `records`.

        `records` is required: this package may not import the transcription tier, so the caller
        (`agent` or `durable`) supplies it. It is consulted only when a filter is given.
        """
        self._store = store
        self._records = records

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for reactions similar to `query` (a reaction SMILES), or none.

        A query that is not a valid reaction SMILES yields no evidence; only `FingerprintInputError`
        is caught, so an index that refuses the search (e.g. width mismatch) surfaces in the sweep's
        `sources_failed` instead of reading as "nothing similar". Each match cites its
        `reaction-<id>` record, written by the same call that indexed the fingerprint.

        `type`/`tag`/`since`/`until` narrow the result: the index knows nothing of metadata, so this
        searches deeper than the page, applies the records' eligibility gate, then truncates.
        Withdrawn and citation-only records are dropped on both paths via `structurally_withheld`,
        since an unfiltered sweep does not go through the eligibility gate.
        """
        wanted = {key: filters[key] for key in _NOTE_FILTERS if filters.get(key) is not None}
        page = settings.fingerprint_top_k
        try:
            # `.hits` only: a retriever's contract is evidence chunks, and an unbuilt index yields
            # none.
            matches = (
                await find_similar_reactions(
                    self._store, query, top_k=self._depth(page) if wanted else None
                )
            ).hits
        except FingerprintInputError:
            return []
        if wanted:
            matches = await self._eligible(matches, wanted, page)
        # On both paths: `eligible` answers a filter, this answers whether a hit may be served at
        # all.
        asked = [(match.source, match.id) for match in matches]
        withheld = await self._records.structurally_withheld(asked)
        matches = [match for match in matches if (match.source, match.id) not in withheld]
        return [
            EvidenceChunk(
                content=f"Similar reaction {match.label} (Tanimoto {match.similarity:.2f})",
                # Qualified by source: two sites behind one entry id are two hits.
                source_note_id=note_id_for_reaction(match.id, match.source),
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

        Bounded by `fingerprint_max_top_k`, so the over-fetch cannot bypass the per-query memory
        cap.
        """
        return min(page * settings.retrieval_filter_overfetch, settings.fingerprint_max_top_k)

    async def _eligible(
        self, matches: list[Match], wanted: dict[str, Any], page: int
    ) -> list[Match]:
        """Keep the neighbours whose record passes `wanted`, most similar first, cut to `page`.

        A match with no stored record is dropped: it cannot be shown to satisfy the filter. The gate
        is `records.eligible_reaction_ids`, the same type/tag/window rules the note-backed
        retrievers apply.
        """
        eligible = await self._records.eligible([match.id for match in matches], wanted)
        kept = [match for match in matches if match.id in eligible]
        if len(matches) >= self._depth(page) and len(kept) < page:
            # The deeper search was exhausted without filling a page, so more matches may lie
            # further down;
            # say so rather than return a short list that reads as complete.
            log.warning(
                "filtered reaction search returned %d of %d wanted hits after scanning the "
                "%d-neighbour limit; raise CHEMCLAW_RETRIEVAL_FILTER_OVERFETCH to look deeper",
                len(kept),
                page,
                len(matches),
            )
        return kept[:page]


def _chunk_for(
    note: Note,
    retriever_name: str,
    score: float,
    conflicts: NoteConflicts | None,
    terms: Sequence[str] = (),
) -> EvidenceChunk:
    """Build one evidence chunk from a note, carrying its provenance.

    One builder for every note-backed retriever, so provenance and `matched_terms` cannot differ
    between legs. `terms` windows the excerpt and fills `matched_terms` (by `term_coverage`); with
    no terms the field is `None`, meaning "not reported" rather than "nothing matched".
    """
    return EvidenceChunk(
        content=_excerpt(note.body, terms) or note.id,
        source_note_id=note.id,
        retriever=retriever_name,
        score=score,
        conflicts_with=conflicts.ids if conflicts else [],
        conflicts_total=conflicts.total if conflicts else 0,
        created_by=note.created_by,
        source=note.source or "",
        confidence=note.confidence,
        # A windowed sweep serves retired notes, so the chunk carries when the note stopped being
        # valid. `None` means its validity window is open.
        valid_to=note.valid_to,
        # `None`, not `[]`, when the caller offered no terms: "not reported" rather than
        # "nothing matched", the distinction `Hits.found` argues one class over.
        matched_terms=matched_terms(note, terms) if terms else None,
    )


def _chunks_from_hits(
    hits: list[IndexHit],
    notes: dict[str, Note],
    retriever_name: str,
    conflicts: dict[str, NoteConflicts] | None = None,
    terms: Sequence[str] = (),
) -> list[EvidenceChunk]:
    """Map index hits to cited evidence chunks, dropping any hit whose note no longer loads.

    The graph on disk is authoritative: a stale index row for a deleted note is dropped so a
    citation never dangles. The hit's score is kept, clamped to [0, 1] because `ts_rank` is
    unbounded.
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
                terms,
            )
        )
    return chunks


class VectorRetriever:
    """Retrieve notes by dense-embedding similarity to the query. A `SourceRetriever`.

    An entry point into the graph, not a replacement: it surfaces semantically related notes the
    agent then expands. The index defaults to the production one so a manifest can name this class
    directly.
    """

    def __init__(
        self,
        index: NoteIndex | None = None,
        notes_dir: str | None = None,
        name: str = "vector",
    ) -> None:
        """Search `index` (the production note index by default); excerpts from `notes_dir`.

        `name` is the data-source name, passed by the registry from the manifest.
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
        # Scope the index query to eligible notes so top-k slots are not spent on filtered-out
        # notes.
        hits = await self._index.search_dense(
            query_embedding, settings.retrieval_top_k, within=set(notes)
        )
        # The query's own terms even on the dense leg: a typed word found in the body is the part a
        # chemist can check; otherwise `_excerpt` falls back to the head.
        return _chunks_from_hits(
            hits, notes, self.name, await _conflict_index(self._dir, filters), query_terms(query)
        )


class LexicalRetriever:
    """Retrieve notes by full-text term match (Postgres FTS). A `SourceRetriever`.

    `ts_rank` over the GIN-indexed `tsvector` of the same notes `GraphRetriever` scans; an entry
    point into the graph, not a replacement. The matching rules differ from the graph leg's:
    Postgres stems and drops stop-words (`couplings` matches `coupled`), while
    `kg.search.term_coverage` matches substrings (`ester` matches `polyester`).
    """

    def __init__(
        self,
        index: NoteIndex | None = None,
        notes_dir: str | None = None,
        name: str = "lexical",
    ) -> None:
        """Search `index` (the production note index by default); excerpts from `notes_dir`.

        `name` is the data-source name, passed by the registry from the manifest.
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
        return _chunks_from_hits(
            hits, notes, self.name, await _conflict_index(self._dir, filters), query_terms(query)
        )
