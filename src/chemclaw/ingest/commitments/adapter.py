"""The commitments half of the `DataSource` seam: a source that supplies entities, not a corpus.

A portfolio export is a set of typed entities with lifecycles rather than chunks and notes, so it
gets its own Protocol and DTO, composed beside `ElnAdapter` and `SourceRetriever` rather than
merged into them or given a separate seam. Like the other halves it is read-only: mirroring a
milestone in confers no ability to move one.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from chemclaw.ingest.commitments.models import Commitment


@runtime_checkable
class CommitmentAdapter(Protocol):
    """Fetch the commitments a source holds, newest state first.

    Takes a watermark like `ElnAdapter.fetch_new_entries`, so the durable sync shares the ELN
    sync's cursor discipline.
    """

    #: Whether `fetch_commitments` always returns the source's whole picture, ignoring `since`.
    #:
    #: The upsert only converges upward, so a withdrawn row would stay live for ever; a snapshot
    #: source licenses `durable/commitment_sync.py` to sweep what a pass did not restate. Default
    #: `False`: for an incremental source an absent row means "unchanged", and sweeping would empty
    #: the mirror on the first quiet pass.
    snapshot: bool = False

    async def fetch_commitments(self, since: datetime | None) -> list[Commitment]:
        """Every commitment whose state changed since `since`, or all of them when `None`.

        A source that cannot answer incrementally returns everything; the upsert on
        `(source, external_id)` makes that idempotent. Returning everything once is not declaring
        `snapshot`, which is a promise about every call and the only thing that makes deletion safe.
        """
        ...
