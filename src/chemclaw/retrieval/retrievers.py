"""Concrete source retrievers — thin adapters over existing layers (plan step 5b.3).

Two real sources behind the one `SourceRetriever` contract, proving the harness core is
source-agnostic (a third — analytics, or external literature — is another adapter here, not a
core change): `GraphRetriever` reads the knowledge graph (Phase 2), `FingerprintReactionRetriever`
runs reaction-fingerprint search (Phase 3). Neither introduces a new store. Every chunk they
emit carries the id of the note it came from, so the harness can cite it (5b.2).
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
from chemclaw.kg.search import query_terms, term_frequencies
from chemclaw.retrieval.evidence import EvidenceChunk, RetrieverSkip
from chemclaw.retrieval.vector_index import IndexHit, NoteIndex, default_note_index
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions
from chemclaw.science.fingerprints.store import FingerprintInputError, FingerprintStore, Match

log = logging.getLogger(__name__)


def _excerpt(body: str, terms: Sequence[str] = ()) -> str:
    """A report-sized excerpt of a note body, windowed on the match, with wikilinks stripped.

    An excerpt must not carry a source note's `[[links]]` verbatim into the report body — that
    would add unintended (possibly dangling) graph edges — so `chemclaw.kg.note.strip_links` is
    applied here. It is applied *again* at the report, on every chunk rather than only on
    note-backed ones: the share and warehouse retrievers build their content from raw document
    text and never reach this function, so this was never the whole of that guarantee.

    **The window follows the match, because a reviewer has to see what the note was retrieved
    for.** A note matches on its whole searchable text — id, type, SMILES, tags and body — and the
    excerpt was a blind character prefix of the body, so a note whose answer is at the end read as
    an unexplained bullet plus a citation. Measured over the committed corpus for the query
    `yield`: 32 of 38 notes have bodies past the 240-character budget, and 6 of 16 returned chunks
    did not contain the term that matched. `campaign` and `optimization-campaign` are the worst
    case by construction, since their yields and outcomes are in a table at the end — the same
    failure `core/config/retrieval.py` articulates for `protocol_digest_max_chars`. In the
    conversational tools this is recoverable with `expand_note`; in `report_note` it is the final
    artifact a chemist signs at the PR-gate, and nothing there expands.

    `terms` are the query's terms (`kg.search.query_terms`) when the caller has them. With none, or
    with a match the head already covers, or with a match that is *not* in the body at all — the
    note was found by its id, its type, its tags or its structure — this is a plain prefix, which
    is what every excerpt was before and still is for the callers that pass nothing.
    """
    stripped = strip_links(body.strip())
    window = settings.note_excerpt_chars
    if len(stripped) <= window:
        return stripped
    start = _window_start(stripped, terms, window)
    if start == 0:
        return stripped[:window]
    # The leading marker is inside the budget rather than added to it: the cap is what a sweep's
    # `gather_evidence_max_chars` was measured against, and an excerpt that silently starts
    # mid-document reads as the note's opening line.
    return "…" + stripped[start : start + window - 1]


def _window_start(text: str, terms: Sequence[str], window: int) -> int:
    """Where to start the excerpt so the first matched term is inside it. `0` = the head.

    A third of the budget is spent on what came *before* the match, because a number with no
    sentence in front of it is a number a reader cannot place — and the sentence a chemist wants
    is the one the match is in, not the one after it. The start is pushed forward to the next word
    boundary so the excerpt does not open mid-word, which is how the pre-windowing excerpts ended
    (`in place of the cla`) and is no better at the other end.
    """
    lowered = text.casefold()
    offsets = [found for term in terms if (found := lowered.find(term.casefold())) >= 0]
    if not offsets:
        return 0
    first = min(offsets)
    if first < window:  # the head already shows it
        return 0
    start = min(first - window // 3, len(text) - window)
    space = text.find(" ", start)
    return start if space < 0 or space >= first else space + 1


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

    Deliberately not cached, though a three-source sweep runs it three times: the parse behind it
    is cached (`load_notes`), so the repeated work is this filter loop — a few milliseconds per
    leg at 10⁴ notes — and a cache over it would need the corpus fingerprint, the filter dict
    *and* the current date in its key (`is_current(today)` makes the answer date-sensitive).
    Three cheap loops beat one cache with a three-part invalidation story.

    **The filter runs in the worker thread with the load, and that is the whole of this
    function.** It used to be a `for` loop over the offloaded result, i.e. on the event loop that
    serves every other concurrent turn on the pod — "a few milliseconds per leg" is true of a
    small corpus and false of the one this is sized for, and the loop it stalls is shared by every
    user the pod is serving. `_eligible_sync` says what that measured.
    """
    return await asyncio.to_thread(_eligible_sync, directory, filters, date.today())


