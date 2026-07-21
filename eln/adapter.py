"""The ELN adapter contract (plan step 4.2).

Only the *contract* is fixed, never an ELN's shape: an adapter fetches raw entries newer
than a cursor and maps each into the canonical `OrdReaction`. Every ELN-specific quirk
lives behind this seam (G6), so the sync (`workflows.eln_sync`) and everything above it are
identical no matter which ELN is wired. There is no universal ELN abstraction — one adapter
per source (DEFERRED.md: generalize only from a third source).
"""

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.errors import ChemclawError
from eln.ord import OrdReaction


class ElnMappingError(ChemclawError):
    """An adapter could not map a raw entry to a canonical reaction (G4).

    Defined at the contract level (not in a concrete adapter) so the sync's
    reject-and-continue handler catches *any* adapter's mapping failure, not just one
    adapter's error type. Concrete adapters raise this (or a subclass) for a bad entry.
    """


class RawEntry(BaseModel):
    """One raw ELN entry: its id, its creation time, and its source-shaped payload.

    `payload` is deliberately untyped (`dict[str, Any]`) — it is the ELN's own format,
    which only the adapter that produced it understands. Nothing above the adapter reads it.
    """

    entry_id: str = Field(min_length=1)
    created_at: datetime
    payload: dict[str, Any]


@runtime_checkable
class ElnAdapter(Protocol):
    """Fetch new ELN entries and map them to the canonical schema. One per ELN source."""

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Return entries created at or after `since` (the sync's high-water cursor).

        Inclusive on purpose: the cursor is the newest timestamp already seen, and an
        entry stamped in that same second but exported after the run would be skipped
        forever under strictly-after semantics. Re-fetching the boundary entry is safe
        because ingestion is idempotent (id-keyed upserts + idempotent note branch).
        """
        ...

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map one raw entry to a canonical `OrdReaction` (the ELN-specific step)."""
        ...
