"""Sync new ELN entries into the graph + fingerprint index (plan step 4.5, core).

The backend-agnostic sync loop: pull entries newer than a cursor from an adapter, map each
to the canonical schema, and ingest it. A single bad entry (unparseable ELN shape or a
reaction that fails validation) is recorded and skipped, never aborting the whole batch
(G4) — the summary says exactly what was ingested and what was rejected and why. Because
every write is idempotent, re-running from an earlier cursor is safe. Deps are injected, so
this whole flow is tested in-memory; `workflows.eln_sync` wraps it as a Temporal activity
with production stores, adapter, and submitter.
"""

import logging
from datetime import datetime

from pydantic import BaseModel

from chemclaw.errors import ChemclawError
from eln.adapter import ElnAdapter
from eln.ingest import ingest_reaction
from kg.pr_gate import NoteSubmitter
from mcp_servers.fpstore import FingerprintStore

logger = logging.getLogger(__name__)


class RejectedEntry(BaseModel):
    """An entry that could not be ingested, with the reason (for the sync report)."""

    entry_id: str
    reason: str


class IngestSummary(BaseModel):
    """The outcome of one sync run: what was ingested, what was rejected, the next cursor.

    `next_cursor` is the newest entry timestamp seen, which the scheduler persists and
    passes as `since` next run. Fetching is inclusive at the cursor (see `ElnAdapter`),
    so an entry stamped exactly at `next_cursor` may be re-fetched next run — harmless,
    because ingestion is idempotent (id-keyed upserts + idempotent note branch), and it
    guarantees a same-second entry exported after this run is never skipped.
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
        except ChemclawError as exc:
            # The shared bad-data base covers *any* per-entry failure: an adapter's
            # mapping error, a validation failure, and a fingerprint that cannot be
            # computed (e.g. a schema-valid but degenerate reaction). Enumerating
            # concrete types here once turned one bad entry into a batch abort.
            rejected.append(RejectedEntry(entry_id=raw.entry_id, reason=str(exc)))
            continue
        ingested.append(raw.entry_id)
    # The summary is a return value the scheduler stores; also log the outcome so an admin
    # running this under a Temporal Schedule sees it without opening the workflow result, and
    # gets a WARNING trail of exactly which entries were rejected and why.
    logger.info("eln sync: ingested=%d rejected=%d", len(ingested), len(rejected))
    for entry in rejected:
        logger.warning("eln sync rejected entry %s: %s", entry.entry_id, entry.reason)
    return IngestSummary(ingested=ingested, rejected=rejected, next_cursor=cursor)
