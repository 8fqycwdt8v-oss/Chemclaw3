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
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.ingest.eln.adapter import (
    ElnAdapter,
    RawEntry,
    entry_window,
    fetch_retractions,
)
from chemclaw.ingest.eln.ingest import ingest_reaction
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import ReactionRecordStore
from chemclaw.science.fingerprints.store import FingerprintStore
from chemclaw.science.labels.store import LabelIndex

logger = logging.getLogger(__name__)

# External identifiers/messages cross a trust boundary when they reach the log: a CR/LF in
# an ELN entry id (or in an error message quoting one) could forge whole log lines.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# The one retraction refusal that is a *configuration fact* rather than an incident: the capability
# is optional, so most adapters simply do not have it and never will. Named here because the level
# it is logged at depends on being able to tell it from the other two, and a WARNING on every pass
# of every file-drop source is precisely how the two that *are* incidents get scrolled past.
_RETRACTIONS_UNSUPPORTED = "this adapter cannot report retractions"


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
    # How many stored runs this pass retired because the source reported them withdrawn. A count
    # rather than a list of ids, matching `SyncReport.pruned`: the store answers with the rows it
    # actually changed, and an already-retracted row is not one of them.
    retracted: int = 0
    # Why the retraction sweep did not run, empty when it did. **This is what keeps "retired
    # nothing" and "could not run" from being one number.** `retracted=0` with no refusal is a
    # source that reports withdrawals and had none; `retracted=0` with a refusal is a source that
    # said nothing this pass could act on, and the second must never be read as the first — it is
    # `prune_share`'s "an unreachable share and an empty one look identical", which is a sentence
    # about evidence rather than about shares.
    retraction_refusal: str = ""


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
    started = time.perf_counter()
    floor = _fetch_floor(since) if apply_overlap else since
    entries = await adapter.fetch_new_entries(floor)
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
    # **After the ingest, deliberately.** An ELN that soft-deletes keeps exporting the withdrawn
    # entry, so the same pass can both re-write its row and be told it is withdrawn; sweeping last
    # retires it in this run rather than the next. The other order is not wrong, only slower — the
    # upsert cannot clear a tombstone by construction (`records._UPSERT` says why).
    retired, refusal = await _sweep_retractions(adapter, record_store, floor, source)
    # The summary is a return value the scheduler stores; also log the outcome so an admin
    # running this under a Temporal Schedule sees it without opening the workflow result, and
    # gets a WARNING trail of exactly which entries were rejected and why.
    #
    # **`source` is the field this line most needed and did not have**, and it is a parameter of
    # this very function: with two ELN sources enabled, two identical `eln sync: ingested=…` lines
    # arrived per run and nothing said which was which. `fetched` and `duration_s` are the other
    # two the old line could not be read without — a pass that fetched 500 entries and ingested 0
    # is a corpus already up to date, while one that fetched 0 is a source that answered nothing,
    # and both used to render as `ingested=0`. `next_cursor` is here because the *cursor* is what
    # this run's progress actually is: the wedge this loop documents at length advances no cursor
    # while reporting `ingested=N rejected=0`, and this is the field that shows it standing still.
    _record_pass(
        source,
        ingested=len(ingested),
        rejected=len(rejected),
        skipped_existing=len(skipped_existing),
        fetched=len(entries),
        next_cursor=cursor,
        retracted=retired,
        retraction_refusal=refusal,
        duration_s=time.perf_counter() - started,
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
        retracted=retired,
        retraction_refusal=refusal,
    )


