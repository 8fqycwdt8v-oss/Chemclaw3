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


@runtime_checkable
class SourceRetriever(Protocol):
    """Retrieve evidence for a query from one internal source. One per source."""

    name: str

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return evidence chunks answering `query` under `filters` (may be empty)."""
        ...
