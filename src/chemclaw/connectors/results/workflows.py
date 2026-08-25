"""The `results` bundle's durable workflow: one republish pass over the stored corpus.

Deterministic orchestration only — it runs one activity and shapes the envelope. The walk itself is
`chemclaw.cli.backfill_publications`, reused rather than reimplemented: an operator running it from
a terminal and a chemist launching it as a job must cover exactly the same rows, and two walks that
agreed today would diverge on the next table.
"""

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.connectors.queues import bundle_queue
    from chemclaw.connectors.results.specs import RepublishSpec
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import ConnectorJobResult
    from chemclaw.durable.registry import durable_activity, durable_workflow

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
    from chemclaw.cli.backfill_publications import (
        _backfill_cached,
        _backfill_jobs,
        _requeue_failed,
    )

    requeued = await _requeue_failed() if spec.requeue_failed else 0
    cached_seen, cached_queued, cached_skipped = await _backfill_cached(
        dry_run=False, batch=spec.batch
    )
    jobs_seen, jobs_queued, jobs_skipped = await _backfill_jobs(dry_run=False, batch=spec.batch)
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
@workflow.defn
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
