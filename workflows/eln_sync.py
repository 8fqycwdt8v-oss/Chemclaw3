"""Durable ELN sync (plan step 4.5): fetch → validate → index → PR-gate, on the bg queue.

A thin Temporal wrapper over `eln.sync.sync_entries`: the activity wires the production
adapter, fingerprint stores, and note submitter and does all the I/O (ELN read, DB writes,
git push); the workflow invokes it with the high-water cursor. It runs on the
`background-jobs` queue (light, periodic work), and a Temporal Schedule drives it
(`scripts/schedules.py`). The sync is **self-cursoring and per-source**: each active ingest
source carries its own cursor in `sync_cursors` (keyed by the registry source name). A
scheduled run (no `since`) loads each source's cursor, syncs from it, and stores the advanced
value — so two ingest sources whose newest entries differ never let one skip the other's
lagging entries (the per-source cursor fix, D-054). An explicit `since` (a manual backfill)
runs every source from that point and does not touch any stored cursor. Each source is
drained in bounded, heartbeating chunks (`eln_sync_batch_size` new entries per activity
attempt, cursor persisted per chunk), so an arbitrarily large backlog makes durable forward
progress instead of wedging one over-window attempt forever. Factories are module-level so
tests swap them for in-memory stores and a fake submitter.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

    from chemclaw.config import settings
    from chemclaw.errors import ChemclawError
    from eln.adapter import RawEntry
    from eln.cursor import load_cursor, store_cursor
    from eln.ord import OrdReaction
    from eln.sync import IngestSummary, RejectedEntry, sync_entries
    from kg.git_submitter import default_submitter
    from mcp_servers.fpstore import default_molecule_store, default_reaction_store
    from sources.base import IngestHalf
    from sources.registry import active_ingest_source_names, make_data_source

from workflows.publish import BAD_DATA_RETRY

# Module-level indirection so tests swap the production stores for in-memory ones.
_reaction_store = default_reaction_store
_molecule_store = default_molecule_store


def _merge(summaries: list[IngestSummary], floor: datetime) -> IngestSummary:
    """Fold the per-source summaries into one combined report for the workflow's return value.

    Each active ingest source is synced and cursored independently; this only combines their
    outcomes so a single `IngestSummary` describes the whole run. `next_cursor` is the max seen and
    is informational — the real cursors are stored per source — falling back to `floor` (the run's
    `since`) when no source ran.
    """
    ingested: list[str] = []
    rejected: list[RejectedEntry] = []
    cursors: list[datetime] = []
    for summary in summaries:
        ingested.extend(summary.ingested)
        rejected.extend(summary.rejected)
        cursors.append(summary.next_cursor)
    return IngestSummary(
        ingested=ingested, rejected=rejected, next_cursor=max(cursors, default=floor)
    )


@activity.defn
async def list_ingest_sources() -> list[str]:
    """Return the active ingest source names — the set the workflow syncs and cursors per source."""
    return active_ingest_source_names()


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

    Entries at or before the run's cursor (`since`) — the overlap window's idempotent re-ingest —
    pass through uncapped: they are cheap re-writes and never advance the cursor. Entries after it
    are sorted oldest-first and truncated to `limit`, so one activity attempt does a bounded amount
    of PR-gate work no matter how large the backlog, and `truncated` tells the workflow to come
    back for the rest with the advanced cursor. Because the cap applies only past `since`, every
    kept chunk that was truncated strictly advances the cursor — the loop always makes progress.
    """

    def __init__(self, inner: IngestHalf, since: datetime, limit: int) -> None:
        self._inner = inner
        self._since = since
        self._limit = limit
        self.truncated = False

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Fetch from the wrapped adapter: the overlap plus the oldest `limit` new entries."""
        entries = sorted(
            await self._inner.fetch_new_entries(since),
            key=lambda entry: (entry.created_at, entry.entry_id),
        )
        overlap = [entry for entry in entries if entry.created_at <= self._since]
        new = [entry for entry in entries if entry.created_at > self._since]
        self.truncated = len(new) > self._limit
        return overlap + new[: self._limit]

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Delegate mapping unchanged — bounding is purely a fetch concern."""
        return self._inner.map_to_ord(raw)


async def _heartbeat_forever() -> None:
    """Beat until cancelled, several times per heartbeat-timeout window (the usual margin).

    The sync activity's real work happens inside `sync_entries`, which this layer must not modify
    (the loop is backend-agnostic core, G6) — so liveness is time-based: a sibling task beats while
    the sync runs, letting Temporal detect a dead worker within `eln_sync_heartbeat_timeout_seconds`
    instead of waiting out the whole start-to-close.
    """
    # A third of the timeout is the conventional margin: two beats may be lost to scheduling or
    # network delay before Temporal wrongly declares the worker dead.
    interval = settings.eln_sync_heartbeat_timeout_seconds / 3
    while True:
        activity.heartbeat()
        await asyncio.sleep(interval)


