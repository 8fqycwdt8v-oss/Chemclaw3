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
    from eln.json_adapter import JsonExportAdapter
    from eln.sync import IngestSummary, sync_entries
    from kg.git_submitter import default_submitter
    from mcp_servers.fpstore import PostgresFingerprintStore


def _reaction_store() -> PostgresFingerprintStore:
    """The production reaction fingerprint store (Postgres). Overridden in tests."""
    return PostgresFingerprintStore("reaction_fingerprints", settings.drfp_bits)


def _molecule_store() -> PostgresFingerprintStore:
    """The production molecule fingerprint store (Postgres). Overridden in tests."""
    return PostgresFingerprintStore("molecule_fingerprints", settings.ecfp_bits)


@activity.defn
async def sync_eln_entries(since: datetime) -> IngestSummary:
    """Ingest every ELN entry newer than `since`; return the summary + next cursor."""
    return await sync_entries(
        JsonExportAdapter(), _reaction_store(), _molecule_store(), default_submitter(), since
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
        )
