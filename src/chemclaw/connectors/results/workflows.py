"""The `results` bundle's durable workflow: one republish pass over the stored corpus.

Deterministic orchestration only. The walk is `chemclaw.publish.backfill`, shared with
`python -m chemclaw.cli.backfill_publications` so the job and the CLI cover exactly the same rows.
"""

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.connectors.queues import bundle_queue
    from chemclaw.connectors.results.specs import RepublishSpec
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import ConnectorJobResult
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.publish.backfill import backfill_cached, backfill_jobs, requeue_failed
    from chemclaw.publish.registry import ResultSinkError, unpublishable_reason

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, connector_queue_wait_timeout

_QUEUE = bundle_queue("results")


@durable_activity(_QUEUE)
@activity.defn
async def republish_stored_results(spec: RepublishSpec) -> dict[str, int]:
    """Walk the stored corpus and queue what has not been published. Returns the counts.

    Runs on this bundle's own queue: a full scan of two never-pruned tables should not share a
    worker with small jobs.
    """
    # Heartbeat throughout: the scan has no unit boundary to report at, and a killed worker must be
    # noticed before start-to-close lapses.
    activity.heartbeat()
    return await beating(
        _walk(spec), "republish stored results", settings.result_republish_heartbeat_timeout_seconds
    )


async def _walk(spec: RepublishSpec) -> dict[str, int]:
    """The scan itself, so the activity above is nothing but its heartbeat wrapper.

    Refuses before scanning when this deployment publishes nowhere, since `enqueue` is then a no-op
    and the counts would look like an up-to-date corpus. `ResultSinkError` is non-retryable
    (`durable/publish._BAD_DATA_TYPES`), so the job fails fast and the chemist reads the reason.
    """
    reason = unpublishable_reason()
    if reason is not None:
        raise ResultSinkError(reason)
    requeued = await requeue_failed() if spec.requeue_failed else 0
    cached = await backfill_cached(dry_run=False, batch=spec.batch)
    jobs = await backfill_jobs(dry_run=False, batch=spec.batch)
    # Flat, because `ConnectorJobResult.data` is `dict[str, int]`. `_failed` is separate from
    # `_skipped` because they need different actions (see `WalkCounts`).
    return {
        "requeued": requeued,
        "calculations_seen": cached.seen,
        "calculations_queued": cached.queued,
        "calculations_skipped": cached.skipped,
        "calculations_failed": cached.failed,
        "records_from_calculations": cached.records,
        "jobs_seen": jobs.seen,
        "jobs_queued": jobs.queued,
        "jobs_skipped": jobs.skipped,
        "jobs_failed": jobs.failed,
        "records_from_jobs": jobs.records,
    }


@durable_workflow(_QUEUE)
# Without `failure_exception_types` a plain exception in workflow code retries forever and the
# parent never sends the failure push-back. `tests/test_workflow_registry.py` checks every bundle
# workflow carries it.
@workflow.defn(failure_exception_types=[Exception])
class RepublishResultsWorkflow:
    """Re-queue stored calculations for the external results store."""

    @workflow.run
    async def run(self, spec: RepublishSpec) -> ConnectorJobResult:
        """Run one republish pass and report what it queued.

        Proposes no knowledge note: moving records between stores establishes nothing about
        chemistry.
        """
        counts = await workflow.execute_activity(
            republish_stored_results,
            spec,
            # Its own budget, strictly inside the parent's ceiling
            # (`result_republish_timeout_seconds`), so the retry policy stays reachable.
            start_to_close_timeout=timedelta(seconds=settings.result_republish_timeout_seconds),
            heartbeat_timeout=timedelta(
                seconds=settings.result_republish_heartbeat_timeout_seconds
            ),
            # Bounds the queue wait, so a `connector-results` queue served by no pod fails promptly
            # rather than at the parent's ceiling. See `durable/publish.py`.
            schedule_to_start_timeout=connector_queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
        # Rows and records are different units, and the summary names each as what it is.
        queued = counts["calculations_queued"] + counts["jobs_queued"]
        records = counts["records_from_calculations"] + counts["records_from_jobs"]
        skipped = counts["calculations_skipped"] + counts["jobs_skipped"]
        failed = counts["calculations_failed"] + counts["jobs_failed"]
        seen = counts["calculations_seen"] + counts["jobs_seen"]
        summary = (
            f"Queued {records} scientific record(s) from {queued} of {seen} stored row(s) "
            f"({counts['calculations_seen']} calculations and {counts['jobs_seen']} job records "
            f"examined; {skipped} skipped as unprojectable by this release"
        )
        # Named only when it is non-zero, because it is the one number that asks for a code change
        # — and a line reading "0 unreadable" on every healthy pass is how it stops being read.
        if failed:
            summary += (
                f"; {failed} had a projector in this release that could not read them, so this "
                f"pass did not cover them"
            )
        summary += (
            f"; {counts['requeued']} retired publication(s) re-queued)."
            if counts["requeued"]
            else ")."
        )
        return ConnectorJobResult(summary=summary, data=dict(counts))
