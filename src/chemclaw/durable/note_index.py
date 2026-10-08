"""Scheduled rebuild of the derived note index.

Keeps the dense and lexical `note_index` in step with the graph: under hybrid retrieval a stale
entry ranks confidently beside live hits with no staleness signal. One activity wrapping
`reindex_exclusively`, one workflow on `background-jobs`, one Schedule. Idempotent by upsert, and
the index is derived (Git-Markdown is the source of truth), so a failed run never loses data.
"""

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.heartbeat import beating
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.retrieval.vector_index import (
        REINDEX_SKIPPED,
        default_note_index,
        reindex_exclusively,
    )

from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout


@durable_activity("background")
@activity.defn
async def reindex_notes_activity() -> int:
    """Rebuild the derived note index from the knowledge graph; return the note count indexed.

    Returns `REINDEX_SKIPPED` (-1) instead of a count when another worker held the pass, so a
    skipped run reads differently from an index that was already current (0) in the run's result.
    Heartbeats throughout, since a whole-corpus pass plus an embedding batch has no unit boundary to
    report progress at.
    """
    indexed = await beating(
        reindex_exclusively(default_note_index()),
        "note reindex",
        settings.background_activity_heartbeat_timeout_seconds,
    )
    return REINDEX_SKIPPED if indexed is None else indexed


@durable_workflow("background")
# Declared so a broken run is reported through `ScheduleHealth.last_outcome` rather than parking
# silently while hybrid retrieval serves a stale index.
@workflow.defn(failure_exception_types=[Exception])
class NoteReindexWorkflow:
    """Refresh the derived note index so hybrid retrieval sees the current graph.

    A single activity: one bounded pass plus one embedding batch, nothing to fan out.
    """

    @workflow.run
    async def run(self) -> int:
        """Run the reindex activity and return how many notes were indexed."""
        return await workflow.execute_activity(
            reindex_notes_activity,
            start_to_close_timeout=timedelta(seconds=settings.note_reindex_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            # Without a heartbeat timeout the beats detect nothing; the beat interval is derived
            # from this same value (`durable/heartbeat.py::beating`).
            heartbeat_timeout=timedelta(
                seconds=settings.background_activity_heartbeat_timeout_seconds
            ),
            retry_policy=BAD_DATA_RETRY,
        )
