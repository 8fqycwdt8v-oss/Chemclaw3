"""Scheduled rebuild of the derived note index (gap SCH-2).

F10-A shipped the dense+lexical `note_index` and a `make reindex` CLI, but nothing kept it in step
with the graph. The graph changes on every merged PR, so under `retrieval_mode="hybrid"` the vector
and lexical legs were serving whatever the last manual reindex captured — and because RRF fusion is
score-agnostic, a stale entry ranks confidently *alongside* live graph hits with no staleness
signal. That is worse than the legs being absent.

This is the missing driver: one activity wrapping the existing `reindex_notes` (no new logic, no new
store), one workflow on `background-jobs`, and one Temporal Schedule (`durable/schedules.py`).
Reindexing is idempotent by upsert, so re-running is always safe and the Schedule needs no cursor.

The index is *derived* — Git-Markdown stays the source of truth (D-004) — so a failed run degrades
retrieval quality for one cycle and never loses data.
"""

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.heartbeat import beating
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.retrieval.vector_index import default_note_index, reindex_notes

from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout


@durable_activity("background")
@activity.defn
async def reindex_notes_activity() -> int:
    """Rebuild the derived note index from the knowledge graph; return the note count indexed.

    Heartbeating throughout, because there is no unit boundary to report progress at: this is one
    whole-corpus pass plus one embedding batch over the network, which is precisely the "opaque
    single call" shape `durable/heartbeat.py` was extracted for. Without it a worker that dies here
    is invisible until `note_reindex_timeout_seconds` — ten minutes — has elapsed.
    """
    return await beating(
        reindex_notes(default_note_index()),
        "note reindex",
        settings.background_activity_heartbeat_timeout_seconds,
    )


@durable_workflow("background")
@workflow.defn
class NoteReindexWorkflow:
    """Refresh the derived note index so hybrid retrieval sees the current graph.

    A single activity: the work is one bounded pass over the note directory plus one embedding
    batch, so there is nothing to fan out. It runs on the background queue beside the other
    periodic jobs.
    """

    @workflow.run
    async def run(self) -> int:
        """Run the reindex activity and return how many notes were indexed."""
        return await workflow.execute_activity(
            reindex_notes_activity,
            start_to_close_timeout=timedelta(seconds=settings.note_reindex_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            # Without a heartbeat timeout the beats the activity now sends do nothing for
            # failure detection, and this activity's start-to-close budget is the only thing that
            # would notice a dead worker — ten minutes, over a pass that normally takes seconds.
            # `connectors/calc/workflows.py` states the rule; core's own long work simply never
            # applied it. The beat is derived from this same number
            # (`durable/heartbeat.py::beating`), so the two cannot drift.
            heartbeat_timeout=timedelta(
                seconds=settings.background_activity_heartbeat_timeout_seconds
            ),
            retry_policy=BAD_DATA_RETRY,
        )
