"""Agent tools for the knowledge graph (plan steps 2.5, 2.6).

Read tools (`find_notes`, `expand_note`) let the agent retrieve by graph traversal
— the capability behind the `knowledge-graph-query` skill. The write tools route an
agent-authored note through the PR-gate for human review (D-005), never straight to the graph:
`propose_knowledge_note` for new knowledge, `record_failure` for the case that had no path at all
— knowledge the graph already holds turning out to be wrong. Graph building is file I/O, so
it runs off the event loop.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import networkx as nx
from pydantic import BaseModel, Field

from chemclaw.agent.authz import require_actor
from chemclaw.agent.framing import frame_untrusted
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_proposal
from chemclaw.ingest.eln.compound import compound_dependencies
from chemclaw.ingest.eln.records import RECORD_TYPE, default_record_store
from chemclaw.kg.analytics import GraphGaps, analyze
from chemclaw.kg.git_submitter import default_submitter
from chemclaw.kg.graph import build_graph, load_notes, neighborhood, note_in
from chemclaw.kg.note import Note, Relation, external_record_id, resolves_outside_graph
from chemclaw.kg.pr_gate import propose_note
from chemclaw.kg.relations import DEFAULT_RELATION
from chemclaw.kg.search import query_terms, term_coverage
from chemclaw.memory.failure import close_refuted_note, failure_note

log = logging.getLogger(__name__)


class NoteRef(BaseModel):
    """A lightweight reference to a note (no body), for listing and neighbors.

    Provenance is surfaced here (KM-6) so the agent can weigh a source — who authored it
    (`created_by`), where it came from (`source`), how sure it is (`confidence`), and its validity
    window — without a second lookup. Fields default so a bare reference is still constructible.
    """

    id: str
    type: str
    compound_smiles: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_by: str = "human"
    source: str | None = None
    confidence: float | None = None
    valid_from: date | None = None
    valid_to: date | None = None


class NeighborRef(NoteRef):
    """A neighbouring note, plus the typed edges that connect it to the note being expanded.

    **Direction is kept, and that is the whole point of two fields rather than one.** "A supersedes
    B" and "B supersedes A" are opposite claims about which note is the current answer, and
    `contradicts`, `precursor-of` and `computed-from` are the same shape. Flattening them into one
    list of relation names would hand the model a set of edges it could read either way round.

    Both lists are empty for a neighbour that is not *directly* linked — at `hops=2` most are not —
    and for one linked only by a bare `[[wikilink]]`, whose relation is `cites` and which carries no
    claim worth reporting. An empty pair therefore means "adjacent in the neighbourhood, nothing
    asserted about how", which is exactly what the untyped view said before D-134 gave edges types
    and nothing read them.
    """

    # Asserted by the expanded note *about* this neighbour, and by this neighbour about it. Sorted
    # and deduplicated: an edge carries a tuple of `Relation`s (see `kg.graph._assemble_graph`), and
    # what a reader needs here is which relations hold, not how many times each was written.
    relations_out: list[str] = Field(default_factory=list)
    relations_in: list[str] = Field(default_factory=list)


class NoteView(BaseModel):
    """A note's body plus the notes within a few links of it (graph neighborhood)."""

    note: NoteRef
    body: str
    neighbors: list[NeighborRef]


def _ref(note: Note) -> NoteRef:
    return NoteRef(
        id=note.id,
        type=note.type,
        compound_smiles=note.compound_smiles,
        tags=note.tags,
        created_by=note.created_by,
        source=note.source,
        confidence=note.confidence,
        valid_from=note.valid_from,
        valid_to=note.valid_to,
    )