def _eligible_sync(directory: Path, filters: dict[str, Any], today: date) -> dict[str, Note]:
    """The synchronous body of `_eligible_notes`: load, then filter, in one worker thread.

    Split out rather than inlined into a lambda so `GraphRetriever` can reuse it *inside its own
    single thread hop* — its scoring loop needs the same notes, and two hops would put the
    hand-back between them on the event loop again.

    `today` is passed in rather than read here so that every note in one sweep is judged current
    against the same date, and so a test can drive the currency rule without moving the clock.
    """
    want_type = filters.get("type")
    want_tag = filters.get("tag")
    since = filters.get("since")
    until = filters.get("until")
    notes: dict[str, Note] = {}
    # The directory check goes into the worker thread with the load, rather than being a `stat` on
    # the event loop before it. It is a small syscall, but this runs per retriever per query on the
    # loop that serves every other concurrent turn, and the reason the load below is offloaded
    # applies to it unchanged.
    for note in _load_if_present(directory):
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


def _rank_by_terms(
    directory: Path, filters: dict[str, Any], terms: Sequence[str], today: date
) -> list[tuple[int, float, float, Note]]:
    """`GraphRetriever`'s whole search, synchronously: eligible notes, scored, ranked and cut.

    One function rather than three awaits, because each hand-back between them lands on the event
    loop — see `GraphRetriever.retrieve` for the measurement, and `_eligible_sync` for why the
    filter half moved first. The scoring loop is here rather than in the coroutine for the same
    reason it was written: `term_frequencies` rebuilds each note's searchable text and lowercases
    its body, which is O(corpus) pure Python and belongs in the worker thread with the load.

    Returns:
        `(coverage, relevance, confidence, note)` for the best `retrieval_top_k` matches, best
        first.
    """
    frequencies: list[tuple[dict[str, int], Note]] = []
    for note in _eligible_sync(directory, filters, today).values():
        counts = term_frequencies(note, terms)
        if counts:
            frequencies.append((counts, note))
    # Document frequency over the *matched* population, which is the same number as over the
    # eligible one: a term's df counts the notes containing it, and a note containing it
    # matched. So the rarer term in a two-term query earns the larger weight without a second
    # pass over the corpus.
    population = len(frequencies)
    document_frequency: dict[str, int] = {}
    for counts, _ in frequencies:
        for term in counts:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    scored: list[tuple[int, float, float, Note]] = []
    for counts, note in frequencies:
        # Confidence is a *trust* signal (KM-5), so it decides which of two equally relevant
        # notes survives truncation — never relevance itself; see `_relevance`. A note with no
        # confidence takes the configured neutral default.
        confidence = (
            note.confidence
            if note.confidence is not None
            else settings.retrieval_default_confidence
        )
        scored.append(
            (len(counts), _relevance(counts, document_frequency, population), confidence, note)
        )
    complete = [entry for entry in scored if entry[0] == len(terms)]
    # RRF reads each source's list as ranked best-first, so the list must be ordered by this
    # retriever's own relevance signal — disk order is not a ranking. Coverage leads only on
    # the widened search (on the complete one it is the same for every hit, so this reduces
    # to confidence exactly as before). Note id breaks ties deterministically.
    #
    # Ranked *then* cut *then* materialized, bounded by the `retrieval_top_k` every sibling
    # leg honours. This was the one unbounded retriever: a broad query on a large corpus
    # built an `EvidenceChunk` (an excerpt scan plus a conflicts lookup each) for every
    # scored note before the merge budget threw all but a handful away — and the graph leg
    # then arrived at the round-robin with thousands of entries against every other leg's k,
    # which is the crowding-out asymmetry D-2026-08-01 was written about, relocated from the
    # cut to the input.
    return sorted(
        complete or scored,
        key=lambda entry: (-entry[0], -entry[1], -entry[2], entry[3].id),
    )[: settings.retrieval_top_k]