async def _sweep_retractions(
    adapter: ElnAdapter,
    record_store: ReactionRecordStore,
    since: datetime,
    source: str,
) -> tuple[int, str]:
    """Retire the runs `source` reports withdrawn since `since`; return how many, and any refusal.

    **A retraction is carried by the source and is never inferred from absence.** That is the
    single load-bearing property here, and it is why `ingest/documents/sync.py::prune_share` ports
    only in its refusals. The share's sweep is safe because a crawl is a *full enumeration*: "this
    run saw every file and did not see this row" is evidence. An ELN sync is a **delta** —
    `fetch_new_entries(since)` returns only what changed after the cursor — so "not seen this run"
    is the normal, permanent state of every entry ever ingested, and mark-and-sweep applied to it
    would retire the whole corpus on the first pass. Not a risk to be mitigated: an inapplicable
    mechanism. `tests/test_eln.py` pins it against a future session reintroducing it.

    Three ways a pass fails to be evidence of a withdrawal, all refusals, all `prune_share`'s
    translated to what this source can actually be asked:

    - **The adapter cannot report retractions** (`fetch_retractions` answers `None`). The share's
      "it saw no candidate files at all", and the same trap: a source that cannot express a
      withdrawal and one that reports none are identical from here, and only the second is
      evidence. Its corpus is never retired by this sweep, which is what makes the capability
      genuinely optional rather than a default-on behaviour every adapter has to opt out of.
    - **The report is not whole** (`complete=False`). The share's "the drain did not finish": a
      page limit, a partly-readable feed, one of several backing sources unavailable. Half a report
      is not a report. Note this is the *retraction* fetch's completeness, not the entry fetch's —
      `fetch_was_truncated` is about a different drain, and a delta sweep reads no entries as
      evidence, so the entry page being cut short says nothing either way.
    - **The report could not be fetched at all.** The share's failed root. `ChemclawError` and
      `OSError` are caught rather than every exception, for `sync_entries`' reason: an adapter's
      own error family and the transport failures under it are what "the source did not answer"
      looks like, and a `TypeError` in a binding is a defect that must not be filed as an outage.

    A retraction never advances the sync cursor, so this window is re-read on every pass and the
    same withdrawal is re-reported until the entry stream carries the cursor past it. That is
    deliberate and free: `retract` skips a row already retracted, so the earliest report wins, the
    count stays honest, and a future-stamped withdrawal cannot poison a cursor the way a
    future-stamped entry can.
    """
    try:
        report = await fetch_retractions(adapter, since)
    except (ChemclawError, OSError) as exc:
        return 0, f"the source could not be asked: {_log_safe(str(exc))}"
    if report is None:
        return 0, _RETRACTIONS_UNSUPPORTED
    if not report.complete:
        return 0, "the source's report of withdrawals was partial"
    if not report.retractions:
        return 0, ""
    retired = await record_store.retract(
        {item.entry_id: item.retracted_at for item in report.retractions}, source
    )
    if retired:
        logger.info(
            "%s: retired %d run(s) the source reported withdrawn; they stay readable by id and "
            "leave every current-evidence query",
            source,
            retired,
        )
    return retired, ""


def _record_pass(
    source: str,
    *,
    ingested: int,
    rejected: int,
    skipped_existing: int,
    fetched: int,
    next_cursor: datetime,
    retracted: int,
    retraction_refusal: str,
    duration_s: float,
) -> None:
    """Emit the one record a sync pass leaves behind, and tally what it did to the corpus.

    One line per pass — the volume rule the rejection trail below already follows, and the reason
    it is a `log_event` rather than a sentence is that every number here used to be text inside a
    message: an operator could grep it and could not filter or aggregate it, so "is this source
    still ingesting?" was a question no dashboard could answer.

    The outcomes partition what the fetch returned: `ingested` wrote a record, `rejected` is
    deterministic bad data the cursor advances past, `skipped` is an overlap replay whose stored
    body is byte-identical, so there was nothing to write. `retracted` is the one outcome that is
    *not* about the fetched entries at all — it counts runs the source reported withdrawn, which
    arrive through their own report precisely because an absence from the fetch means nothing.
    """
    if retraction_refusal:
        # `prune_share`'s wording, because it is the same argument about the same kind of evidence
        # — but not always its level: an adapter with no such capability is a deployment's shape,
        # and the summary field says so on every pass whether or not this line does.
        logger.log(
            logging.DEBUG if retraction_refusal == _RETRACTIONS_UNSUPPORTED else logging.WARNING,
            "%s: nothing was retired — %s. A source that cannot report a withdrawal and one with "
            "none to report look identical from here, and of the two mistakes only re-ingesting "
            "is recoverable",
            source,
            retraction_refusal,
        )
    for outcome, count in (
        ("ingested", ingested),
        ("rejected", rejected),
        ("skipped", skipped_existing),
        ("retracted", retracted),
    ):
        _count_records(source, outcome, count)
    log_event(
        logger,
        "ingest.finished",
        "%s: fetched=%d ingested=%d rejected=%d skipped_existing=%d retracted=%d in %.3fs",
        source,
        fetched,
        ingested,
        rejected,
        skipped_existing,
        retracted,
        duration_s,
        source=source,
        fetched=fetched,
        ingested=ingested,
        rejected=rejected,
        skipped_existing=skipped_existing,
        retracted=retracted,
        # Empty when the sweep ran. A field rather than a second event, so one line answers both
        # "did anything leave the corpus" and "was this pass able to ask".
        retraction_refusal=retraction_refusal,
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