class NoteSearch(BaseModel):
    """What `find_notes` found — and what a short list actually means.

    The bare `list[NoteRef]` this replaced had both of `EvidenceSweep`'s silences: a capped list
    was byte-identical to a small corpus (the warning went to a log no model reads — the exact
    defect `truncated_by`/`total_before_cap` were introduced to fix in the sibling tool the
    system prompt chains this one with), and a query no note fully matched returned `[]` even
    when the sweep's graph leg would have widened to partial matches, so the two tools answered
    one question differently.
    """

    matches: list[NoteRef] = Field(default_factory=list)
    # How many notes matched before the cap; equal to `len(matches)` when nothing was cut.
    total_matches: int = 0
    # True when no note contained every term and the matches are partial-coverage hits instead,
    # best coverage first — the same fallback `GraphRetriever` applies to the same corpus.
    widened: bool = False


def _scan_notes(notes_dir: Path, terms: Sequence[str], today: date, cap: int) -> NoteSearch:
    """Search every current note under `notes_dir` for `terms`, ranked and capped.

    The whole answer, not just the scored pairs: ranking and truncation are O(N) over the matches
    too, and on a broad query the matches *are* the corpus — measured, leaving only those two steps
    behind still cost 621 ms of loop stall at eight concurrent calls over 10 000 notes. A partial
    offload is the shape of the defect, not the fix.

    **Synchronous on purpose, and it is the whole reason this function exists.** The load was
    already offloaded and the O(N) scan over its result was not, so `term_coverage` — which
    rebuilds each note's searchable text, `model_dump()`s its conditions and lowercases the body —
    ran on the event loop that serves every other concurrent turn on the pod. Measured on a
    10 000-note corpus with a 5 ms heartbeat: one `find_notes` stalled the loop for 151 ms and
    eight concurrent ones for 836 ms, against an idle p50 of 0.18 ms. During that stall no SSE
    token is flushed to any user on the pod, no `/healthz` is answered and no bearer token is
    validated.

    It buys latency and jitter, **not throughput**: the scan is pure Python holding the GIL, so a
    thread pool runs eight of these no faster than one (measured elsewhere in this review at 0.91x
    on four threads). The corpus is markdown in Git rather than rows in Postgres, so there is no
    database to push the scan into either.

    `load_notes`, not `build_graph`: this is a substring sweep over each note's own metadata and
    body, and it never follows an edge. Assembling the graph made a cold call pay node and edge
    insertion for a traversal it does not do, and made the sweep iterate dangling link targets —
    nodes with no note behind them, skipped one line later. Both caches sit behind the same stat
    fingerprint, so a warm call is unchanged.

    Args:
        notes_dir: The corpus root, resolved by the caller so this stays testable with a fixture.
        terms: The query's tokens, already normalised by `query_terms`.
        today: The date `is_current` is judged against — passed in rather than read here so one
            scan cannot straddle midnight.
        cap: How many references the answer may carry, `graph_max_results` from the caller.

    Returns:
        The search result, with `total_matches` counting the hits before the cap.
    """
    scored: list[tuple[int, NoteRef]] = []
    for note in sorted(load_notes(notes_dir), key=lambda candidate: candidate.id):
        # Discovery serves current evidence only: a not-yet-valid or expired note is not surfaced
        # as current fact (KM-7). It stays in Git and remains reachable by explicit id.
        if not note.is_current(today):
            continue
        coverage = term_coverage(note, terms)
        if coverage:
            scored.append((coverage, _ref(note)))
    complete = [pair for pair in scored if pair[0] == len(terms)]
    widened = not complete and bool(scored)
    # Widened results rank by coverage (a note matching three of four terms beats one matching
    # one), complete ones stay in id order — coverage is identical across them, and a stable
    # order is what makes two identical queries return identical lists.
    chosen = sorted(scored, key=lambda pair: (-pair[0], pair[1].id)) if widened else complete
    # A broad needle matches most of the corpus, and the whole hit list goes into the model's
    # context. Bound it like every other retrieval surface (`fingerprint_max_top_k`,
    # `retrieval_top_k`) — and the cut is *declared* in the return value, because the log line
    # this used to warn into is one no model reads, and a capped list with no marker reads as the
    # whole corpus (D-066 #4).
    return NoteSearch(
        matches=[ref for _, ref in chosen[:cap]],
        total_matches=len(chosen),
        widened=widened,
    )


