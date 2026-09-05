"""The third half of the `DataSource` seam: a source that supplies *entities* rather than a corpus.

`ingest/sources/base.py` composes two optional halves and says why: *"The two capabilities are
genuinely disjoint today — `ElnAdapter` and `SourceRetriever` are separate protocols with different
methods and DTOs — so this seam does not merge them into one fat interface."* A commitments half is
the third such capability and takes the same treatment: its own Protocol, its own DTO, composed
rather than merged.

**Why a third half and not a fourth seam.** The 2026-08-28 audit's finding was that the source seam
is *corpus-shaped* — records become chunks, notes and fingerprints — and that a portfolio export is
not a corpus but a set of typed entities with lifecycles. Both halves of that are true, and the
conclusion is not a new manifest: this seam was built to compose disjoint halves, and adding one
costs a field where a fourth seam would cost a manifest, a registry, a validator and a discovery
path an operator has to learn. What a commitment is *not* is settled by the Protocol below, which
returns `Commitment` and never a `RawEntry`.

**A commitments half is read-only, like the other two.** `ingest/sources/README.md` states that a
source "cannot acquire a write path by declaring one", and that rule is unchanged here: mirroring a
milestone in does not confer the ability to move one. Writing back is the effector seam's business.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from chemclaw.ingest.commitments.models import Commitment


@runtime_checkable
class CommitmentAdapter(Protocol):
    """Fetch the commitments a source holds, newest state first.

    One method, taking a watermark, exactly as `ElnAdapter.fetch_new_entries` does — so the durable
    sync that drives it is the same shape as the ELN sync and shares its cursor discipline.
    """

    #: Whether `fetch_commitments` always returns the source's **whole** picture, ignoring `since`.
    #:
    #: It exists because the upsert can only converge *upward*. A snapshot says "this is no longer
    #: committed" by not containing the row, and `(source, external_id)` gives the write no way to
    #: hear that: a withdrawn milestone kept a live state and stayed in `outstanding()` for ever,
    #: inside a list stamped with the refreshed rows' freshness. `durable/commitment_sync.py` sweeps
    #: what the pass did not restate — but only where that absence *means* something, which is
    #: exactly this claim.
    #:
    #: Default `False`, so a source that answers incrementally is never swept: for that source an
    #: absent row means "unchanged", and sweeping would delete the whole mirror on the first
    #: quiet pass. Opting in is the adapter asserting the stronger contract about itself.
    snapshot: bool = False

    async def fetch_commitments(self, since: datetime | None) -> list[Commitment]:
        """Every commitment whose state changed since `since`, or all of them when `None`.

        A source that cannot answer incrementally returns everything and lets the upsert absorb it;
        the sync is keyed on `(source, external_id)`, so a full re-read is idempotent rather than
        duplicating. That is the right default for a portfolio export, which is usually a snapshot.

        Returning everything is not the same as declaring `snapshot` — one is what this call did,
        the other is a promise about every call, and only the promise makes a deletion safe.
        """
        ...