@activity.defn
async def sync_eln_entries(source: str, since: datetime) -> SyncChunk:
    """Ingest a bounded chunk of entries newer than `since` from the one named ingest source.

    Bounded (`eln_sync_batch_size`) and heartbeating, so a large backlog can neither blow the
    activity's start-to-close window in one giant attempt nor hide a dead worker until it lapses.
    """
    data_source = make_data_source(source)
    ingest = data_source.ingest
    if ingest is None:  # names come from the ingest-filtered set, so this is a wiring bug
        raise ChemclawError(f"data source {source!r} has no ingest half")
    bounded = _BoundedIngest(ingest, since, settings.eln_sync_batch_size)
    # First beat immediately (a fast sync may finish before the sibling task is ever scheduled),
    # then the task keeps beating for as long as the chunk actually takes.
    activity.heartbeat()
    heartbeater = asyncio.create_task(_heartbeat_forever())
    try:
        summary = await sync_entries(
            bounded, _reaction_store(), _molecule_store(), default_submitter(), since
        )
    finally:
        heartbeater.cancel()
    return SyncChunk(summary=summary, has_more=bounded.truncated)


@activity.defn
async def load_sync_cursor(source: str) -> datetime:
    """Return the persisted high-water cursor for `source` (epoch if it has never synced)."""
    return await load_cursor(source)


@activity.defn
async def store_sync_cursor(source: str, cursor: datetime) -> None:
    """Persist the advanced high-water cursor for `source` after a scheduled run."""
    await store_cursor(source, cursor)


@workflow.defn
class ElnSyncWorkflow:
    """Run one ELN sync durably, returning what was ingested across every active ingest source.

    Scheduled runs pass no `since`: for each active ingest source the workflow loads its stored
    cursor, syncs, and stores the advanced one — so consecutive firings never re-do or skip work,
    and each source advances on its own timeline. A manual run may pass an explicit `since` to
    backfill every source from a chosen point without disturbing any stored cursor.
    """

    @workflow.run
    async def run(self, since: datetime | None = None) -> IngestSummary:
        """Sync each active source from its cursor (or `since`); advance cursors when scheduled.

        Each source is synced in bounded chunks (`eln_sync_batch_size` new entries per activity
        attempt), the cursor advancing — and, when scheduled, being persisted — after every chunk.
        A large backfill therefore makes durable forward progress chunk by chunk instead of
        retrying one over-window batch forever.
        """
        activity_timeout = timedelta(seconds=settings.eln_sync_timeout_seconds)
        sources: list[str] = await workflow.execute_activity(
            list_ingest_sources,
            start_to_close_timeout=activity_timeout,
            retry_policy=BAD_DATA_RETRY,
        )
        summaries: list[IngestSummary] = []
        for source in sources:
            # Scheduled (no `since`): resume from this source's own cursor. Manual backfill: run
            # every source from the explicit `since` and leave the stored cursors untouched.
            if since is None:
                source_since = await workflow.execute_activity(
                    load_sync_cursor,
                    source,
                    start_to_close_timeout=activity_timeout,
                    retry_policy=BAD_DATA_RETRY,
                )
            else:
                source_since = since
            while True:
                chunk: SyncChunk = await workflow.execute_activity(
                    sync_eln_entries,
                    args=[source, source_since],
                    start_to_close_timeout=activity_timeout,
                    heartbeat_timeout=timedelta(
                        seconds=settings.eln_sync_heartbeat_timeout_seconds
                    ),
                    # Bad data must reject-and-continue inside the sync, never retry the batch.
                    retry_policy=BAD_DATA_RETRY,
                )
                summaries.append(chunk.summary)
                if since is None:
                    await workflow.execute_activity(
                        store_sync_cursor,
                        args=[source, chunk.summary.next_cursor],
                        start_to_close_timeout=activity_timeout,
                        retry_policy=BAD_DATA_RETRY,
                    )
                if not chunk.has_more:
                    break
                if chunk.summary.next_cursor <= source_since:
                    # Unreachable with a well-behaved adapter (a truncated chunk always advances
                    # the cursor), but a buggy source must wedge one run with a warning, not
                    # spin this loop — and Temporal's event history — forever.
                    workflow.logger.warning(
                        "eln sync for %s reported more entries but no cursor advance; stopping",
                        source,
                    )
                    break
                source_since = chunk.summary.next_cursor
        floor = since if since is not None else datetime.min.replace(tzinfo=UTC)
        return _merge(summaries, floor)
