"""The `results` bundle's durable workflow: one republish pass over the stored corpus.

Deterministic orchestration only — it runs one activity and shapes the envelope. The walk itself is
`chemclaw.publish.backfill`, reused rather than reimplemented: an operator running
`python -m chemclaw.cli.backfill_publications` and a chemist launching this job must cover exactly
the same rows, and two walks that agreed today would diverge on the next table. That shared module
lives in the publish layer rather than in `cli/` precisely so this bundle can reach it — a
connector may not import a terminal entrypoint, and `tests/test_layering.py` caught the inversion
when it did.
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

from chemclaw.durable.publish import BAD_DATA_RETRY

_QUEUE = bundle_queue("results")


@durable_activity(_QUEUE)
@activity.defn
async def republish_stored_results(spec: RepublishSpec) -> dict[str, int]:
    """Walk the stored corpus and queue what has not been published. Returns the counts.

    Runs on this bundle's own queue rather than the light background one: it is a full scan of two
    never-pruned tables, which is precisely the shape that should not share a worker with the many
    small jobs.
    """
    requeued = await requeue_failed() if spec.requeue_failed else 0
    cached_seen, cached_queued, cached_skipped = await backfill_cached(
        dry_run=False, batch=spec.batch
    )
    jobs_seen, jobs_queued, jobs_skipped = await backfill_jobs(dry_run=False, batch=spec.batch)
    return {
        "requeued": requeued,
        "calculations_seen": cached_seen,
        "calculations_queued": cached_queued,
        "calculations_skipped": cached_skipped,
        "jobs_seen": jobs_seen,
        "jobs_queued": jobs_queued,
        "jobs_skipped": jobs_skipped,
    }


@durable_workflow(_QUEUE)
# **`failure_exception_types` or this workflow cannot fail — it hangs.** The SDK treats a plain
# exception raised in workflow code as a suspected bug and parks the run in an internal
# workflow-task-failure loop that ignores the retry policy and never gives up. On the job path that
# is the wrong default: a chemist has already been told the job is running, and the only way they
# ever hear otherwise is the push-back `ConnectorJobWorkflow` sends — which it can only send if
# this run actually ends. Every other bundle workflow carries the same declaration for the same
# measured reason, and `tests/test_workflow_registry.py` checks the registry rather than a list of
# names, so it caught this one the day it was added.
@workflow.defn(failure_exception_types=[Exception])
class RepublishResultsWorkflow:
    """Re-queue stored calculations for the external results store."""

    @workflow.run
    async def run(self, spec: RepublishSpec) -> ConnectorJobResult:
        """Run one republish pass and report what it queued.

        **Proposes no knowledge note, deliberately.** A republish moves records between stores; it
        establishes nothing about chemistry, so there is nothing for a human to validate and a note
        would put an operational event into the knowledge graph.
        """
        counts = await workflow.execute_activity(
            republish_stored_results,
            spec,
            start_to_close_timeout=timedelta(seconds=settings.connector_job_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )
        queued = counts["calculations_queued"] + counts["jobs_queued"]
        skipped = counts["calculations_skipped"] + counts["jobs_skipped"]
        summary = (
            f"Queued {queued} stored result(s) for the external results store "
            f"({counts['calculations_seen']} calculations and {counts['jobs_seen']} job records "
            f"examined; {skipped} skipped as unprojectable by this release"
        )
        summary += (
            f"; {counts['requeued']} retired publication(s) re-queued)."
            if counts["requeued"]
            else ")."
        )
        return ConnectorJobResult(summary=summary, data=dict(counts))
