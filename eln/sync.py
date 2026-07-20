"""Sync new ELN entries into the graph + fingerprint index (plan step 4.5, core).

The backend-agnostic sync loop: pull entries newer than a cursor from an adapter, map each
to the canonical schema, and ingest it. A single bad entry (unparseable ELN shape or a
reaction that fails validation) is recorded and skipped, never aborting the whole batch
(G4) — the summary says exactly what was ingested and what was rejected and why. Because
every write is idempotent, re-running from an earlier cursor is safe. Deps are injected, so
this whole flow is tested in-memory; `workflows.eln_sync` wraps it as a Temporal activity
with production stores, adapter, and submitter.
"""

from datetime import datetime

from pydantic import BaseModel

from eln.adapter import ElnAdapter, ElnMappingError
from eln.ingest import IngestError, ingest_reaction
from kg.pr_gate import NoteSubmitter
from mcp_servers.fpstore import FingerprintStore


class RejectedEntry(BaseModel):
    """An entry that could not be ingested, with the reason (for the sync report)."""

    entry_id: str
    reason: str


class IngestSummary(BaseModel):
    """The outcome of one sync run: what was ingested, what was rejected, the next cursor.

    `next_cursor` is the newest entry timestamp seen, which the scheduler persists and
    passes as `since` next run so each entry is processed once.
    """

    ingested: list[str]
    rejected: list[RejectedEntry]
    next_cursor: datetime


async def sync_entries(
    adapter: ElnAdapter,
    reaction_store: FingerprintStore,
    molecule_store: FingerprintStore,
    submitter: NoteSubmitter,
    since: datetime,
) -> IngestSummary:
    """Fetch entries after `since`, ingest each, and return a summary + next cursor."""
    entries = await adapter.fetch_new_entries(since)
    ingested: list[str] = []
    rejected: list[RejectedEntry] = []
    cursor = since
    for raw in entries:
        cursor = max(cursor, raw.created_at)
        try:
            reaction = adapter.map_to_ord(raw)
            await ingest_reaction(reaction, reaction_store, molecule_store, submitter)
        except (IngestError, ElnMappingError) as exc:
            # ElnMappingError covers *any* adapter's mapping failure (bad shape, unknown
            # role, schema violation); IngestError covers a reaction that fails validation.
            rejected.append(RejectedEntry(entry_id=raw.entry_id, reason=str(exc)))
            continue
        ingested.append(raw.entry_id)
    return IngestSummary(ingested=ingested, rejected=rejected, next_cursor=cursor)
