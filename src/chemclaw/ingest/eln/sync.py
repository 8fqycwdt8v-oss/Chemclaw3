"""Sync new ELN entries into the graph and fingerprint index.

The backend-agnostic loop: pull entries newer than a cursor from an adapter, map each to the
canonical schema, and ingest it. A bad entry is recorded and skipped, never aborting the batch, and
the summary says what was ingested and rejected and why. Every write is idempotent, so re-running
from an earlier cursor is safe. Dependencies are injected; `chemclaw.durable.eln_sync` wraps this as
a Temporal activity.
"""

import logging
import re
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.ingest.eln.adapter import ElnAdapter, RawEntry, entry_window
from chemclaw.ingest.eln.ingest import ingest_reaction
from chemclaw.ingest.eln.ord import RecordTier
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import ReactionRecordStore
from chemclaw.ingest.eln.warehouse.expr import pattern_budget
from chemclaw.science.fingerprints.store import FingerprintStore
from chemclaw.science.labels.store import LabelIndex

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

    `next_cursor` is the newest *fetch window* seen (`entry_window` — the later of creation and
    amendment, which is what the adapters filter on), which the scheduler persists and
    passes as `since` next run. Fetching is inclusive at the cursor (see `ElnAdapter`),
    so an entry stamped exactly at `next_cursor` may be re-fetched next run — harmless,
    because ingestion is idempotent (id-keyed upserts throughout), and it
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

    **Both non-rejected lists describe work this run did or skipped, and they say different
    things.** An entry is *ingested* when this run indexed its fingerprints and wrote its
    transcription — which, since D-2026-08-25, is the whole of what ingesting means: the record is
    queryable the moment the write returns, with no review queue between the entry and a chemist.
    It is *skipped_existing* when the corpus already holds a byte-identical body, so there was
    nothing to index or store again.

    There used to be a third list, `awaiting_merge`, for entries proposed into a review queue that
    had not moved — the operator-facing signal that the same entries were going round every run
    while the ingest count read as steady progress. It is gone because the queue is: nothing waits
    on a human to become readable, so an ingested entry is simply ingested.
    """

    ingested: list[str]
    # The subset of `ingested` that landed citation-only (stored and citable, in no structure
    # index). A subset rather than a fourth outcome, because the entry was ingested.
    citation_only: list[str] = Field(default_factory=list)
    skipped_existing: list[str] = Field(default_factory=list)
    rejected: list[RejectedEntry]
    next_cursor: datetime


async def sync_entries(
    adapter: ElnAdapter,
    reaction_store: FingerprintStore,
    molecule_store: FingerprintStore,
    record_store: ReactionRecordStore,
    since: datetime,
    *,
    label_index: LabelIndex,
    source: str,
    apply_overlap: bool = True,
) -> IngestSummary:
    """Fetch entries from `since` minus the overlap window, ingest each, return a summary.

    The fetch reaches behind the cursor (`eln_sync_overlap_seconds`) so a late-landing file with an
    older timestamp is still picked up; `next_cursor` is floored at `since`, so the overlap never
    regresses the cursor. `apply_overlap=False` fetches from `since` itself, for continuation
    chunks.

    `label_index` and `source` are required and passed to `ingest_reaction`.

    An overlap entry whose stored body is byte-identical (and whose withdrawal state is unchanged)
    is skipped after one indexed lookup over the replayed ids, bounded by the page. That also skips
    its label row, correctly, since an identical body is an identical canonical record; an amended
    body re-records and re-derives labels.
    """
    started = time.perf_counter()
    floor = _fetch_floor(since) if apply_overlap else since
    entries = await adapter.fetch_new_entries(floor)
    ingested: list[str] = []
    citation_only: list[str] = []
    skipped_existing: list[str] = []
    rejected: list[RejectedEntry] = []
    stored: dict[str, str] | None = None
    withdrawn: set[tuple[str, str]] = set()
    cursor = since
    horizon = datetime.now(UTC) + timedelta(seconds=settings.eln_sync_future_tolerance_seconds)
    # One regex budget for the whole page, opened here so no caller can forget it: the per-cell
    # `eln_regex_timeout_seconds` multiplies by every cell of every entry and gives no page bound on
    # its own (see `expr.pattern_budget`). `tests/test_warehouse_binding.py` requires every
    # entry-mapping loop to enter this.
    with pattern_budget():
        for raw in entries:
            # The cursor advances on the timestamp the entry was fetched by (`entry_window`, the
            # later of creation and amendment). Advancing on `created_at` alone would wedge a source
            # whose fetch orders by amendment: the same page would return forever, silently.
            window = entry_window(raw.created_at, raw.modified_at, raw.retracted_at)
            # A timestamp beyond the wall clock must never become the cursor, since nothing lowers a
            # stored cursor. A future creation stamp rejects the entry; a future amendment stamp is
            # a metadata typo on real chemistry, so the entry ingests and only the cursor ignores
            # the value (later replays are skipped by the body comparison).
            if raw.created_at > horizon:
                rejected.append(
                    RejectedEntry(
                        entry_id=raw.entry_id,
                        reason=f"created_at {raw.created_at.isoformat()} is implausibly far "
                        "in the future (beyond wall clock + tolerance)",
                        created_at=raw.created_at,
                    )
                )
                continue
            if window > horizon:
                logger.warning(
                    "eln entry %s reports an amendment at %s, beyond the wall clock: ingesting it, "
                    "but the sync cursor stays at %s and this entry is re-fetched every run until "
                    "the source is corrected",
                    raw.entry_id,
                    window.isoformat(),
                    cursor.isoformat(),
                )
            else:
                cursor = max(cursor, window)
            try:
                reaction = adapter.map_to_ord(raw)
                record = record_from_ord_reaction(reaction)
                if raw.created_at <= since:
                    # A replayed entry: what is stored decides whether anything is new, including
                    # in-place amendments (which keep `created_at`). Loaded lazily, once per run,
                    # keyed on this batch's ids.
                    if stored is None:
                        replayed = _replay_record_ids(adapter, entries, since)
                        stored = await record_store.bodies(replayed, source)
                        # A withdrawal is re-exported with an unchanged body, so the body cannot
                        # decide alone; withdrawal state is fetched the same lazy, id-keyed way
                        # (`066`'s partial index).
                        withdrawn = await record_store.retracted(
                            [(source, one) for one in replayed]
                        )
                    if stored.get(record.reaction_id) == record.body and (
                        (source, record.reaction_id) in withdrawn
                    ) == (raw.retracted_at is not None):
                        # Byte-identical body and the same withdrawal state: nothing to write.
                        # Anything else falls through and overwrites the record, which is what an
                        # amendment, a retraction or a re-publication is.
                        skipped_existing.append(raw.entry_id)
                        continue
                await ingest_reaction(
                    reaction,
                    reaction_store,
                    molecule_store,
                    record_store,
                    label_index=label_index,
                    source=source,
                    retracted_at=raw.retracted_at,
                )
            except (ChemclawError, ValidationError) as exc:
                # The shared bad-data base covers any per-entry failure (mapping, validation, an
                # uncomputable fingerprint). pydantic's `ValidationError` is a sibling `ValueError`,
                # not a `ChemclawError`, so it is caught alongside.
                rejected.append(
                    RejectedEntry(entry_id=raw.entry_id, reason=str(exc), created_at=raw.created_at)
                )
                continue
            ingested.append(raw.entry_id)
            if reaction.tier is RecordTier.CITATION_ONLY:
                citation_only.append(raw.entry_id)
    # Logged as well as returned, so a scheduled run is visible without opening the workflow result.
    # `source` distinguishes multiple ELNs; `fetched` separates "up to date" from "source answered
    # nothing"; `next_cursor` shows a wedge as a cursor standing still.
    _record_pass(
        source,
        ingested=len(ingested),
        citation_only=len(citation_only),
        rejected=len(rejected),
        skipped_existing=len(skipped_existing),
        fetched=len(entries),
        next_cursor=cursor,
        duration_s=time.perf_counter() - started,
    )
    for entry in rejected:
        # A rejection inside the overlap window was already warned about when first seen, so it logs
        # at DEBUG; future-stamped entries stay WARNING every run.
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
        citation_only=citation_only,
        skipped_existing=skipped_existing,
        rejected=rejected,
        next_cursor=cursor,
    )


def _record_pass(
    source: str,
    *,
    ingested: int,
    citation_only: int,
    rejected: int,
    skipped_existing: int,
    fetched: int,
    next_cursor: datetime,
    duration_s: float,
) -> None:
    """Emit the one record a sync pass leaves behind, and tally what it did to the corpus.

    One structured `log_event` per pass so the numbers can be filtered and aggregated. Outcomes
    partition what was fetched: `ingested` wrote a record, `rejected` is bad data the cursor
    advances past, `skipped` is an unchanged replay. `citation_only` is the part of `ingested` in
    that tier, on its own series.
    """
    for outcome, count in (
        ("ingested", ingested),
        ("rejected", rejected),
        ("skipped", skipped_existing),
    ):
        _count_records(source, outcome, count)
    if citation_only:
        record_metric(
            lambda m: m.increment(
                "chemclaw_ingest_citation_only_total", citation_only, {"source": source}
            )
        )
    log_event(
        logger,
        "ingest.finished",
        "%s: fetched=%d ingested=%d rejected=%d skipped_existing=%d citation_only=%d in %.3fs",
        source,
        fetched,
        ingested,
        rejected,
        skipped_existing,
        citation_only,
        duration_s,
        source=source,
        fetched=fetched,
        ingested=ingested,
        citation_only=citation_only,
        rejected=rejected,
        skipped_existing=skipped_existing,
        next_cursor=next_cursor.isoformat(),
        duration_s=round(duration_s, 3),
    )


def _count_records(source: str, outcome: str, count: int) -> None:
    """Add `count` to this source's tally of one outcome (a named function; see `sync.py`'s)."""
    record_metric(
        lambda m: m.increment(
            "chemclaw_ingest_records_total", count, {"source": source, "outcome": outcome}
        )
    )


def _replay_record_ids(adapter: ElnAdapter, entries: list[RawEntry], since: datetime) -> list[str]:
    """The record ids the replay-window entries of this batch map to.

    A record id is not an entry id: a warehouse binding may key the fetch and the reaction on
    different columns, and looking up by entry id would silently disable the replay skip. Mapping
    failures are ignored here; the main loop records the rejection once.
    """
    ids: list[str] = []
    for raw in entries:
        if raw.created_at > since:
            continue
        try:
            ids.append(adapter.map_to_ord(raw).reaction_id)
        except (ChemclawError, ValidationError):
            continue
    return ids


def _fetch_floor(since: datetime) -> datetime:
    """`since` minus the configured overlap window (clamped at the epoch floor).

    Files can arrive out of event-time order (an export retry drops an older-stamped file late), and
    a strict `>= since` fetch would lose them.
    """
    overlap = timedelta(seconds=settings.eln_sync_overlap_seconds)
    epoch = datetime.min.replace(tzinfo=UTC)
    return since - overlap if since - epoch > overlap else epoch
