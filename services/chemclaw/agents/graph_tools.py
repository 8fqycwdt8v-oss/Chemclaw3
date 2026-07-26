"""Agent tools for the knowledge graph (plan steps 2.5, 2.6).

Read tools (`find_notes`, `expand_note`) let the agent retrieve by graph traversal
— the capability behind the `knowledge-graph-query` skill. The write tool
(`propose_knowledge_note`) routes an agent-authored note through the PR-gate for
human review (D-005), never straight to the graph. Graph building is file I/O, so
it runs off the event loop.
"""

import asyncio
import logging
from datetime import date

from pydantic import BaseModel, Field

from agents.framing import frame_untrusted
from agents.tool_registry import tool
from chemclaw.config import settings
from chemclaw.errors import ChemclawError
from kg.git_submitter import default_submitter
from kg.graph import build_graph, neighborhood
from kg.note import Note
from kg.pr_gate import propose_note

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
    needles = text.lower().split()
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
        haystack = " ".join(
            [note.id, note.type, note.compound_smiles or "", *note.tags, note.body]
        ).lower()
        if needles and all(needle in haystack for needle in needles):
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
            own always-safe "bad input" contract (`chemclaw.errors`), so `agents.tool_authz`
            surfaces this message to the model verbatim instead of MAF's opaque generic
            failure — the common real cause is a citation to a note still pending PR-gate
            review (D-018: a fingerprint-indexed reaction whose note has not yet been merged),
            which the chemist can otherwise not distinguish from a typo or a deleted note.
    """
    graph = await asyncio.to_thread(build_graph, settings.knowledge_path)
    if note_id not in graph or graph.nodes[note_id].get("note") is None:
        raise ChemclawError(
            f"no note with id {note_id!r} — it may not exist, or it may be a citation to a "
            "reaction that has been indexed but whose note is still pending human review"
        )
    note = graph.nodes[note_id]["note"]
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
async def propose_knowledge_note(
    id: str,
    type: str,
    body: str,
    compound_smiles: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
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
        created_by="agent",
    )
    return await propose_note(note, default_submitter())
