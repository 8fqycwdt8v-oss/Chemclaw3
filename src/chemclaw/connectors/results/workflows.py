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
    from chemclaw.publish.registry import ResultSinkError, publishing_enabled

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, connector_queue_wait_timeout

_QUEUE = bundle_queue("results")


@durable_activity(_QUEUE)
@activity.defn
async def republish_stored_results(spec: RepublishSpec) -> dict[str, int]:
    """Walk the stored corpus and queue what has not been published. Returns the counts.

    Runs on this bundle's own queue rather than the light background one: it is a full scan of two
    never-pruned tables, which is precisely the shape that should not share a worker with the many
    small jobs.
    """
    # Beating throughout, not just around one leg: the walk is a scan of two never-pruned tables
    # and has no unit boundary to report progress at, so the honest signal is "still running" — the
    # `beating()` case exactly. Without it a worker killed ten minutes into a five-hour walk was
    # not noticed until the start-to-close lapsed.
    activity.heartbeat()
    return await beating(
        _walk(spec), "republish stored results", settings.result_republish_heartbeat_timeout_seconds
    )


async def _walk(spec: RepublishSpec) -> dict[str, int]:
    """The scan itself, so the activity above is nothing but its heartbeat wrapper.

    **Refuses before it scans when this deployment publishes nowhere.** `enqueue` is a no-op with
    `CHEMCLAW_RESULT_SINKS` empty, so without this the job ran a full pass over two never-pruned
    tables, wrote nothing, and reported `calculations_seen: 10, calculations_queued: 0` — which is
    exactly what a corpus with nothing left to publish reports. The CLI has had this guard from the
    start and exits 1; the durable job is the *chemist*-facing half of the same walk, where a
    misconfiguration is least diagnosable, so it was missing precisely where it mattered more.

    `ResultSinkError` rather than a report field: it is already in
    `durable/publish._BAD_DATA_TYPES`, so the job fails fast with the reason instead of spending
    eight attempts on a setting no retry changes, and the chemist reads it in the push-back rather
    than in a count.
    """
    if not publishing_enabled():
        raise ResultSinkError(
            "no result sink is enabled (CHEMCLAW_RESULT_SINKS is empty), so a republish would "
            "scan the whole stored corpus and queue nothing. Enable a sink first."
        )
    requeued = await requeue_failed() if spec.requeue_failed else 0
    cached = await backfill_cached(dry_run=False, batch=spec.batch)
    jobs = await backfill_jobs(dry_run=False, batch=spec.batch)
    # Flat, because `ConnectorJobResult.data` is `dict[str, int]` and a chemist reads these keys.
    # `_failed` is its own key rather than folded into `_skipped` for the reason `WalkCounts`
    # gives: they need different actions, and the union of them is what hid the first.
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
            # Its own budget, strictly inside the parent's ceiling — see
            # `result_republish_timeout_seconds`. Handing it `connector_job_timeout_seconds` made
            # the two expire together, which cost the retry policy and named neither setting.
            start_to_close_timeout=timedelta(seconds=settings.result_republish_timeout_seconds),
            heartbeat_timeout=timedelta(
                seconds=settings.result_republish_heartbeat_timeout_seconds
            ),
            # The budget above starts when a worker picks the task up, so it bounds none of the
            # wait for one; without this, a `connector-results` queue served by no pod was
            # indistinguishable from a busy one until the parent job's execution ceiling fired.
            # Stated once in `durable/publish.py`, which also says why a bundle's bound is not
            # core's.
            schedule_to_start_timeout=connector_queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
        # Rows and records are different units and the summary names both as what they are. The
        # sentence used to read "Queued N stored result(s)" over a row count, which a shape that
        # decomposes made larger than the number of rows examined beside it.
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