def _load_if_present(directory: Path) -> list[Note]:
    """Every note under `directory`, raising `RetrieverSkip` when there are none at all.

    A tree with zero parseable notes is a deployment fact, not a corpus answer: a mis-pointed
    `knowledge_path` (or an unmounted volume) used to zero the graph, dense and lexical legs at
    once with nothing anywhere saying so — three sources reporting "found nothing" about a corpus
    they never saw. Filters that exclude everything are the legitimate empty answer and do not
    come through here.
    """
    notes = load_notes(directory) if directory.exists() else []
    if not notes:
        raise RetrieverSkip(f"no notes found under {directory}")
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
        # Load, filter, score, rank and cut in **one** worker thread. Every one of those steps is
        # O(corpus) pure Python — `term_frequencies` alone rebuilds each note's searchable text and
        # lowercases its body — and every one of them used to run on the event loop that serves
        # every concurrent turn, SSE stream and probe on the pod. Measured on the sibling path
        # (`agent/graph_tools.py::find_notes`, the same scan over the same corpus) with a 5 ms
        # heartbeat over 10 000 notes: p50 loop lag 418 ms at eight concurrent calls, against
        # 26 ms once the scan moved. It buys latency and jitter, not throughput — the scan holds
        # the GIL either way.
        chosen = await asyncio.to_thread(_rank_by_terms, self._dir, filters, terms, date.today())
        conflicts = await _conflict_index(self._dir)
        return [
            _chunk_for(note, self.name, confidence, conflicts.get(note.id), terms)
            for _, _, confidence, note in chosen
        ]


# BM25's saturation constant, in its usual range. Named rather than inlined because it is the one
# number deciding how much a repeated term is worth: at 1.2 a term occurring twice scores 0.63 of a
# term occurring endlessly, and a note that merely *mentions* the query cannot out-rank one about it
# by repetition alone.
_TERM_SATURATION = 1.2


def _relevance(
    counts: dict[str, int], document_frequency: dict[str, int], population: int
) -> float:
    """A matched note's within-corpus relevance: saturating term frequency, weighted by rarity.

    **Why this exists at all.** The graph leg used to rank its hits by `confidence` and break ties
    on the note id, and confidence is a *trust* signal rather than a relevance one — so a
    well-trusted note that mentions the query once outranked a note about it, and any tie fell
    through to alphabetical order. Measured on 5,000 notes that all matched every term, the leg
    returned `reaction-00000`, `-00001`, `-00002`: the first eight ids in the corpus, which is not
    a ranking. On the shipped 38-note corpus confidence takes only ten distinct values and 18 notes
    share one of two, so the alphabetical fall-through is the common case rather than the edge.

    Trust has not been discarded, it has been demoted to what it can honestly decide: among notes
    of *equal relevance*, the more-trusted one still survives the cut first (KM-5's intent), rather
    than deciding relevance itself.

    This is BM25 without the document-length normalisation — deliberately, because `search_text` is
    a note's whole metadata-plus-body haystack and its length tracks how much a note *records*
    rather than how padded it is; penalising a thorough campaign note for being thorough is the
    wrong correction here. The saturation term is what bounds repetition instead.
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

    Declared here rather than imported from `chemclaw.ingest.eln.records`, and that is a layering
    fact rather than a style preference: `ingest` depends on `retrieval`, so importing back would
    make a cycle out of what is really a one-method need. A Protocol is structural, so the concrete
    `ReactionRecordStore` satisfies this without either package knowing about the other — and the
    retriever ends up declaring what it needs instead of where it comes from.
    """

    async def eligible(self, reaction_ids: Sequence[str], filters: dict[str, Any]) -> set[str]:
        """Which of `reaction_ids` pass `filters` and are current."""
        ...


