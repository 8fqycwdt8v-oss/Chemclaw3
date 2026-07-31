"""The report harness's source-agnostic contract (plan steps 5b.1, 5b.2).

An `EvidenceChunk` is a retrieved fact that **must** carry a back-reference to the source note
it came from (`source_note_id`) — the harness refuses to synthesize anything not tied to a
note (no fabricated statistics, 5b.4). A `SourceRetriever` is the only thing the harness core
knows: a `retrieve(query, filters)` that returns evidence chunks. Concrete sources (graph,
fingerprint search, analytics) implement it as thin adapters, so adding a source — later even
external literature — is a new retriever behind this interface, never a change to the core (G6).
"""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class EvidenceChunk(BaseModel):
    """One retrieved fact and its mandatory citation back to the source note."""

    content: str = Field(min_length=1)
    source_note_id: str = Field(min_length=1)
    # How the chunk was found (which retriever) — provenance for the report footer.
    retriever: str = Field(min_length=1)
    # A relevance/support score in [0, 1], higher = keep first when a sweep must truncate (KM-5).
    # Each retriever sets it in its own terms — graph hits by the note's `confidence`, structural
    # hits by similarity — so it orders within a sweep; it is a ranking heuristic, not a calibrated
    # cross-source probability. Defaults to a neutral 0.5: every current retriever sets it
    # explicitly, so this only governs a future retriever that forgets to — and neutral keeps such
    # a chunk in the middle of the ranking rather than silently pinning it last (and truncated).
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    # Notes this chunk's source note is known or suspected to disagree with (`kg.conflicts`,
    # KM-8). A *flag*, never a filter: retrieval has no basis for deciding which of two curated
    # notes is right, and silently returning both is what made two contradictory notes read as
    # corroboration. Empty for the ordinary case, so a reader sees the marker only when there is
    # something to see.
    conflicts_with: list[str] = Field(default_factory=list)
    # Who authored the source note, where it came from, and how sure it is (D-160). `NoteRef` has
    # exposed all three to `find_notes`/`expand_note` since KM-6; the sweep that gathers most of
    # the evidence an answer is built on carried none of them, so the model saw a claim and no way
    # to weigh who was claiming it. `confidence` did reach here — as `score`, a truncation-order
    # signal — which is not the same thing as being *told* a note is uncertain.
    #
    # This is harmless while everything readable was human-merged, and becomes a correctness bug
    # the moment a second, ungated tier exists (D-161). It ships first and on its own for that
    # reason.
    #
    # `created_by` is deliberately `""`, not `"human"`, when the retriever could not establish it:
    # a structural hit is generated from the fingerprint index and has no note author. Defaulting
    # to "human" would assert provenance nobody checked, in the one field whose whole purpose is
    # to be trusted.
    created_by: str = ""
    source: str = ""
    confidence: float | None = None


@runtime_checkable
class SourceRetriever(Protocol):
    """Retrieve evidence for a query from one internal source. One per source."""

    name: str

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return evidence chunks answering `query` under `filters` (may be empty)."""
        ...
