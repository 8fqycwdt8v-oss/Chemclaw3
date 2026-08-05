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
from datetime import date

import networkx as nx
from pydantic import BaseModel, Field

from chemclaw.agent.authz import require_actor
from chemclaw.agent.framing import frame_untrusted
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_proposal
from chemclaw.ingest.eln.compound import compound_dependencies
from chemclaw.kg.analytics import GraphGaps, analyze
from chemclaw.kg.git_submitter import default_submitter
from chemclaw.kg.graph import build_graph, load_notes, neighborhood
from chemclaw.kg.note import Note, Relation
from chemclaw.kg.pr_gate import propose_note
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


class NoteView(BaseModel):
    """A note's body plus the notes within a few links of it (graph neighborhood)."""

    note: NoteRef
    body: str
    neighbors: list[NoteRef]


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


@tool
async def find_notes(text: str) -> list[NoteRef]:
    """Find notes whose id, tags, SMILES, or body contain every word of `text` (case-insensitive).

    Use this to locate an entry note before expanding its neighborhood.

    Args:
        text: One or more words to search for. Each word may match anywhere in the note
            (id, type, SMILES, tags, or body) independently — this is not a phrase search, so
            "Suzuki coupling solvent" matches a note containing all three words in any order or
            position, not only one containing that exact run of text.

    Returns:
        Matching note references (id + type + smiles + tags), body omitted. An empty result means
        no current note contains every word — it does not mean the topic is absent from the
        graph; a single differently-worded term (e.g. just "suzuki") may still find it.
    """
    graph = await asyncio.to_thread(build_graph, settings.knowledge_path)
    # The same tokenizer and the same haystack every other note search uses
    # (`chemclaw.kg.search`), so a note this tool finds is one `gather_evidence` can also cite.
    # A bare `text.lower().split()` here made "the biaryl" require the literal word "the".
    terms = query_terms(text)
    today = date.today()
    # A broad needle matches most of the corpus, and the whole hit list goes into the model's
    # context. Bound it like every other retrieval surface (`fingerprint_max_top_k`,
    # `retrieval_top_k`), and warn on truncation so it is never a silent cap (D-066 #4).
    cap = settings.graph_max_results
    matches = []
    for node_id in sorted(graph.nodes):
        note = graph.nodes[node_id].get("note")
        if note is None:
            continue
        # Discovery serves current evidence only: a not-yet-valid or expired note is not surfaced
        # as current fact (KM-7). It stays in Git and remains reachable by explicit id.
        if not note.is_current(today):
            continue
        if terms and term_coverage(note, terms) == len(terms):
            matches.append(_ref(note))
            if len(matches) == cap:
                log.warning(
                    "find_notes capped at %d matches (id order) for %r; "
                    "narrow the query or raise CHEMCLAW_GRAPH_MAX_RESULTS",
                    cap,
                    text,
                )
                break
    return matches


def _require_note(graph: nx.DiGraph, note_id: str) -> Note:
    """The note `note_id` names, or a `ChemclawError` saying why it could not be found.

    One message for both by-id lookups (`expand_note`, `record_failure`), because the cause a
    chemist most needs to hear is the same for both and easy to get wrong: an id that resolves to
    nothing is *usually* a citation to a note whose PR-gate submission has not been merged yet
    (D-018), which reads identically to a typo unless the message says so.
    """
    if note_id not in graph or graph.nodes[note_id].get("note") is None:
        raise ChemclawError(
            f"no note with id {note_id!r} — it may not exist, or it may be a citation to a "
            "reaction that has been indexed but whose note is still pending human review"
        )
    note: Note = graph.nodes[note_id]["note"]
    return note


@tool
async def expand_note(note_id: str, hops: int = 1) -> NoteView:
    """Return a note's body and the notes within `hops` links of it (1–2 typical).

    Retrieval is graph traversal, not vector similarity: neighbors are stated
    relations. Raises if the id is unknown.

    Args:
        note_id: The id of the entry note.
        hops: How many link steps to expand (1 or 2).

    Returns:
        The note's body plus its neighborhood as references.

    Raises:
        ChemclawError: When `note_id` names no current note. A `ChemclawError` is chemclaw's
            own always-safe "bad input" contract (`chemclaw.core.errors`), so
            `chemclaw.agent.tool_authz`
            surfaces this message to the model verbatim instead of MAF's opaque generic
            failure — the common real cause is a citation to a note still pending PR-gate
            review (D-018: a fingerprint-indexed reaction whose note has not yet been merged),
            which the chemist can otherwise not distinguish from a typo or a deleted note.
    """
    graph = await asyncio.to_thread(build_graph, settings.knowledge_path)
    note = _require_note(graph, note_id)
    # `hops` comes from the model; clamp it to [0, graph_max_hops] so a large value is bounded
    # rather than traversing the whole graph (SEC-4).
    hops = min(max(hops, 0), settings.graph_max_hops)
    today = date.today()
    # The anchor is an explicit by-id lookup, so it is returned even if expired; its neighbors are a
    # discovery sweep, so non-current ones are dropped from the current-evidence view (KM-7).
    neighbors = [
        _ref(graph.nodes[nid]["note"])
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
    # retirement as the single decision they are (STO-7's `dependencies`, used here for a note that
    # is amended rather than newly minted).
    retirement = (
        [close_refuted_note(refuted, note.id, held_until)] if held_until is not None else []
    )
    reference = await propose_note(note, default_submitter(), dependencies=retirement)
    record_proposal(note.id, reference)
    return reference
