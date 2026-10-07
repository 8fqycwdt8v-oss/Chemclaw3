"""Agent tools for the knowledge graph.

`find_notes` and `expand_note` read by graph traversal. `record_knowledge_note` writes new
agent-authored knowledge directly, labelled with its provenance, and `record_failure` retires a
note that turned out to be wrong. Graph building is file I/O, so it runs off the event loop.
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import networkx as nx
from pydantic import BaseModel, Field, computed_field

from chemclaw.agent.authz import require_actor
from chemclaw.agent.framing import SYSTEM_SPEECH_MARK, frame_untrusted
from chemclaw.agent.tool_framing import defanged_payload
from chemclaw.core.chem import InvalidSmilesError, compound_id
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_note_written
from chemclaw.ingest.eln.compound import compound_dependencies, compound_note
from chemclaw.ingest.eln.ord import RecordTier
from chemclaw.ingest.eln.records import RECORD_TYPE, default_record_store
from chemclaw.kg.analytics import GraphGaps, analyze
from chemclaw.kg.git_writer import default_writer
from chemclaw.kg.graph import (
    build_graph,
    current_successor,
    load_notes,
    neighborhood,
    note_in,
)
from chemclaw.kg.note import Note, Relation, external_record_ref, resolves_outside_graph
from chemclaw.kg.record import record_note
from chemclaw.kg.relations import DEFAULT_RELATION
from chemclaw.kg.search import query_terms, term_coverage
from chemclaw.memory.failure import close_refuted_note, failure_note
from chemclaw.science.fingerprints.molfp.search import indexed_structure
from chemclaw.science.fingerprints.store import default_molecule_store

# The molecule index `_expand_compound` falls back to, as a seam a test replaces.
_molecule_store = default_molecule_store

log = logging.getLogger(__name__)


class NoteRef(BaseModel):
    """A lightweight reference to a note (no body), for listing and neighbors.

    Provenance is surfaced here (KM-6) so the agent can weigh a source — who authored it
    (`created_by`), where it came from (`source`), how sure it is (`confidence`), and its validity
    window — without a second lookup. Fields default so a bare reference is still constructible.

    **The calculations a note rests on are part of that provenance, and were dropped here.**
    `record_knowledge_note` tells the model to file `calc_refs` from a job's result envelope
    (D-2026-08-21 built the envelope that carries them) "so a stale calculation can be traced to
    the conclusions drawn from it" — and this projection is every reader there is: the model
    through `find_notes`/`expand_note`, and the chemist through `GET /notes/{id}`, which returns
    this same object. Neither saw one, so the citation on a computed note was write-only, and the
    control `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` rests on — "the citations a
    chemist checks at the point of use" — could not be exercised on the notes that most need it.

    Both fields are checked by `Note`'s own validators (`_calc_ref_shape`), which is why they are
    the only strings added here that `_ref` does not have to defang.
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
    # The calculation keys and stored artifacts (`<calc key>#<name>`) this note cites. Empty for
    # every note that rests on no calculation, which is most of the corpus.
    calc_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)


class NeighborRef(NoteRef):
    """A neighbouring note, plus the typed edges that connect it to the note being expanded.

    Direction is kept in two fields because "A supersedes B" and "B supersedes A" are opposite
    claims. Both are empty for a neighbour not directly linked, or linked only by an untyped
    `[[wikilink]]` (`cites`).
    """

    # Relations asserted by the expanded note about this neighbour, and by it about the note; sorted
    # and deduplicated.
    relations_out: list[str] = Field(default_factory=list)
    relations_in: list[str] = Field(default_factory=list)


class NoteView(BaseModel):
    """A note's body plus the notes within a few links of it (graph neighborhood)."""

    note: NoteRef
    body: str
    neighbors: list[NeighborRef]


