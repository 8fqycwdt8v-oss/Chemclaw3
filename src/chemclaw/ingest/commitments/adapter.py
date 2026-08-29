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

    async def fetch_commitments(self, since: datetime | None) -> list[Commitment]:
        """Every commitment whose state changed since `since`, or all of them when `None`.

        A source that cannot answer incrementally returns everything and lets the upsert absorb it;
        the sync is keyed on `(source, external_id)`, so a full re-read is idempotent rather than
        duplicating. That is the right default for a portfolio export, which is usually a snapshot.
        """
        ...