@tool
async def find_notes(text: str) -> NoteSearch:
    """Find notes whose id, tags, SMILES, or body contain every word of `text` (case-insensitive).

    Use this to locate an entry note before expanding its neighborhood.

    Args:
        text: One or more words to search for. Each word may match anywhere in the note
            (id, type, SMILES, tags, or body) independently — this is not a phrase search, so
            "Suzuki coupling solvent" matches a note containing all three words in any order or
            position, not only one containing that exact run of text.

    Returns:
        The matching note references (id + type + smiles + tags, body omitted) with
        `total_matches` saying how many there were before the cap, and `widened` marking a
        result of partial-coverage hits when no current note contained every word. An empty
        result means not even one term matched — it does not mean the topic is absent from the
        graph; a differently-worded term may still find it.
    """
    # The same tokenizer and the same haystack every other note search uses
    # (`chemclaw.kg.search`), so a note this tool finds is one `gather_evidence` can also cite.
    # A bare `text.lower().split()` here made "the biaryl" require the literal word "the".
    terms = query_terms(text)
    if not terms:
        return NoteSearch()
    # Every step of the search runs in the worker thread, including the ranking and the cut: see
    # `_scan_notes` for the measurement that says why a partial offload is not one.
    return await asyncio.to_thread(
        _scan_notes, settings.knowledge_path, terms, date.today(), settings.graph_max_results
    )


def _edge_relations(graph: nx.DiGraph, source: str, target: str) -> list[str]:
    """The relation names asserted on the `source -> target` edge, sorted; empty if there is none.

    Reads the `relations` attribute `chemclaw.kg.graph._assemble_graph` puts on every edge, which
    until now no caller in this repository read at all — `graph.related` is the only other reader
    and nothing calls it (see `kg/README.md`). A bare `[[wikilink]]` yields `cites`, which is
    filtered out by `_neighbor_ref` rather than here: this function answers what the graph says,
    and what is worth reporting is the caller's question.
    """
    if not graph.has_edge(source, target):
        return []
    return sorted({relation.rel for relation in graph[source][target].get("relations", ())})


def _neighbor_ref(graph: nx.DiGraph, anchor_id: str, note: Note) -> NeighborRef:
    """One neighbour of `anchor_id`, carrying the typed edges between the two.

    `cites` is dropped from both directions deliberately. It is `relations.DEFAULT_RELATION` — what
    every untyped `[[wikilink]]` in the corpus already means — so reporting it would put the word
    "cites" on the majority of neighbours while saying nothing the neighbourhood itself does not
    already say. What survives is the set of edges an author typed *on purpose*, which is the set
    a reader has to weigh: a `contradicts` neighbour is not the same evidence as an `analogue-of`
    one, and before this the two arrived indistinguishable.
    """
    return NeighborRef(
        **_ref(note).model_dump(),
        relations_out=[
            rel for rel in _edge_relations(graph, anchor_id, note.id) if rel != DEFAULT_RELATION
        ],
        relations_in=[
            rel for rel in _edge_relations(graph, note.id, anchor_id) if rel != DEFAULT_RELATION
        ],
    )


def _require_note(graph: nx.DiGraph, note_id: str) -> Note:
    """The note `note_id` names, or a `ChemclawError` saying why it could not be found.

    One message for both by-id lookups (`expand_note`, `record_failure`). It used to name the
    PR-gate as the likely cause, because a citation to an indexed reaction whose note nobody had
    merged read identically to a typo (D-018). That cause is gone — a reaction record is readable
    the moment it is ingested — so the message no longer offers an explanation that is now wrong.
    """
    if note_id not in graph or graph.nodes[note_id].get("note") is None:
        raise ChemclawError(f"no note with id {note_id!r}")
    note: Note = graph.nodes[note_id]["note"]
    return note


