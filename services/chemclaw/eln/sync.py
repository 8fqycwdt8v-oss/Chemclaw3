"""Sync new ELN entries into the graph + fingerprint index (plan step 4.5, core).

The backend-agnostic sync loop: pull entries newer than a cursor from an adapter, map each
to the canonical schema, and ingest it. A single bad entry (unparseable ELN shape or a
reaction that fails validation) is recorded and skipped, never aborting the whole batch
(G4) — the summary says exactly what was ingested and what was rejected and why. Because
every write is idempotent, re-running from an earlier cursor is safe. Deps are injected, so
this whole flow is tested in-memory; `workflows.eln_sync` wraps it as a Temporal activity
with production stores, adapter, and submitter.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from chemclaw.config import settings
from chemclaw.errors import ChemclawError
from eln.adapter import ElnAdapter
from eln.ingest import ingest_reaction
from eln.note import note_from_ord_reaction
from kg.graph import load_notes
from kg.pr_gate import NoteSubmitter
from mcp_servers.fpstore import FingerprintStore

logger = logging.getLogger(__name__)

# External identifiers/messages cross a trust boundary when they reach the log: a CR/LF in
# an ELN entry id (or in an error message quoting one) could forge whole log lines.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _log_safe(value: str) -> str:
    """Collapse control characters to spaces so external text cannot forge log lines."""
    return _CONTROL_CHARS.sub(" ", value)


class RejectedEntry(BaseModel):
    """An entry that could not be ingested, with the reason and its timestamp.

    `created_at` is the entry's own ELN timestamp: it is the exact `since` an admin re-runs
    the sync from to re-ingest this entry once its source record is corrected upstream (the
    sync is re-runnable from any earlier cursor — ingestion is idempotent). See runbook (v).
    """

    entry_id: str
    reason: str
    created_at: datetime


class IngestSummary(BaseModel):
    """The outcome of one sync run: what was ingested, what was rejected, the next cursor.

    `next_cursor` is the newest entry timestamp seen, which the scheduler persists and
    passes as `since` next run. Fetching is inclusive at the cursor (see `ElnAdapter`),
    so an entry stamped exactly at `next_cursor` may be re-fetched next run — harmless,
    because ingestion is idempotent (id-keyed upserts + idempotent note branch), and it
    guarantees a same-second entry exported after this run is never skipped.

    The cursor advances past *rejected* entries too (a rejection is deterministic bad
    data — re-fetching it would only re-reject it). Rejections are therefore reported
    here and logged, not retried: correcting the source record upstream and re-ingesting
    it is a deliberate manual/backlog action, not something the periodic sync retries.
    The one exception is an entry stamped implausibly far in the future (beyond wall
    clock + `eln_sync_future_tolerance_seconds`): it is rejected *without* advancing the
    cursor, because a typo'd future year that became the persisted cursor would silently
    skip every later real entry forever. `next_cursor` also never regresses below the
    run's `since`, even though the fetch reaches an overlap window behind it.

    `skipped_existing` lists overlap-window entries whose note already sits merged in
    the knowledge dir: they were fully ingested by an earlier run, so this run skipped
    them cheaply instead of re-running the fingerprint upserts and the PR-gate git
    cycle. Kept separate from `ingested` so operators see the overlap short-circuit
    working rather than a re-inflated ingest count.
    """

    ingested: list[str]
    skipped_existing: list[str] = Field(default_factory=list)
    rejected: list[RejectedEntry]
    next_cursor: datetime


async def sync_entries(
    adapter: ElnAdapter,
    reaction_store: FingerprintStore,
    molecule_store: FingerprintStore,
    submitter: NoteSubmitter,
    since: datetime,
    *,
    apply_overlap: bool = True,
) -> IngestSummary:
    """Fetch entries from `since` minus the overlap window, ingest each, return a summary.

    The fetch deliberately reaches behind the cursor (`eln_sync_overlap_seconds`) so an
    export file that lands late with an older payload timestamp is still picked up —
    re-ingesting the window is free because every write is idempotent. The returned
    `next_cursor` is floored at `since`, so the overlap never regresses the stored cursor.

    `apply_overlap=False` fetches from `since` itself (still inclusive, per the adapter
    contract): the workflow's chunk loop reaches behind the cursor only on its first
    chunk, so draining a backlog does not re-fetch the whole overlap window per chunk.

    Overlap replay is cheap, not just idempotent: an overlap entry whose note is already
    merged into the knowledge dir was fully ingested by an earlier run — ELN exports are
    immutable (the overlap window's premise), so it is skipped by a note-id lookup
    instead of re-running fingerprint upserts plus a PR-gate git submission cycle.
    """
    entries = await adapter.fetch_new_entries(_fetch_floor(since) if apply_overlap else since)
    ingested: list[str] = []
    skipped_existing: list[str] = []
    rejected: list[RejectedEntry] = []
    existing_ids: set[str] | None = None
    cursor = since
    horizon = datetime.now(UTC) + timedelta(seconds=settings.eln_sync_future_tolerance_seconds)
    for raw in entries:
        if raw.created_at > horizon:
            # A timestamp beyond the wall clock (a typo'd year) must never become the
            # persisted high-water cursor: nothing ever lowers a stored cursor, so it
            # would silently skip every later real entry. Reject without advancing.
            rejected.append(
                RejectedEntry(
                    entry_id=raw.entry_id,
                    reason=f"timestamp {raw.created_at.isoformat()} is implausibly far "
                    "in the future (beyond wall clock + tolerance)",
                    created_at=raw.created_at,
                )
            )
            continue
        cursor = max(cursor, raw.created_at)
        try:
            reaction = adapter.map_to_ord(raw)
            if raw.created_at <= since:
                # Overlap replay: the merged-note lookup is loaded lazily (once per run,
                # off the event loop) and only when the fetch actually replayed entries.
                if existing_ids is None:
                    existing_ids = await asyncio.to_thread(_merged_note_ids)
                if note_from_ord_reaction(reaction).id in existing_ids:
                    skipped_existing.append(raw.entry_id)
                    continue
            await ingest_reaction(reaction, reaction_store, molecule_store, submitter)
        except (ChemclawError, ValidationError) as exc:
            # The shared bad-data base covers *any* per-entry failure: an adapter's
            # mapping error, a validation failure, and a fingerprint that cannot be
            # computed (e.g. a schema-valid but degenerate reaction). Enumerating
            # concrete types here once turned one bad entry into a batch abort.
            # pydantic's ValidationError is caught alongside because it is a *sibling*
            # ValueError, not a ChemclawError — e.g. an entry id that is not a valid
            # note slug fails at Note construction, which is deterministic bad data
            # per entry, exactly what reject-and-continue exists for.
            rejected.append(
                RejectedEntry(entry_id=raw.entry_id, reason=str(exc), created_at=raw.created_at)
            )
            continue
        ingested.append(raw.entry_id)
    # The summary is a return value the scheduler stores; also log the outcome so an admin
    # running this under a Temporal Schedule sees it without opening the workflow result, and
    # gets a WARNING trail of exactly which entries were rejected and why.
    logger.info(
        "eln sync: ingested=%d rejected=%d skipped_existing=%d",
        len(ingested),
        len(rejected),
        len(skipped_existing),
    )
    for entry in rejected:
        # An overlap-window rejection (`created_at <= since`) is a replay: the cursor advances
        # past sane-timestamped rejections, so this entry was already warned about when first
        # seen. DEBUG keeps hourly re-rejections from burying genuinely new WARNINGs; future-
        # stamped entries stay WARNING every run because they keep poisoning the fetch window.
        level = logging.DEBUG if entry.created_at <= since else logging.WARNING
        logger.log(
            level,
            "eln sync rejected entry %s (at %s): %s",
            _log_safe(entry.entry_id),
            entry.created_at.isoformat(),
            _log_safe(entry.reason),
        )
    return IngestSummary(
        ingested=ingested,
        skipped_existing=skipped_existing,
        rejected=rejected,
        next_cursor=cursor,
    )


def _merged_note_ids() -> set[str]:
    """Ids of every note already merged into the knowledge dir (the already-ingested check).

    Why: the hourly overlap replay must not pay a full ingest (fingerprint upserts + the
    PR-gate's fetch/checkout/push dance) per already-merged entry just to discover it was a
    no-op. Reads through the graph loader's stat-fingerprint cache, so a run where nothing
    merged costs a directory stat, and each replayed entry costs one set lookup.
    """
    knowledge = Path(settings.knowledge_dir)
    if not knowledge.is_dir():
        return set()
    return {note.id for note in load_notes(knowledge)}


def _fetch_floor(since: datetime) -> datetime:
    """`since` minus the configured overlap window (clamped at the epoch floor).

    Why: files arrive in the export directory decoupled from event-time order — an
    upstream export retry can drop an older-stamped file *after* a newer one was synced.
    A strictly `>= since` fetch would drop it forever; the bounded overlap re-fetches it.
    """
    overlap = timedelta(seconds=settings.eln_sync_overlap_seconds)
    epoch = datetime.min.replace(tzinfo=UTC)
    return since - overlap if since - epoch > overlap else epoch
