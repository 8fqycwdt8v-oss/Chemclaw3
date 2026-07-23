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
runs every source from that point and does not touch any stored cursor. Factories are
module-level so tests swap them for in-memory stores and a fake submitter.
"""

from datetime import UTC, datetime, timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from chemclaw.errors import ChemclawError
    from eln.cursor import load_cursor, store_cursor
    from eln.sync import IngestSummary, RejectedEntry, sync_entries
    from kg.git_submitter import default_submitter
    from mcp_servers.fpstore import default_molecule_store, default_reaction_store
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


@activity.defn
async def sync_eln_entries(source: str, since: datetime) -> IngestSummary:
    """Ingest every entry newer than `since` from the one named ingest source."""
    data_source = make_data_source(source)
    ingest = data_source.ingest
    if ingest is None:  # names come from the ingest-filtered set, so this is a wiring bug
        raise ChemclawError(f"data source {source!r} has no ingest half")
    return await sync_entries(
        ingest, _reaction_store(), _molecule_store(), default_submitter(), since
    )


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
        """Sync each active source from its cursor (or `since`); advance cursors when scheduled."""
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
            summary = await workflow.execute_activity(
                sync_eln_entries,
                args=[source, source_since],
                start_to_close_timeout=activity_timeout,
                # Bad data must reject-and-continue inside the sync, never retry the batch.
                retry_policy=BAD_DATA_RETRY,
            )
            if since is None:
                await workflow.execute_activity(
                    store_sync_cursor,
                    args=[source, summary.next_cursor],
                    start_to_close_timeout=activity_timeout,
                    retry_policy=BAD_DATA_RETRY,
                )
            summaries.append(summary)
        floor = since if since is not None else datetime.min.replace(tzinfo=UTC)
        return _merge(summaries, floor)
