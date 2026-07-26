"""The unified `DataSource` contract: two independent, optional halves (plan F7-T1).

A data source is anything the system can *ingest from* (an ELN/LIMS drop), *retrieve evidence from*
(the knowledge graph, a future literature index), or both. The two capabilities are genuinely
disjoint today — `ElnAdapter` and `SourceRetriever` are separate protocols with different methods
and DTOs — so this seam does not merge them into one fat interface. It **composes** them: a
`DataSource` names itself and exposes an optional `ingest` half and an optional `retrieve` half,
each being the existing protocol verbatim. Only the composition is fixed here; the shape of what
flows through each half is never re-invented (D-018/D-023).
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from eln.adapter import ElnAdapter, RawEntry
from report.evidence import EvidenceChunk, SourceRetriever

# The two halves are the existing protocols, named by their role in the seam. Reusing them verbatim
# is the whole point: a source re-hosts an adapter/retriever unchanged, it does not reimplement one.
IngestHalf = ElnAdapter  # fetch_new_entries(since) -> [RawEntry]; map_to_ord(raw) -> OrdReaction
RetrieveHalf = SourceRetriever  # name; retrieve(query, filters) -> [EvidenceChunk]

# Re-export the reused DTOs so a source module imports them from the seam, not from two subsystems.
__all__ = ["DataSource", "IngestHalf", "RetrieveHalf", "SourceSpec", "RawEntry", "EvidenceChunk"]


@runtime_checkable
class DataSource(Protocol):
    """A named attachment point exposing an optional ingest and/or an optional retrieve half.

    Members are read-only (properties), so a `frozen` implementation like `SourceSpec` satisfies the
    contract — nothing reassigns a source's halves after it is built.
    """

    @property
    def name(self) -> str:
        """The source's stable key (also its registry name)."""
        ...

    @property
    def ingest(self) -> IngestHalf | None:
        """The ingest half, or `None` if this source cannot be ingested from."""
        ...

    @property
    def retrieve(self) -> RetrieveHalf | None:
        """The retrieve half, or `None` if this source cannot be retrieved from."""
        ...


@dataclass(frozen=True)
class SourceSpec:
    """The concrete `DataSource` a registry entry builds: a name plus whichever halves it provides.

    Constructing one with neither half is a programming error — a source that can be neither
    ingested from nor retrieved from is not a source — so it is rejected at build time.
    """

    name: str
    ingest: IngestHalf | None = None
    retrieve: RetrieveHalf | None = None

    def __post_init__(self) -> None:
        """Reject a source that provides neither half (nothing could ever use it)."""
        if self.ingest is None and self.retrieve is None:
            raise ValueError(f"data source {self.name!r} must provide an ingest or retrieve half")
