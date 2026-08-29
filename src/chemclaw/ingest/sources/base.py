"""The unified `DataSource` contract: three independent, optional halves (F7-T1; F4 added one).

A data source is anything the system can *ingest from* (an ELN/LIMS drop), *retrieve evidence
from* (the knowledge graph, a literature index), or read *committed work* from (a portfolio export).
The three capabilities are genuinely disjoint — `ElnAdapter`, `SourceRetriever` and
`CommitmentAdapter` are separate protocols with different methods and DTOs — so this seam does not
merge them into one fat interface. It **composes** them: a `DataSource` names itself and exposes
each half optionally, each being the existing protocol verbatim. Only the composition is fixed here;
the shape of what flows through each half is never re-invented (D-018/D-023).

**The third half is what makes this seam entity-shaped as well as corpus-shaped.** The first two
turn records into chunks, notes and fingerprints; a portfolio export is not a corpus but a set of
typed entities with lifecycles, and ingesting one through the corpus halves would land it as
searchable prose (`D-2026-08-29-a-mirror-is-not-a-plan`). It is composed here rather than given a
seam of its own precisely because this file already argues that disjoint halves compose.
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
# The third half (F4): a source that supplies *entities* rather than a corpus. Composed on the same
# terms as the other two — its own Protocol, its own DTO, never merged into a fat interface — and
# added here rather than as a fourth seam because this one was built to compose disjoint halves.
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

    @property
    def commitments(self) -> CommitmentHalf | None:
        """The commitments half, or `None` if this source holds no committed work."""
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
    commitments: CommitmentHalf | None = None

    def __post_init__(self) -> None:
        """Reject a source that provides no half at all (nothing could ever use it)."""
        if self.ingest is None and self.retrieve is None and self.commitments is None:
            raise ValueError(
                f"data source {self.name!r} must provide an ingest, retrieve or commitments half"
            )