class FingerprintReactionRetriever:
    """Retrieve reactions structurally similar to a reaction-SMILES query. A `SourceRetriever`."""

    name = "reaction-fingerprint"

    def __init__(self, store: FingerprintStore, records: ReactionMetadata) -> None:
        """Search `store`, resolving a metadata filter against `records`.

        Both are injected, and `records` is **required** rather than defaulted: this package may not
        import the transcription tier (see `ReactionMetadata`), and a default that reached for the
        production store would be that import wearing a different hat. The caller — `agent` or
        `durable`, both of which may depend on `ingest` — supplies it.

        The metadata lookup only happens when a filter is actually given. This took a `notes_dir`
        while reactions were markdown notes on disk; they are rows now (D-2026-08-25).
        """
        self._store = store
        self._records = records

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for reactions similar to `query` (a reaction SMILES), or none.

        A query that is not a valid reaction SMILES yields no evidence (not an error) — each
        retriever answers only what its source can, so prose queries simply return empty here.
        **Only that**, which is why the catch below names `FingerprintInputError` rather than its
        parent: `FingerprintError` also covers the index refusing to be searched (`cannot compare
        fingerprints of different widths` — an index built under different parameters than the
        query), and returning `[]` for that told a chemist the corpus holds no similar reaction.
        The sweep's `sources_failed` channel is where that belongs, and `fanout._sweep` puts it
        there the moment this stops swallowing it — the same correction the share, warehouse and
        vendored halves already took.
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
        except FingerprintInputError:
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
        """Keep the neighbours whose record passes `wanted`, most similar first, cut to `page`.

        A match with no stored record is dropped here, and deliberately: a filter says "only
        records that are X", and a record nobody can read cannot be shown to be X, so serving it
        would answer a narrowed question with an unnarrowed hit. An unfiltered sweep never reaches
        this and still surfaces every structural hit the index holds.

        The gate itself is `records.eligible_reaction_ids`, which applies the same type/tag/window
        rules every note-backed retriever applies — against columns rather than parsed frontmatter,
        and over this page of candidates rather than the whole corpus.
        """
        eligible = await self._records.eligible([match.id for match in matches], wanted)
        kept = [match for match in matches if match.id in eligible]
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
    note: Note,
    retriever_name: str,
    score: float,
    conflicts: NoteConflicts | None,
    terms: Sequence[str] = (),
) -> EvidenceChunk:
    """Build one evidence chunk from a note, carrying its provenance (D-160).

    One builder for every note-backed retriever, so provenance cannot be attached on the graph
    path and forgotten on the index path — which is precisely the drift that would produce a
    partially-provenanced evidence list, the worst of the three possible states. `terms` is what
    lets the excerpt window on the match rather than on the head of the body; a caller with no
    terms to offer gets the prefix, which is the honest fallback (see `_excerpt`).
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
    )


def _chunks_from_hits(
    hits: list[IndexHit],
    notes: dict[str, Note],
    retriever_name: str,
    conflicts: dict[str, NoteConflicts] | None = None,
    terms: Sequence[str] = (),
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
                terms,
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
        # The query's own terms, even on the dense leg: this note was surfaced by meaning, but if
        # a word the chemist typed is *in* the body then that is the part of it they can check.
        # Where none is, `_excerpt` falls back to the head exactly as before.
        return _chunks_from_hits(
            hits, notes, self.name, await _conflict_index(self._dir), query_terms(query)
        )


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
        return _chunks_from_hits(
            hits, notes, self.name, await _conflict_index(self._dir), query_terms(query)
        )
