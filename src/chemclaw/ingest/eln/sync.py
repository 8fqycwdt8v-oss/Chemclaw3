"""Sync new ELN entries into the graph + fingerprint index (plan step 4.5, core).

The backend-agnostic sync loop: pull entries newer than a cursor from an adapter, map each
to the canonical schema, and ingest it. A single bad entry (unparseable ELN shape or a
reaction that fails validation) is recorded and skipped, never aborting the whole batch
(G4) — the summary says exactly what was ingested and what was rejected and why. Because
every write is idempotent, re-running from an earlier cursor is safe. Deps are injected, so
this whole flow is tested in-memory; `chemclaw.durable.eln_sync` wraps it as a Temporal activity
with production stores and adapter.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.adapter import ElnAdapter, RawEntry, entry_window
from chemclaw.ingest.eln.ingest import ingest_reaction
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import ReactionRecordStore
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

    The fetch deliberately reaches behind the cursor (`eln_sync_overlap_seconds`) so an
    export file that lands late with an older payload timestamp is still picked up —
    re-ingesting the window is free because every write is idempotent. The returned
    `next_cursor` is floored at `since`, so the overlap never regresses the stored cursor.

    `apply_overlap=False` fetches from `since` itself (still inclusive, per the adapter
    contract): the workflow's chunk loop reaches behind the cursor only on its first
    chunk, so draining a backlog does not re-fetch the whole overlap window per chunk.

    `label_index` and `source` are required keyword arguments, passed straight through to
    `ingest_reaction`, which explains why neither has a default: the label index's record phase
    can only be written from the canonical record in hand, and `source` is half of its row key.

    Overlap replay is cheap, not just idempotent: an overlap entry whose stored record carries the
    same body has nothing left to index or store, so it is skipped by one indexed lookup over the
    replayed ids. That lookup used to be a parse of every merged note in the corpus, which is what
    made this loop outgrow its own activity timeout at ~700k entries; it is now bounded by the
    page, not by the corpus.

    The skip covers the label row too, and correctly: a byte-identical body is a byte-identical
    canonical record, so its record phase is the row already stored. An *amended* body falls
    through and re-records, which is what re-derives the labels — `labeller_version` is left
    untouched by the record upsert only when the record smiles is unchanged.
    """
    entries = await adapter.fetch_new_entries(_fetch_floor(since) if apply_overlap else since)
    ingested: list[str] = []
    skipped_existing: list[str] = []
    rejected: list[RejectedEntry] = []
    stored: dict[str, str] | None = None
    cursor = since
    horizon = datetime.now(UTC) + timedelta(seconds=settings.eln_sync_future_tolerance_seconds)
    for raw in entries:
        # **The cursor advances on the timestamp the entry was *fetched* by**, which for a source
        # that reports amendments is the later of the two (`entry_window`) — the one definition of
        # "the timestamp an entry is filtered on", and until this the one place that did not use it.
        # Advancing on `created_at` alone wedges a source permanently: the fetch filters, orders and
        # truncates on the amendment watermark, so once more than one page of already-created rows
        # has been amended, every fetch returns that same page, the cursor never moves past it, and
        # reactions created afterwards are never ingested again. Nothing reports it — the batch is
        # not truncated by the *workflow's* reckoning either, so the wedge guard in
        # `durable/eln_sync.py` is never reached and the log reads `ingested=N rejected=0`.
        window = entry_window(raw.created_at, raw.modified_at)
        # **A timestamp beyond the wall clock costs the cursor, and only sometimes the entry.**
        # Nothing ever lowers a stored cursor, so an implausible value that became one would
        # silently skip every later real entry — that is the whole of what this guard is for.
        #
        # A *creation* stamp past the horizon says the record is not about anything that has
        # happened, so the entry is rejected and reported. An *amendment* stamp past it says
        # somebody typed a year wrong in a metadata field of an entry whose chemistry is real: the
        # earlier form of this guard checked `entry_window` and so rejected that entry outright,
        # and — because the fetch filters on the same watermark — re-fetched and re-rejected it on
        # every run, forever, costing the corpus a real experiment for a typo. So the entry ingests
        # and only the cursor refuses the value. It is re-fetched each run and, once its record is
        # stored, skipped by the body comparison below at the cost of one lookup.
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
                # The entry was seen before, so what is stored decides whether there is anything
                # new in it. An *amendment* arrives here too: an ELN corrects an entry in place and
                # `created_at` does not move, so a corrected entry is by definition an old one —
                # which is also why the adapter has to widen its fetch window (`entry_window`) or
                # this branch never sees it at all. Loaded lazily, once per run, and only when a
                # replay actually happened; keyed on the ids this batch holds, never on the corpus.
                if stored is None:
                    stored = await record_store.bodies(
                        _replay_record_ids(adapter, entries, since), source
                    )
                if stored.get(record.reaction_id) == record.body:
                    # Byte-identical to what is stored: nothing to index or write, so skip the
                    # whole ingest. A *different* body falls through and overwrites the record,
                    # which is what an amendment is — no versioning scheme and no review needed,
                    # because the transcription asserts nothing either way.
                    skipped_existing.append(raw.entry_id)
                    continue
            await ingest_reaction(
                reaction,
                reaction_store,
                molecule_store,
                record_store,
                label_index=label_index,
                source=source,
            )
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


def _replay_record_ids(adapter: ElnAdapter, entries: list[RawEntry], since: datetime) -> list[str]:
    """The record ids the replay-window entries of this batch map to.

    **A record id is not an entry id**, and assuming it was is a defect this function exists to
    prevent. `RawEntry.entry_id` is whatever the source keys its rows on; `OrdReaction.reaction_id`
    is a separately declared field — in a warehouse binding, literally two different columns:
    `ingest/eln/warehouse/binding.py` requires `reaction_id` in the reaction map, while `entry.key`
    names the fetch key. Looking the store up by entry id and reading the answer by record id
    misses on every source where the two differ, which costs nothing visible: the upsert is
    idempotent, so
    the run stays correct and simply re-ingests everything forever while `skipped_existing` reports
    nothing. A silently dead optimization is worse than an absent one.

    Mapping runs twice per replayed entry — here and in the loop — which is ~84 µs of pure
    function. A mapping failure is **ignored here on purpose**: the loop below has the one `except`
    that knows how to record a rejection, and reporting it twice (or reporting it from here, out of
    order) is how one bad entry ends up in the summary as two.
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

    Why: files arrive in the export directory decoupled from event-time order — an
    upstream export retry can drop an older-stamped file *after* a newer one was synced.
    A strictly `>= since` fetch would drop it forever; the bounded overlap re-fetches it.
    """
    overlap = timedelta(seconds=settings.eln_sync_overlap_seconds)
    epoch = datetime.min.replace(tzinfo=UTC)
    return since - overlap if since - epoch > overlap else epoch
