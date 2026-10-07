"""The unified `DataSource` contract: three independent, optional halves.

A source may be ingested from (`ElnAdapter`, an ELN/LIMS drop), retrieved from (`SourceRetriever`,
evidence for a query) or read for committed work (`CommitmentAdapter`, a portfolio export of typed
entities rather than a corpus). The halves are disjoint protocols with their own DTOs, so this seam
composes them rather than merging them into one interface.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chemclaw.ingest.commitments.adapter import CommitmentAdapter
from chemclaw.ingest.eln.adapter import ElnAdapter, RawEntry
from chemclaw.retrieval.evidence import EvidenceChunk, SourceRetriever

# The two halves are the existing protocols, named by their role in the seam. Reusing them verbatim
# is the whole point: a source re-hosts an adapter/retriever unchanged, it does not reimplement one.
IngestHalf = ElnAdapter  # fetch_new_entries(since) -> [RawEntry]; map_to_ord(raw) -> OrdReaction
RetrieveHalf = SourceRetriever  # name; retrieve(query, filters) -> [EvidenceChunk]
# The third half: a source that supplies entities rather than a corpus.
CommitmentHalf = CommitmentAdapter  # fetch_commitments(since) -> [Commitment]

# Re-export the reused DTOs so a source module imports them from the seam, not from two subsystems.
__all__ = [
    "CommitmentHalf",
    "DataSource",
    "EvidenceChunk",
    "IngestHalf",
    "RawEntry",
    "RetrieveHalf",
    "SourceSpec",
]


@runtime_checkable
class DataSource(Protocol):
    """A named attachment point exposing any of three optional halves.

    The halves are `ingest`, `retrieve` and `commitments`; a source provides whichever it can and
    `None` for the rest. Members are read-only properties, so a frozen `SourceSpec` satisfies it.
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

    @property
    def commitments(self) -> CommitmentHalf | None:
        """The commitments half, or `None` if this source holds no committed work."""
        ...


@dataclass(frozen=True)
class SourceSpec:
    """The concrete `DataSource` a registry entry builds: a name plus whichever halves it provides.

    A source with no half at all is rejected at build time.
    """

    name: str
    ingest: IngestHalf | None = None
    retrieve: RetrieveHalf | None = None
    commitments: CommitmentHalf | None = None

    def __post_init__(self) -> None:
        """Reject a source that provides no half at all (nothing could ever use it)."""
        if self.ingest is None and self.retrieve is None and self.commitments is None:
            raise ValueError(
                f"data source {self.name!r} must provide an ingest, retrieve or commitments half"
            )
