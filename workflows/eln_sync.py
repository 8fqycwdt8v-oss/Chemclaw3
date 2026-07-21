"""Durable ELN sync (plan step 4.5): fetch → validate → index → PR-gate, on the bg queue.

A thin Temporal wrapper over `eln.sync.sync_entries`: the activity wires the production
adapter, fingerprint stores, and note submitter and does all the I/O (ELN read, DB writes,
git push); the workflow invokes it with the high-water cursor. It runs on the
`background-jobs` queue (light, periodic work), and a Temporal Schedule drives it
(`scripts/schedules.py`). The sync is **self-cursoring**: when started with no `since` (the
scheduled case), it loads its high-water mark from `sync_cursors`, syncs from it, and stores
the advanced value — so the Schedule threads no state through its payload. An explicit
`since` (a manual backfill) runs from that point and does not touch the stored cursor.
Factories are module-level so tests swap them for in-memory stores and a fake submitter.
"""

from datetime import datetime, timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from eln.cursor import load_cursor, store_cursor
    from eln.registry import make_eln_adapter
    from eln.sync import IngestSummary, sync_entries
    from kg.git_submitter import default_submitter
    from mcp_servers.fpstore import default_molecule_store, default_reaction_store

from workflows.publish import BAD_DATA_RETRY

# Module-level indirection so tests swap the production stores for in-memory ones.
_reaction_store = default_reaction_store
_molecule_store = default_molecule_store


@activity.defn
async def sync_eln_entries(since: datetime) -> IngestSummary:
    """Ingest every ELN entry newer than `since`; return the summary + next cursor."""
    adapter = make_eln_adapter(settings.eln_sync_adapter)
    return await sync_entries(
        adapter, _reaction_store(), _molecule_store(), default_submitter(), since
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
    """Run one ELN sync durably, returning what was ingested and the next cursor.

    Scheduled runs pass no `since`: the workflow loads the stored cursor, syncs, and stores
    the advanced one, so consecutive firings never re-do or skip work. A manual run may pass
    an explicit `since` to backfill from a chosen point without disturbing the stored cursor.
    """

    @workflow.run
    async def run(self, since: datetime | None = None) -> IngestSummary:
        """Sync from `since` or the stored cursor; advance the stored cursor when scheduled."""
        activity_timeout = timedelta(seconds=settings.eln_sync_timeout_seconds)
        scheduled = since is None
        source = settings.eln_sync_adapter
        if since is None:
            since = await workflow.execute_activity(
                load_sync_cursor,
                source,
                start_to_close_timeout=activity_timeout,
                retry_policy=BAD_DATA_RETRY,
            )
        summary = await workflow.execute_activity(
            sync_eln_entries,
            since,
            start_to_close_timeout=activity_timeout,
            # Bad data must reject-and-continue inside the sync, never retry the batch.
            retry_policy=BAD_DATA_RETRY,
        )
        # Only a scheduled (cursor-driven) run advances the stored high-water mark; a manual
        # backfill from an explicit `since` leaves the cursor where the scheduled runs put it.
        if scheduled:
            await workflow.execute_activity(
                store_sync_cursor,
                args=[source, summary.next_cursor],
                start_to_close_timeout=activity_timeout,
                retry_policy=BAD_DATA_RETRY,
            )
        return summary
