"""The durable xTB workflow (xTB plan X3/X4).

Why an xTB task needs Temporal at all, when the fast calculators deliberately do not:
the X1/X2 tools are single points, measured at ~2.4 ms, where a workflow would be pure
overhead and the calculation store alone gives the "never twice" guarantee. X3/X4 are a
different animal — an optimization plus a Hessian per species, once per solvent. A
four-species reaction is seconds; a solvent screen over five solvents, or a relaxed scan
on a mid-sized molecule, is minutes. That is past the point where a conversation can
sit and wait, so the tools route by predicted cost (`calc.xtb_cost`): cheap requests run
inline, expensive ones become a job id and a push-back.

Deterministic orchestration only, exactly as `QMJobWorkflow`: this module sequences and
times the activity; every non-deterministic thing lives in `workflows.xtb_activities`.
"""

from datetime import timedelta

from temporalio import workflow

# Activities, models, and config are ordinary modules that must bypass the workflow
# sandbox's re-import isolation (the standard Temporal pattern).
with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from workflows.models import XtbJobInput, XtbJobResult
    from workflows.notify import notify_session_best_effort
    from workflows.registry import durable_workflow
    from workflows.xtb_activities import run_xtb_calculation

from workflows.publish import BAD_DATA_RETRY


@durable_workflow("hpc")
@workflow.defn
class XtbJobWorkflow:
    """Runs one expensive xTB calculation as a durable job, returning a typed result."""

    @workflow.run
    async def run(self, job: XtbJobInput) -> XtbJobResult:
        """Execute the xTB task; safe to replay and to resume after a worker restart."""
        result = await workflow.execute_activity(
            run_xtb_calculation,
            job,
            start_to_close_timeout=timedelta(seconds=settings.xtb_job_timeout_seconds),
            # The activity heartbeats between species and scan points. Without a
            # heartbeat timeout those heartbeats do nothing for failure detection, and a
            # worker that dies mid-job would be noticed only when the hour-long
            # start-to-close budget expired — which on minute-scale work is the
            # difference between a retry and a wasted afternoon.
            heartbeat_timeout=timedelta(seconds=settings.xtb_job_heartbeat_timeout_seconds),
            # A malformed request (unbalanced equation, unknown solvent, bad atom index)
            # is a `ValueError` that no retry can fix — the same non-retryable class the
            # QM job uses, so a bad input fails fast instead of burning the budget.
            retry_policy=BAD_DATA_RETRY,
        )
        # Wake the launching session (F3-T3) so the chemist sees the result without
        # polling. Best-effort: a failed notification must not fail a completed — and
        # now cached — calculation.
        if job.session_id:
            await notify_session_best_effort(
                job.session_id,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "kind": result.kind,
                    "summary": result.summary,
                },
            )
        return result