async def _expand_record(note_id: str) -> NoteView:
    """Expand a `reaction-<id>` citation from the transcription store (D-2026-08-25).

    The ELN half of `expand_note`. A record is data rather than a claim, so what comes back is the
    transcription and an empty neighbourhood — not "we found nothing linked", but "a transcription
    links to nothing by construction". What *is* asserted about these runs lives in the campaigns
    and playbooks that cite them, and those are ordinary graph notes reached the ordinary way.

    `created_by` is reported as `agent` because a program rendered the file, which is what that
    field has always meant; it no longer implies anything is waiting for review.
    """
    record = await default_record_store().read(external_record_id(note_id))
    if record is None:
        raise ChemclawError(f"no reaction record with id {note_id!r}")
    return NoteView(
        note=NoteRef(
            id=note_id,
            type=RECORD_TYPE,
            compound_smiles=record.compound_smiles,
            tags=[record.project] if record.project else [],
            created_by="agent",
            source=record.source,
            confidence=None,
            valid_from=record.performed_at,
            valid_to=None,
        ),
        # Source text a chemist typed into an ELN, so it is framed as data for the same reason a
        # note body is: it reaches the model verbatim and must not be read as instruction.
        body=frame_untrusted(record.body, note_id=note_id),
        neighbors=[],
    )


@tool
async def expand_note(note_id: str, hops: int = 1) -> NoteView:
    """Return a note's body and the notes within `hops` links of it (1–2 typical).

    Retrieval is graph traversal, not vector similarity: neighbors are stated
    relations. Raises if the id is unknown.

    Each directly-linked neighbor carries the *typed* edges between it and this note, in
    `relations_out` (what this note asserts about the neighbor) and `relations_in` (what the
    neighbor asserts about this note) — so a `contradicts` or `supersedes` neighbor is legible as
    one, in the right direction, rather than arriving as an ordinary link. Untyped `[[wikilinks]]`
    and neighbors reached in two hops carry no relations, which is what "nothing is asserted about
    how these are connected" looks like.

    A `reaction-<id>` citation the graph does not hold resolves against the transcription store
    instead (D-2026-08-25), so a structure-search hit expands into its recipe — conditions, the
    charge sheet, the impurity profile, the procedure. It has no neighbourhood: it asserts
    nothing and
    therefore links to nothing. This is also what retires D-018's failure mode, where the same
    citation raised "no note with that id" for as long as nobody merged its pull request, and a
    chemist could not tell that from a typo.

    Args:
        note_id: The id of the entry note.
        hops: How many link steps to expand (1 or 2).

    Returns:
        The note's body plus its neighborhood as references, each with the relations that link it
        to this note.

    Raises:
        ChemclawError: When `note_id` names no current note. A `ChemclawError` is chemclaw's
            own always-safe "bad input" contract (`chemclaw.core.errors`), so
            `chemclaw.agent.tool_authz` surfaces this message to the model verbatim instead of an
            opaque generic failure.
    """
    graph = await asyncio.to_thread(build_graph, settings.knowledge_path)
    # The graph first, the store second, and in that order deliberately: `reaction-` is a *prefix*,
    # not a reservation, so a human-authored note under that name must still win. Store-first made
    # every such note unreachable — silently, because the record lookup fails with its own message.
    #
    # **Whether the graph holds a *note*, not whether it holds the id** — `note_in` says why those
    # differ, and this line testing membership instead was the defect it now prevents.
    if note_in(graph, note_id) is None and resolves_outside_graph(note_id):
        return await _expand_record(note_id)
    note = _require_note(graph, note_id)
    # `hops` comes from the model; clamp it to [0, graph_max_hops] so a large value is bounded
    # rather than traversing the whole graph (SEC-4).
    hops = min(max(hops, 0), settings.graph_max_hops)
    today = date.today()
    # The anchor is an explicit by-id lookup, so it is returned even if expired; its neighbors are a
    # discovery sweep, so non-current ones are dropped from the current-evidence view (KM-7).
    neighbors = [
        _neighbor_ref(graph, note_id, graph.nodes[nid]["note"])
        for nid in sorted(neighborhood(graph, note_id, hops=hops))
        if graph.nodes[nid].get("note") is not None and graph.nodes[nid]["note"].is_current(today)
    ]
    # The body is note content (possibly ingested, not agent-authored): frame it as data.
    return NoteView(
        note=_ref(note), body=frame_untrusted(note.body, note_id=note.id), neighbors=neighbors
    )


