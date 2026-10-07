"""Durable ELN sync: fetch → validate → index → transcribe, on the `background-jobs` queue.

A thin Temporal wrapper over `chemclaw.ingest.eln.sync.sync_entries`, driven by a Schedule. Each
active ingest source keeps its own cursor in `sync_cursors`, so sources never skip each
other's lagging entries (D-054). A scheduled run loads, syncs from and stores each cursor; an
explicit `since` (manual backfill) runs every source from that point and stores nothing.

Each source drains in bounded, heartbeating chunks (`eln_sync_batch_size`), persisting the
cursor per chunk; only the first chunk reaches into the late-file overlap window. A run
continues as new after `eln_sync_max_iterations` chunks so history stays bounded. Factories are
module-level so tests swap them for in-memory stores.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.exceptions import ActivityError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

    from chemclaw.core.config import settings
    from chemclaw.core.errors import ChemclawError
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.eln.adapter import (
        RawEntry,
        accepts_a_late_arrival_switch,
        accepts_a_limit,
        entry_window,
        fetch_was_truncated,
    )
    from chemclaw.ingest.eln.cursor import load_cursor, store_cursor
    from chemclaw.ingest.eln.ord import OrdReaction
    from chemclaw.ingest.eln.records import default_record_store
    from chemclaw.ingest.eln.sync import IngestSummary, sync_entries
    from chemclaw.ingest.rejections import forget_refusals, record_refusals
    from chemclaw.ingest.sources.base import IngestHalf
    from chemclaw.ingest.sources.registry import active_ingest_source_names, make_data_source
    from chemclaw.science.fingerprints.store import default_molecule_store, default_reaction_store
    from chemclaw.science.labels.store import default_label_index

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

# Module-level indirection so tests swap the production stores for in-memory ones.
_reaction_store = default_reaction_store
_molecule_store = default_molecule_store
_label_index = default_label_index
_record_store = default_record_store


class ElnSyncOutcome(BaseModel):
    """What one drain did, in counters — the shape that survives `continue_as_new`.

    **Counters, not the per-chunk `IngestSummary`.** That model carries one entry id per ingested
    and per skipped entry, which is exactly right for one chunk and impossible to carry across a
    chain of runs: a backfill large enough to need `continue_as_new` is a backfill whose id lists
    outgrow Temporal's payload limit, so the thing that bounds the history would be defeated by
    the thing it hands forward. Nothing is lost that an operator can reach: `sync_entries` already
    logs every rejection with its reason at WARNING, and the counts are what the CLI printed.

    `next_cursor` is the max seen across sources and is informational — the real cursors are stored
    per source — falling back to the run's `since` when no source ran.
    """

    ingested: int = 0
    # The part of `ingested` stored citation-only — citable, and in no structure index
    # (`IngestSummary.citation_only`).
    citation_only: int = 0
    skipped_existing: int = 0
    # Reported separately because a run that ingests thousands and rejects thousands is a broken
    # source reporting healthy progress, and one total cannot say so.
    rejected: int = 0
    # Sources this run could not drain at all, named so an operator knows which. Bounded by the
    # source list.
    failed_sources: list[str] = []
    next_cursor: datetime


class ElnSyncPlan(BaseModel):
    """The two live values one drain is fixed to, read once and recorded in history."""

    sources: list[str]
    # Read in the activity: it decides the command count, so a replay must see the recorded value.
    max_iterations: int


class ElnSyncState(BaseModel):
    """A run's position, carried across `continue_as_new` so a huge backfill drains over many runs.

    Every field is bounded: source names, one cursor, one flag and five numbers. That is the whole
    reason the counters above exist.
    """

    max_iterations: int
    # Sources still to drain; the first is the one in progress.
    remaining: list[str]
    # The manual-backfill floor. `None` is the scheduled path, which reads and writes a stored
    # cursor per source instead.
    since: datetime | None = None
    # The cursor within the source in progress, carried so a continued run resumes mid-source.
    source_since: datetime | None = None
    # The overlap window is re-checked once per run chain, so this stays False across
    # `continue_as_new`.
    apply_overlap: bool = True
    ingested: int = 0
    citation_only: int = 0
    skipped_existing: int = 0
    rejected: int = 0
    # Sources abandoned mid-run because they could not be reached; carried so a continued run
    # reports the whole chain's failures rather than only the last leg's.
    failed_sources: list[str] = []
    next_cursor: datetime | None = None


def _absorb(state: ElnSyncState, summary: IngestSummary) -> None:
    """Fold one chunk's summary into the drain's carried counters (max cursor, summed counts)."""
    state.ingested += len(summary.ingested)
    state.citation_only += len(summary.citation_only)
    state.skipped_existing += len(summary.skipped_existing)
    state.rejected += len(summary.rejected)
    state.next_cursor = (
        summary.next_cursor
        if state.next_cursor is None
        else max(state.next_cursor, summary.next_cursor)
    )


@durable_activity("background")
@activity.defn
async def plan_eln_sync() -> ElnSyncPlan:
    """Name the active ingest sources and fix the run chain's iteration bound.

    Both are live reads, so they belong in an activity — see `ElnSyncPlan`.
    """
    return ElnSyncPlan(
        sources=active_ingest_source_names(),
        max_iterations=settings.eln_sync_max_iterations,
    )


class SyncChunk(BaseModel):
    """One bounded sync attempt's outcome: the summary, plus whether newer entries remain.

    `has_more` is what lets the workflow loop chunk by chunk instead of the activity ingesting an
    unbounded backlog in one attempt — the failure mode where a large first backfill can never fit
    the start-to-close window and the scheduled sync wedges with zero forward progress.
    """

    summary: IngestSummary
    has_more: bool


class _BoundedIngest:
    """An `ElnAdapter` wrapper that caps how many *new* entries one sync attempt sees.

    Entries at or before `since` (the overlap re-ingest) pass uncapped and never advance the cursor.
    Entries after it are sorted oldest-first and truncated to `limit`, so each attempt is bounded
    and
    every truncated chunk strictly advances the cursor. A source's own truncation
    (`fetch_was_truncated`) also sets `truncated`, so a short page means "come back" rather than
    "nothing new".
    """

    def __init__(
        self, inner: IngestHalf, since: datetime, limit: int, *, first_chunk: bool = True
    ) -> None:
        self._inner = inner
        self._since = since
        self._limit = limit
        self._first_chunk = first_chunk
        self.truncated = False

    async def _fetch(self, since: datetime, limit: int | None) -> list[RawEntry]:
        """Ask the wrapped adapter for entries, offering each capability only if it takes it.

        An out-of-tree adapter written to the one-argument `fetch_new_entries` signature must not
        get
        arguments it never declared; `accepts_a_limit` and `accepts_a_late_arrival_switch` are the
        probes. The late-arrival switch is sent only as `False`, since `True` is the default
        behaviour.
        """
        extra: dict[str, Any] = {}
        if limit is not None and accepts_a_limit(self._inner):
            extra["limit"] = limit
        if not self._first_chunk and accepts_a_late_arrival_switch(self._inner):
            extra["report_late_arrivals"] = False
        return await self._inner.fetch_new_entries(since, **extra)

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Fetch from the wrapped adapter: the overlap plus the oldest `limit` new entries.

        Ordered, split and truncated on `entry_window` (the later of creation and amendment), the
        same
        timestamp the stored cursor uses, so the cursor never advances past an entry the cap
        dropped.

        The bound is also offered to the source, so it can `LIMIT` its own read, but only when
        `since >= self._since` (no overlap rewind): on the first chunk, a limit applied at the
        overlap
        floor could be spent entirely on already-ingested entries and stall the cursor.
        """
        bounded = since >= self._since
        entries = sorted(
            await self._fetch(since, self._limit if bounded else None),
            key=lambda entry: (
                entry_window(entry.created_at, entry.modified_at, entry.retracted_at),
                entry.entry_id,
            ),
        )
        overlap = [
            entry
            for entry in entries
            if entry_window(entry.created_at, entry.modified_at, entry.retracted_at) <= self._since
        ]
        new = [
            entry
            for entry in entries
            if entry_window(entry.created_at, entry.modified_at, entry.retracted_at) > self._since
        ]
        self.truncated = len(new) > self._limit or fetch_was_truncated(self._inner)
        return overlap + new[: self._limit]

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Delegate mapping unchanged — bounding is purely a fetch concern."""
        return self._inner.map_to_ord(raw)


# `sync_entries` is backend-agnostic core and has no progress hooks, so liveness is time-based via
# `durable.heartbeat.beating` (one-second floor on the interval). The eager pre-beat at the call
# site covers a sync shorter than one interval.


@durable_activity("background")
@activity.defn
async def sync_eln_entries(source: str, since: datetime, apply_overlap: bool = True) -> SyncChunk:
    """Ingest a bounded chunk of entries newer than `since` from the one named ingest source.

    Bounded (`eln_sync_batch_size`) and heartbeating. `apply_overlap` is True only for a run's
    first chunk, so later chunks fetch from the advancing cursor instead of replaying the window.

    Every record this chunk refused is written to the rejection ledger from `summary.rejected`,
    the exact set this chunk processed and refused — the cursor is already past those entries, so
    a missed row would be permanent.
    """
    data_source = make_data_source(source)
    ingest = data_source.ingest
    if ingest is None:  # names come from the ingest-filtered set, so this is a wiring bug
        raise ChemclawError(f"data source {source!r} has no ingest half")
    # Only the first chunk reaches behind the cursor, so only it can judge late arrivals; on a
    # continuation chunk this run itself ingested everything in between.
    bounded = _BoundedIngest(ingest, since, settings.eln_sync_batch_size, first_chunk=apply_overlap)
    # First beat immediately (a fast sync may finish before `beating()`'s first interval elapses),
    # then it keeps beating for as long as the chunk actually takes.
    activity.heartbeat()
    summary = await beating(
        sync_entries(
            bounded,
            _reaction_store(),
            _molecule_store(),
            _record_store(),
            since,
            label_index=_label_index(),
            source=source,
            apply_overlap=apply_overlap,
        ),
        f"eln sync {source}",
        settings.eln_sync_heartbeat_timeout_seconds,
    )
    # Never raises. Kept out of `sync_entries` so that loop stays I/O-free with injected
    # dependencies.
    await record_refusals(source, {entry.entry_id: entry.reason for entry in summary.rejected})
    # An entry this chunk stored is no longer refused, whatever refused it before.
    await forget_refusals(source, summary.ingested)
    return SyncChunk(summary=summary, has_more=bounded.truncated)


@durable_activity("background")
@activity.defn
async def load_sync_cursor(source: str) -> datetime:
    """Return the persisted high-water cursor for `source` (epoch if it has never synced)."""
    return await load_cursor(source)


@durable_activity("background")
@activity.defn
async def store_sync_cursor(source: str, cursor: datetime) -> None:
    """Persist the advanced high-water cursor for `source` after a scheduled run."""
    await store_cursor(source, cursor)


@durable_workflow("background")
# Declared (unlike `DocumentShareSyncWorkflow`) because `cli.live_data.backfill` awaits the
# result with no execution timeout: a parked run would block it forever. Failing costs at most
# the chunk in flight, since every chunk persists its cursor.
@workflow.defn(failure_exception_types=[Exception])
class ElnSyncWorkflow:
    """Run one ELN sync durably, returning what was ingested across every active ingest source.

    Scheduled runs pass no `since` and advance each source's stored cursor independently; a manual
    run may pass `since` to backfill every source without touching any stored cursor.
    """

    @workflow.run
    async def run(
        self, since: datetime | None = None, state: ElnSyncState | None = None
    ) -> ElnSyncOutcome:
        """Sync each active source from its cursor (or `since`); advance cursors when scheduled.

        Each source syncs in bounded chunks, the cursor advancing (and, when scheduled, persisted)
        after each. After `eln_sync_max_iterations` chunks the run continues as new, so history
        length
        depends on the bound rather than the backlog.

        `state` is passed only by `continue_as_new`; a scheduled or manual run passes nothing.
        """
        activity_timeout = timedelta(seconds=settings.eln_sync_timeout_seconds)
        if state is None:
            plan: ElnSyncPlan = await workflow.execute_activity(
                plan_eln_sync,
                start_to_close_timeout=activity_timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            state = ElnSyncState(
                max_iterations=plan.max_iterations, remaining=plan.sources, since=since
            )
        iterations = 0
        while state.remaining:
            source = state.remaining[0]
            if state.source_since is None:
                # Scheduled (no `since`): resume from this source's own cursor. Manual backfill:
                # run every source from the explicit `since` and leave the stored cursors alone.
                if state.since is None:
                    state.source_since = await workflow.execute_activity(
                        load_sync_cursor,
                        source,
                        start_to_close_timeout=activity_timeout,
                        schedule_to_start_timeout=queue_wait_timeout(),
                        retry_policy=BAD_DATA_RETRY,
                    )
                else:
                    state.source_since = state.since
            try:
                chunk: SyncChunk = await workflow.execute_activity(
                    sync_eln_entries,
                    args=[source, state.source_since, state.apply_overlap],
                    start_to_close_timeout=activity_timeout,
                    schedule_to_start_timeout=queue_wait_timeout(),
                    heartbeat_timeout=timedelta(
                        seconds=settings.eln_sync_heartbeat_timeout_seconds
                    ),
                    # Bad data must reject-and-continue inside the sync, never retry the batch.
                    retry_policy=BAD_DATA_RETRY,
                )
            except ActivityError as exc:
                # One source's failure is not the run's: sources have separate cursors and backends,
                # so the rest
                # still sync. A workflow cancellation arrives as
                # `ActivityError(cause=CancelledError)` and is
                # re-raised so the run ends CANCELLED. Bad data never reaches here; it is rejected
                # inside
                # `sync_entries`.
                if is_cancelled_exception(exc):
                    raise
                # Dropped rather than retried in-loop: its cursor is untouched, so the next run
                # resumes it.
                workflow.logger.warning("eln sync for %s failed; skipping it: %s", source, exc)
                state.failed_sources.append(source)
                state.remaining = state.remaining[1:]
                state.source_since = None
                state.apply_overlap = True
                iterations += 1
                # The same iteration bound the tail applies, repeated because `continue` below is
                # what skips a tail that needs a `chunk` this branch does not have.
                if state.remaining and iterations >= state.max_iterations:
                    workflow.continue_as_new(args=[since, state])
                continue
            # Only the first chunk of a drain reaches behind the cursor for late files; later chunks
            # (including continued runs) fetch from the advancing cursor.
            state.apply_overlap = False
            _absorb(state, chunk.summary)
            iterations += 1
            if state.since is None:
                await workflow.execute_activity(
                    store_sync_cursor,
                    args=[source, chunk.summary.next_cursor],
                    start_to_close_timeout=activity_timeout,
                    schedule_to_start_timeout=queue_wait_timeout(),
                    retry_policy=BAD_DATA_RETRY,
                )
            if chunk.has_more and chunk.summary.next_cursor > state.source_since:
                state.source_since = chunk.summary.next_cursor
            else:
                if chunk.has_more:
                    # Unreachable with a well-behaved adapter; a buggy source stops with a warning
                    # rather than
                    # looping forever.
                    workflow.logger.warning(
                        "eln sync for %s reported more entries but no cursor advance; stopping",
                        source,
                    )
                # This source is drained: move to the next one, from its own cursor.
                state.remaining = state.remaining[1:]
                state.source_since = None
                state.apply_overlap = True
            if state.remaining and iterations >= state.max_iterations:
                # The carried state is bounded by construction — source names, one cursor, one
                # flag, five counters — so unlike the document drain there is nothing to compact.
                workflow.continue_as_new(args=[since, state])
        # `state.since` rather than the parameter: a continued run is handed both, and the state
        # is the one that is true for the whole chain by construction.
        floor = state.since if state.since is not None else datetime.min.replace(tzinfo=UTC)
        return ElnSyncOutcome(
            ingested=state.ingested,
            citation_only=state.citation_only,
            skipped_existing=state.skipped_existing,
            rejected=state.rejected,
            failed_sources=state.failed_sources,
            next_cursor=state.next_cursor if state.next_cursor is not None else floor,
        )