def _ref(note: Note) -> NoteRef:
    """One note as a reference, with its unconstrained frontmatter neutralised.

    `tags`, `source` and `compound_smiles` are free text (campaign tags come from an ELN field
    anyone sets), and they sit beside framed bodies, so a delimiter in them could close an envelope.
    They are defanged, not framed: a tag is a label, not evidence.
    """
    return NoteRef(
        id=note.id,
        type=note.type,
        compound_smiles=defanged_payload(note.compound_smiles),
        tags=defanged_payload(note.tags),
        created_by=note.created_by,
        source=defanged_payload(note.source),
        confidence=note.confidence,
        valid_from=note.valid_from,
        valid_to=note.valid_to,
        # Not defanged, and that is the one exception this function's rule has: both fields are
        # shape-checked at parse time (`Note._calc_ref_shape`), so neither can carry a delimiter.
        calc_refs=note.calc_refs,
        artifact_refs=note.artifact_refs,
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
    # How many current notes the search looked at, so a miss over a real corpus differs from an
    # empty graph. `None` means the search cannot say (no searchable term), not zero.
    corpus_notes: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence to read before saying the graph holds nothing on a topic.

        A `computed_field` so `model_dump()` carries it into the payload the answer is written from.
        """
        if self.corpus_notes is None:
            return (
                "NOT SEARCHED: the query held no searchable term, so no note was examined. This "
                "says nothing whatever about the graph — ask again with a word to search for."
            )
        if not self.corpus_notes:
            return (
                "NO CORPUS: the knowledge graph holds no current note at all, so this query was "
                "compared against nothing. This is NOT evidence that the topic is unknown — the "
                "question was not answered. Say the graph is empty on this deployment."
            )
        if not self.matches:
            return (
                f"NO MATCH: {self.corpus_notes} current note(s) were searched and none carried "
                "your terms. The graph is populated, so this is a real miss — but it is a miss on "
                "these words, and a differently-worded term may still find it."
            )
        cut = (
            f" {self.total_matches} matched and the {len(self.matches)} best are shown, so the "
            "count is a floor rather than a total."
            if self.total_matches > len(self.matches)
            else ""
        )
        widened = (
            " No note carried every term, so these are partial-coverage hits, best coverage first."
            if self.widened
            else ""
        )
        return (
            f"FOUND: {len(self.matches)} note(s) out of {self.corpus_notes} searched.{cut}{widened}"
        )


def _scan_notes(notes_dir: Path, terms: Sequence[str], today: date, cap: int) -> NoteSearch:
    """Search every current note under `notes_dir` for `terms`, ranked and capped.

    Synchronous so the caller can run the whole O(N) scan, ranking and truncation included, in a
    thread; on the event loop it would stall every other turn on the pod. That buys latency, not
    throughput (the scan holds the GIL).

    It deliberately does not use the Postgres `note_index`: that index is only maintained where a
    lexical or vector source is enabled, and its stemmed matching differs from the substring rule
    `term_coverage` shares with `GraphRetriever`, so moving this reader alone would find notes
    `gather_evidence` cannot cite. Uses `load_notes`, not `build_graph`, since it follows no edge.

    Args:
        notes_dir: The corpus root, resolved by the caller so this stays testable with a fixture.
        terms: The query's tokens, already normalised by `query_terms`.
        today: The date `is_current` is judged against, passed in so one scan cannot straddle
            midnight.
        cap: How many references the answer may carry, `graph_max_results` from the caller.

    Returns:
        The search result, with `total_matches` counting the hits before the cap.
    """
    scored: list[tuple[int, NoteRef]] = []
    searched = 0
    for note in sorted(load_notes(notes_dir), key=lambda candidate: candidate.id):
        # Discovery serves current evidence only: a not-yet-valid or expired note is not surfaced
        # as current fact (KM-7). It stays in Git and remains reachable by explicit id.
        if not note.is_current(today):
            continue
        # Counted after the currency filter: that is the corpus this search can hit.
        searched += 1
        coverage = term_coverage(note, terms)
        if coverage:
            scored.append((coverage, _ref(note)))
    complete = [pair for pair in scored if pair[0] == len(terms)]
    widened = not complete and bool(scored)
    # Widened results rank by coverage; complete ones stay in id order, so identical queries return
    # identical lists.
    chosen = sorted(scored, key=lambda pair: (-pair[0], pair[1].id)) if widened else complete
    # Bound the hit list like every other retrieval surface, and declare the cut in the result so a
    # capped list never reads as the whole corpus.
    return NoteSearch(
        matches=[ref for _, ref in chosen[:cap]],
        total_matches=len(chosen),
        widened=widened,
        corpus_notes=searched,
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
        The matching note references (id + type + smiles + tags, body omitted), `total_matches`
        before the cap, `widened` for partial-coverage hits, and `corpus_notes` — how many current
        notes were searched at all.

        **Read `verdict` before saying the graph holds nothing on a topic.** An empty result means
        one of three things: no searchable term, an empty graph, or a real miss. Only the last is
        evidence about the topic.
    """
    # Same tokenizer and haystack as `chemclaw.kg.search`, so a note found here is one
    # `gather_evidence` can cite.
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

    Reports what the graph says; filtering `cites` is `_neighbor_ref`'s job.
    """
    if not graph.has_edge(source, target):
        return []
    return sorted({relation.rel for relation in graph[source][target].get("relations", ())})


def _neighbor_ref(
    graph: nx.DiGraph, anchor_id: str, note: Note, via: str | None = None
) -> NeighborRef:
    """One neighbour of `anchor_id`, carrying the typed edges between the two.

    `via` is the node the edges actually join when `note` stands in for it (a retired compound
    reported as its successor). `cites` is dropped in both directions: it is what every untyped
    wikilink means, so only deliberately typed edges are reported.
    """
    joined = via if via is not None else note.id
    return NeighborRef(
        **_ref(note).model_dump(),
        relations_out=[
            rel for rel in _edge_relations(graph, anchor_id, joined) if rel != DEFAULT_RELATION
        ],
        relations_in=[
            rel for rel in _edge_relations(graph, joined, anchor_id) if rel != DEFAULT_RELATION
        ],
    )


def _current_compound(graph: nx.DiGraph, note_id: str, today: date) -> Note | None:
    """The current compound note that replaced `note_id`, when `note_id` is a superseded compound.

    Compounds only: a compound id is a hash of its structure, so a retired one is the same substance
    under a stale id. A retired claim is a different claim and is returned as written.

    `None` when `note_id` names a current note, a non-compound, or nothing a supersede link reaches.
    """
    note = note_in(graph, note_id)
    if note is not None and (note.type != "compound" or note.is_current(today)):
        return None
    successor = current_successor(graph, note_id, today)
    return successor if successor is not None and successor.type == "compound" else None


def _require_note(graph: nx.DiGraph, note_id: str) -> Note:
    """The note `note_id` names, or a `ChemclawError` saying it could not be found.

    One message for both by-id lookups (`expand_note`, `record_failure`).
    """
    if note_id not in graph or graph.nodes[note_id].get("note") is None:
        raise ChemclawError(f"no note with id {note_id!r}")
    note: Note = graph.nodes[note_id]["note"]
    return note


# `core.chem.compound_id`'s shape: the prefix and a 12-hex-digit structure hash. A slug-named seed
# note (`compound-thf`) never matches, and neither does anything a person would type as a name.
_STRUCTURAL_COMPOUND_ID = re.compile(r"compound-[0-9a-f]{12}")


def _notes_carrying(graph: nx.DiGraph, note_id: str) -> list[Note]:
    """The current notes whose `compound_smiles` is the structure `note_id` was derived from."""
    today = date.today()
    carrying = []
    for _, data in graph.nodes(data=True):
        note = data.get("note")
        if note is None or not note.compound_smiles or not note.is_current(today):
            continue
        try:
            if compound_id(note.compound_smiles) == note_id:
                carrying.append(note)
        except InvalidSmilesError:
            continue
    return sorted(carrying, key=lambda note: note.id)


async def _expand_compound(graph: nx.DiGraph, note_id: str, hops: int) -> NoteView:
    """Resolve a structure-derived compound id that no note was written under.

    Similarity hits carry `core.chem.compound_id`, which may match no note. In order:

    1. A compound note filed under another id carries this structure — expand it as itself.
    2. Other notes carry the structure — render the compound view with them as neighbours.
    3. Only the molecule index holds it — render the compound view alone, inferring nothing.

    The view says, as system text outside the framed body, that no note was written.
    """
    carrying = await asyncio.to_thread(_notes_carrying, graph, note_id)
    for note in carrying:
        if note.type == "compound":
            return _expand_in_graph(graph, note.id, hops)
    smiles = (
        carrying[0].compound_smiles
        if carrying
        else await indexed_structure(_molecule_store(), note_id)
    )
    if smiles is None:
        raise ChemclawError(f"no note with id {note_id!r}")
    rendered = compound_note(smiles)
    notice = (
        "No note has been written about this compound; this view is derived from its structure. "
        "What was run with it is in the reaction records — search them by this SMILES "
        f"(similar_reactions, substrate_precedent). {SYSTEM_SPEECH_MARK}"
    )
    return NoteView(
        note=_ref(rendered),
        body=f"{notice}\n\n{frame_untrusted(rendered.body, note_id=note_id)}",
        neighbors=[_neighbor_ref(graph, note_id, note) for note in carrying],
    )


async def _expand_record(note_id: str) -> NoteView:
    """Expand a `reaction-<id>` citation from the transcription store.

    A record is data, not a claim, so it returns the transcription with an empty neighbourhood.
    A withdrawn entry still resolves, with a system notice outside the framed body and `valid_to`
    set, so a citation to it never dangles and never reads as current.
    """
    # A qualified citation names its source; a bare one reads across sources and is refused when two
    # hold the id.
    source, record_id = external_record_ref(note_id)
    record = await default_record_store().read(record_id, source)
    if record is None:
        raise ChemclawError(f"no reaction record with id {note_id!r}")
    body = frame_untrusted(record.body, note_id=note_id)
    if record.tier is RecordTier.CITATION_ONLY:
        # The tier as system text, so what this system says is distinct from the framed ELN body.
        notice = (
            "Citation-only record: the source gives no structure for at least one species in this "
            "ELN entry (named in the record as the source gave them). Cite it for what it states "
            "— yields, conditions, the species named — and infer no structure for a named species. "
            "It is excluded from structure and similarity search, so its absence from a structural "
            f"answer says nothing about it. {SYSTEM_SPEECH_MARK}"
        )
        body = f"{notice}\n\n{body}"
    if record.retracted_at is not None:
        notice = (
            f"The source withdrew this ELN entry on {record.retracted_at:%Y-%m-%d}. It is no "
            "longer current evidence and must not be cited as a precedent; it is shown because "
            f"something already cites it. {SYSTEM_SPEECH_MARK}"
        )
        body = f"{notice}\n\n{body}"
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
            # The withdrawal goes in `valid_to`, where every `NoteRef` reader looks for "no longer
            # current".
            valid_to=record.retracted_at.date() if record.retracted_at else None,
        ),
        # Source text a chemist typed into an ELN, so it is framed as data for the same reason a
        # note body is: it reaches the model verbatim and must not be read as instruction.
        body=body,
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
    nothing and therefore links to nothing.

    Args:
        note_id: The id of the entry note.
        hops: How many link steps to expand (1 or 2).

    Returns:
        The note's body plus its neighborhood as references, each with the relations that link it
        to this note.

    Raises:
        ChemclawError: When `note_id` names no current note.
    """
    # `ChemclawError` is the safe "bad input" contract, so `tool_authz` passes its message to the
    # model verbatim. Kept out of the tool docstring, which is resent on every model call.
    graph = await asyncio.to_thread(build_graph, settings.knowledge_path)
    # Graph first, store second: `reaction-` is a prefix, not a reservation, so a human-authored
    # note
    # of that name wins. Test for a note, not mere id membership (see `note_in`).
    if note_in(graph, note_id) is None and resolves_outside_graph(note_id):
        return await _expand_record(note_id)
    # A compound id a standardization bump superseded resolves to the note that replaced it, and
    # says so as system text outside the framed body — see `_current_compound`.
    today = date.today()
    successor = _current_compound(graph, note_id, today)
    if successor is not None:
        view = _expand_in_graph(graph, successor.id, hops)
        notice = (
            f"{note_id} is a superseded id for this compound: the current standardization files "
            f"the same structure as {successor.id}, shown here. {SYSTEM_SPEECH_MARK}"
        )
        return view.model_copy(update={"body": f"{notice}\n\n{view.body}"})
    # A structure-derived compound id with no note under it — what nearly every `similar_molecules`
    # hit cites, since an ELN run is a record and not a note. See `_expand_compound`.
    if note_in(graph, note_id) is None and _STRUCTURAL_COMPOUND_ID.fullmatch(note_id):
        return await _expand_compound(graph, note_id, hops)
    return _expand_in_graph(graph, note_id, hops)


def _expand_in_graph(graph: nx.DiGraph, note_id: str, hops: int) -> NoteView:
    """`expand_note` over a note the graph holds: its body and its current neighbourhood."""
    note = _require_note(graph, note_id)
    # `hops` comes from the model; clamp it to [0, graph_max_hops] so a large value is bounded
    # rather than traversing the whole graph (SEC-4).
    hops = min(max(hops, 0), settings.graph_max_hops)
    today = date.today()
    # The anchor is returned even if expired; non-current neighbours are dropped, except a retired
    # compound, which stands for its successor.
    neighbors: dict[str, NeighborRef] = {}
    for nid in sorted(neighborhood(graph, note_id, hops=hops)):
        neighbor = note_in(graph, nid)
        if neighbor is None:
            continue
        if neighbor.is_current(today):
            neighbors.setdefault(nid, _neighbor_ref(graph, note_id, neighbor))
            continue
        replacement = _current_compound(graph, nid, today)
        if replacement is not None and replacement.id != note_id:
            neighbors.setdefault(
                replacement.id, _neighbor_ref(graph, note_id, replacement, via=nid)
            )
    # The body is note content (possibly ingested, not agent-authored): frame it as data.
    return NoteView(
        note=_ref(note),
        body=frame_untrusted(note.body, note_id=note.id),
        neighbors=sorted(neighbors.values(), key=lambda ref: ref.id),
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

    `undistilled_playbook_ids` is the other half and answers a different question: not "which
    topics have no playbook" but "which playbooks record a recurrence nobody has generalised".
    Each is a note id you can open with `expand_note` and whose cited reactions you can read — and
    the `playbook-distillation` skill is the judgment for turning one into a transferable rule. If
    a chemist asks what the system has spotted but not yet made sense of, this is the list.

    Returns:
        Counts per note type, isolated (unlinked) notes, tags with evidence but no distillation,
        the playbooks still awaiting a rule, the most-cited hub notes, and any dangling links in
        the served graph.
    """
    directory = settings.knowledge_path
    graph = await asyncio.to_thread(build_graph, directory)
    notes = await asyncio.to_thread(load_notes, directory)
    gaps = analyze(graph, notes)
    # Tags and dangling-link targets are free text, so they are defanged; the rest are ids, types
    # and counts.
    return gaps.model_copy(
        update={
            "tags_without_distillation": defanged_payload(gaps.tags_without_distillation),
            "dangling_links": defanged_payload(gaps.dangling_links),
        }
    )


@tool
async def record_knowledge_note(
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
    """Record a new note in the knowledge graph, readable by everyone at once.

    The note is authored as `agent` and is written straight into the graph — there is no review
    step and nothing to wait for. It arrives labelled with that provenance, so a chemist reading it
    as evidence can see it is machine-written, and it can be corrected: `record_failure` writes a
    `contradicts` edge, and a later finding supersedes it. Write what the record actually shows and
    cite it; do not state a conclusion the evidence does not carry, because nobody is going to
    catch it before it is served to the next person. Relate it to other notes with `[[wikilinks]]`
    in the body.

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
        calc_refs: Calculation keys this note rests on. They ride on every reader of the note, so
            a chemist reading a computed claim can check the run behind the number. Get them from
            a job's result envelope.
        artifact_refs: Stored artifacts this note cites, as `<calc key>#<name>`.
        relations: Typed links to other notes — `contradicts`, `supersedes`, `follows` — each
            with its own optional confidence and validity window. Use these rather than prose
            when the relationship is the claim: a `contradicts` is what conflict detection reads.
        valid_from: When this became true (an experiment's own date, not today's).
        valid_to: When it stopped being true, if it has. Leave open otherwise — a result does
            not expire on its own, it is superseded.

    Returns:
        A reference to what landed — the commit the note was recorded in.
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
    # A linked compound note is written first (see `record._build_write`), so the agent can cite the
    # molecule without checking whether its note exists.
    reference = await record_note(note, default_writer(), dependencies=compound_dependencies(note))
    # Surface what landed on the turn's stream — see `core.turn_signals`.
    record_note_written(note.id, reference)
    return reference


@tool
async def record_failure(
    refutes: str,
    what_happened: str,
    compound_smiles: str | None = None,
    confidence: float | None = None,
    held_until: date | None = None,
) -> str:
    """Record that something the knowledge graph says did **not** hold in practice.

    Call this when a chemist reports that a note is wrong, misfired, or no longer matches the
    lab — the counterpart to `record_confirmed_answer`, which can only capture an answer that was
    *confirmed*. It writes a `failure-mode` note carrying a `contradicts` edge back to the note it
    refutes, so conflict detection finds it and every later retrieval of that note arrives marked
    as disputed instead of reading as settled fact.

    The edge is `contradicts` and never `supersedes`: a failure report says the old claim is wrong,
    not that this note is the new right answer — it does not contain one. When you *do* know the
    replacement, write it with `record_knowledge_note` and give it a `supersedes` relation.

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
            write so it stops being served as current. A note a *person* wrote is left alone
            instead: the refutation still lands and still carries its `contradicts` edge, but this
            system does not rewrite somebody else's note, so the retirement is dropped with a
            warning rather than taking the refutation down with it. Leave it unset when the claim is
            simply
            wrong: `held_until` records that the claim was valid up to that date, which for a
            never-true claim would be a new false statement, and the `contradicts` edge already
            keeps the disputed note visible and marked.

    Returns:
        A reference to what landed. **The refutation is live immediately** — it is readable by
        everyone as soon as this returns, so write only what the evidence carries.

    Raises:
        ChemclawError: When `refutes` names no note, when `held_until` predates that note's own
            `valid_from`, or when the note has already been retired on some other date.
    """
    graph = await asyncio.to_thread(build_graph, settings.knowledge_path)
    refuted = _require_note(graph, refutes)
    # The reporter is the authenticated user, never a model-supplied parameter.
    note = failure_note(
        refutes,
        what_happened,
        reported_by=require_actor(),
        compound_smiles=compound_smiles,
        confidence=confidence,
    )
    # Refuse an already-retired note rather than re-close it or silently drop the person's
    # `held_until`; say which date holds.
    if held_until is not None and refuted.valid_to is not None:
        raise ChemclawError(
            f"{refutes} was already retired on {refuted.valid_to.isoformat()}, so it cannot also "
            f"be retired on {held_until.isoformat()} — file the refutation without `held_until`, "
            "or correct the existing date first"
        )
    # Both files ride in one write, in `record._build_write`'s order, so the retirement never cites
    # a
    # successor not yet written. Pass the retirement as `superseded`, not `dependencies`:
    # dependencies
    # are skipped when the file exists, and the refuted note always does.
    retirement = (
        [close_refuted_note(refuted, note.id, held_until)] if held_until is not None else []
    )
    reference = await record_note(note, default_writer(), superseded=retirement)
    record_note_written(note.id, reference)
    return reference