@tool
async def find_knowledge_gaps() -> GraphGaps:
    """Report where the knowledge graph is thin, unreachable, or load-bearing (gap KNW-5).

    Use this for "what don't we know?" questions — which area has the least evidence, which topic
    has runs but no distilled playbook, which notes nothing links to. Ordinary retrieval walks
    *outward from a hit*, so it can only ever answer "what do we know about X"; this is the
    complement, and it is the right input to a "what should we run next?" conversation.

    The undistilled counts are over **note tags**, which are topics (`suzuki`, `solvent`), not
    projects — there is no project field on a note. Reporting them as projects is how a live run
    came to state a confident portfolio status the record could not support.

    Returns:
        Counts per note type, isolated (unlinked) notes, tags with evidence but no distillation,
        the most-cited hub notes, and any dangling links in the served graph.
    """
    directory = settings.knowledge_path
    graph = await asyncio.to_thread(build_graph, directory)
    notes = await asyncio.to_thread(load_notes, directory)
    return analyze(graph, notes)


@tool
async def propose_knowledge_note(
    id: str,
    type: str,
    body: str,
    compound_smiles: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    confidence: float | None = None,
    calc_refs: list[str] | None = None,
    artifact_refs: list[str] | None = None,
    relations: list[Relation] | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
) -> str:
    """Propose a new knowledge-graph note for human review via the PR-gate.

    The note is authored as `agent`, so it lands on a feature branch as a PR and a
    human must approve it before it becomes trusted knowledge. Relate it to other
    notes with `[[wikilinks]]` in the body.

    Args:
        id: Stable, unique, human-readable note id (e.g. "reaction-suzuki-x").
        type: Note kind (compound, reaction, job-result, campaign, playbook, …).
        body: Markdown body, including `[[wikilinks]]` to related notes.
        compound_smiles: The molecule this note is about, if any.
        tags: Optional tags.
        source: Where the content came from (experiment id, calculation, …).
        confidence: How much this note should be trusted, 0–1. Set it when you have a
            principled basis (a calculator's calibration, the completeness of a record).
            **Leave it unset when you do not** — an absent confidence means "not stated",
            which retrieval and conflict detection both read correctly; a guessed number
            is read as evidence.
        calc_refs: Calculation keys this note rests on, so a stale calculation can be traced
            to the conclusions drawn from it. Get them from a job's result envelope.
        artifact_refs: Stored artifacts this note cites, as `<calc key>#<name>`.
        relations: Typed links to other notes — `contradicts`, `supersedes`, `follows` — each
            with its own optional confidence and validity window. Use these rather than prose
            when the relationship is the claim: a `contradicts` is what conflict detection reads.
        valid_from: When this became true (an experiment's own date, not today's).
        valid_to: When it stopped being true, if it has. Leave open otherwise — a result does
            not expire on its own, it is superseded.

    Returns:
        The submitted PR reference.
    """
    note = Note(
        id=id,
        type=type,
        body=body,
        compound_smiles=compound_smiles,
        tags=tags or [],
        source=source,
        confidence=confidence,
        calc_refs=calc_refs or [],
        artifact_refs=artifact_refs or [],
        relations=relations or [],
        valid_from=valid_from,
        valid_to=valid_to,
        created_by="agent",
    )
    # A compound note the agent linked is minted into the same PR (STO-7), so the agent can cite
    # the molecule it is writing about without first checking whether that note exists.
    reference = await propose_note(
        note, default_submitter(), dependencies=compound_dependencies(note)
    )
    # Surface the opened branch on the turn's stream (gap RCH-4) — see `core.turn_signals`.
    record_proposal(note.id, reference)
    return reference


