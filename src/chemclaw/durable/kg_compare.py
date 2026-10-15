"""Scheduled comparison of the two note stores while `kg_store=dual`.

Reads the notes directory and the Postgres store, compares their canonical notes and edges
(`kg.compare`), publishes the number of differing notes per kind as
`chemclaw_kg_compare_differences`, and returns the report. Read-only on both stores: repairing is
an operator's act (`make kg-validate-compare ARGS=--repair`), because a repair that ran by itself
would turn every divergence into a clean day and the read flip waits for clean days.

One activity, one workflow on `background-jobs`, one Schedule that exists only under
`kg_store=dual`.
"""

import logging
from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.heartbeat import beating
    from chemclaw.durable.registry import ActivityClass, durable_activity, durable_workflow
    from chemclaw.kg.compare import compare_stores, publish_differences
    from chemclaw.kg.store import GitNoteStore, postgres_note_store

from chemclaw.durable.publish import queue_wait_timeout
from chemclaw.durable.retry import retry_for

log = logging.getLogger(__name__)

#: How many differing note ids a run's result names. The full list is one `make
#: kg-validate-compare` away; a workflow result is not the place for a corpus-sized list.
_REPORTED_IDS = 20


class CompareReport(BaseModel):
    """What a comparison found: whether the stores agree, how many notes each serves, and where."""

    clean: bool
    git_notes: int
    postgres_notes: int
    counts: dict[str, int]
    differing: list[str]


@durable_activity("background", ActivityClass.COMPUTE)
@activity.defn
async def compare_knowledge_stores() -> CompareReport:
    """Compare the notes directory with the Postgres store; publish and return the result.

    Differences that do not persist across `kg_compare_settle_seconds` are not reported (a note
    another pod wrote has not reached this pod's clone). Heartbeats throughout, since reading both
    corpora and the pause have no unit boundary to report at.
    """
    comparison = await beating(
        compare_stores(
            GitNoteStore(settings.knowledge_path),
            postgres_note_store(),
            settle_seconds=settings.kg_compare_settle_seconds,
        ),
        "knowledge store comparison",
        settings.background_activity_heartbeat_timeout_seconds,
    )
    publish_differences(comparison)
    counts = comparison.counts()
    if comparison.clean:
        log.info(
            "kg.compare: the stores agree on %d note(s)",
            comparison.git_notes,
        )
    else:
        log.error(
            "kg.compare: %d note(s) differ between the notes directory and Postgres: %s",
            len(comparison.differences),
            counts,
        )
    return CompareReport(
        clean=comparison.clean,
        git_notes=comparison.git_notes,
        postgres_notes=comparison.postgres_notes,
        counts=counts,
        differing=[d.note_id for d in comparison.differences[:_REPORTED_IDS]],
    )


@durable_workflow("background")
# Declared so a broken run is reported through `ScheduleHealth.last_outcome`: a parked comparison
# would read as the clean day the read flip is waiting for.
@workflow.defn(failure_exception_types=[Exception])
class KgCompareWorkflow:
    """Compare the two note stores once and report; a difference is a result, not a failure."""

    @workflow.run
    async def run(self) -> CompareReport:
        """Run the comparison activity and return its report."""
        return await workflow.execute_activity(
            compare_knowledge_stores,
            start_to_close_timeout=timedelta(seconds=settings.kg_compare_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            heartbeat_timeout=timedelta(
                seconds=settings.background_activity_heartbeat_timeout_seconds
            ),
            retry_policy=retry_for(compare_knowledge_stores),
        )
