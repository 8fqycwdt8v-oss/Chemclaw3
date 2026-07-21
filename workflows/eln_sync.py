"""Durable ELN sync (plan step 4.5): fetch → validate → index → PR-gate, on the bg queue.

A thin Temporal wrapper over `eln.sync.sync_entries`: the activity wires the production
adapter, fingerprint stores, and note submitter and does all the I/O (ELN read, DB writes,
git push); the workflow just invokes it with the high-water cursor. It runs on the
`background-jobs` queue (light, periodic work), and a Temporal Schedule drives it, passing
the previous run's `next_cursor` back as `since`. Factories are module-level so tests swap
them for in-memory stores and a fake submitter.
"""

from datetime import datetime, timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
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


@workflow.defn
class ElnSyncWorkflow:
    """Run one ELN sync durably, returning what was ingested and the next cursor."""

    @workflow.run
    async def run(self, since: datetime) -> IngestSummary:
        """Invoke the sync activity with the high-water cursor."""
        return await workflow.execute_activity(
            sync_eln_entries,
            since,
            start_to_close_timeout=timedelta(seconds=settings.eln_sync_timeout_seconds),
            # Bad data must reject-and-continue inside the sync, never retry the batch.
            retry_policy=BAD_DATA_RETRY,
        )