@tool
async def record_failure(
    refutes: str,
    what_happened: str,
    compound_smiles: str | None = None,
    confidence: float | None = None,
    held_until: date | None = None,
) -> str:
    """Record that something the knowledge graph says did **not** hold in practice (PR-gated).

    Call this when a chemist reports that a note is wrong, misfired, or no longer matches the
    lab — the counterpart to `record_confirmed_answer`, which can only capture an answer that was
    *confirmed*. It writes a `failure-mode` note carrying a `contradicts` edge back to the note it
    refutes, so conflict detection finds it and every later retrieval of that note arrives marked
    as disputed instead of reading as settled fact.

    The edge is `contradicts` and never `supersedes`: a failure report says the old claim is wrong,
    not that this note is the new right answer — it does not contain one. When you *do* know the
    replacement, write it with `propose_knowledge_note` and give it a `supersedes` relation.

    Args:
        refutes: The id of the note that did not hold. It must already be in the graph; find it
            with `find_notes` or `expand_note` first, and never guess one.
        what_happened: What was actually observed, in the chemist's own terms. This text is the
            entire value of a negative result — do not summarize it away, and never invent it.
        compound_smiles: The molecule involved, when there is one.
        confidence: How sure the reporter is, 0–1. One bad run is not a refutation of a general
            rule; leave it unset unless the chemist indicated how firm the finding is.
        held_until: Set this **only** when the chemist says the old claim *used to be true and
            stopped* — pass the last date it held, and the refuted note is retired in the same
            review so it stops being served as current. Leave it unset when the claim is simply
            wrong: `held_until` records that the claim was valid up to that date, which for a
            never-true claim would be a new false statement, and the `contradicts` edge already
            keeps the disputed note visible and marked.

    Returns:
        The submitted PR reference. Nothing changes in the graph until a human merges it.

    Raises:
        ChemclawError: When `refutes` names no note, when `held_until` predates that note's own
            `valid_from`, or when the note has already been retired on some other date.
    """
    graph = await asyncio.to_thread(build_graph, settings.knowledge_path)
    refuted = _require_note(graph, refutes)
    # The reporter is the turn's authenticated user, not a parameter: attribution the model can
    # fill in is attribution that can name someone who said nothing. It is the same `require_actor`
    # rule every other user-triggered write here follows (F4-T3).
    note = failure_note(
        refutes,
        what_happened,
        reported_by=require_actor(),
        compound_smiles=compound_smiles,
        confidence=confidence,
    )
    # An already-retired note is refused rather than quietly left alone. Re-closing it would
    # extend its validity or append the marker twice, so it must not happen — but `held_until`
    # came from a person, and dropping a date somebody supplied is the same silent-correction
    # failure `close_refuted_note` refuses when the window ends before it starts. Say which date
    # already holds, and let them decide.
    if held_until is not None and refuted.valid_to is not None:
        raise ChemclawError(
            f"{refutes} was already retired on {refuted.valid_to.isoformat()}, so it cannot also "
            f"be retired on {held_until.isoformat()} — file the refutation without `held_until`, "
            "or correct the existing date first"
        )
    # Both files ride in one submission, so the reviewer signs off on the refutation and the
    # retirement as the single decision they are. `superseded`, NOT `dependencies`: a dependency is
    # written only where the base branch has no copy (`NoteFile.overwrite=False`), and the refuted
    # note always exists on base — `_require_note` just found it in the merged graph — so passing
    # the retirement as a dependency silently dropped it every time, leaving the refuted claim with
    # its validity window intact and still served as current evidence. `superseded` overwrites the
    # note in place, which is what retiring it means.
    retirement = (
        [close_refuted_note(refuted, note.id, held_until)] if held_until is not None else []
    )
    reference = await propose_note(note, default_submitter(), superseded=retirement)
    record_proposal(note.id, reference)
    return reference
