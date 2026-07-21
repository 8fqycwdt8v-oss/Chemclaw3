"""The QM/DFT durable workflow (plan steps 1.2–1.4).

Deterministic orchestration only: it sequences the activities (prepare → submit →
poll → parse) and owns their timeouts (pulled from `chemclaw.config`, never
hardcoded). All non-determinism lives in `workflows.activities`.

Restarting a worker mid-run must resume from event history without re-executing a
completed activity — the durability spike verified at CHECKMATE 1.
"""

from datetime import timedelta

from temporalio import workflow

# Activities, models, and config are ordinary modules that must bypass the
# workflow sandbox's re-import isolation (the standard Temporal pattern).
with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from workflows.activities import (
        parse_qm_output,
        poll_hpc_status,
        prepare_input,
        submit_to_hpc,
    )
    from workflows.knowledge import write_knowledge_node
    from workflows.models import QMJobInput, QMJobResult
    from workflows.notify import notify_session_best_effort

from workflows.publish import BAD_DATA_RETRY, publish_note_best_effort


@workflow.defn
class QMJobWorkflow:
    """Runs one QM calculation as a durable job, returning a typed result."""

    @workflow.run
    async def run(self, job: QMJobInput) -> QMJobResult:
        """Execute the QM job end-to-end; safe to replay and to resume."""
        activity_timeout = timedelta(seconds=settings.qm_activity_timeout_seconds)

        prepared = await workflow.execute_activity(
            prepare_input, job, start_to_close_timeout=activity_timeout, retry_policy=BAD_DATA_RETRY
        )
        handle = await workflow.execute_activity(
            submit_to_hpc,
            prepared,
            start_to_close_timeout=activity_timeout,
            retry_policy=BAD_DATA_RETRY,
        )
        # The poll's start-to-close budget must cover the *entire* run in one attempt —
        # heartbeating resets only the heartbeat timeout, never start-to-close. The mock finishes in
        # `hpc_mock_run_seconds`; a real Nextflow run takes far longer, so the two backends use
        # different budgets (F5, review finding: a mock-derived 36s cap would kill every real run).
        if settings.hpc_launch_interface == "nextflow":
            poll_budget = settings.hpc_run_timeout_seconds
            poll_heartbeat = settings.hpc_run_heartbeat_timeout_seconds
        else:
            poll_budget = settings.hpc_mock_run_seconds + settings.qm_activity_timeout_seconds
            poll_heartbeat = settings.qm_poll_heartbeat_timeout_seconds
        raw_output = await workflow.execute_activity(
            poll_hpc_status,
            handle,
            start_to_close_timeout=timedelta(seconds=poll_budget),
            heartbeat_timeout=timedelta(seconds=poll_heartbeat),
            retry_policy=BAD_DATA_RETRY,
        )
        result = await workflow.execute_activity(
            parse_qm_output,
            args=[prepared, raw_output],
            start_to_close_timeout=activity_timeout,
            retry_policy=BAD_DATA_RETRY,
        )

        # Optionally publish the result as a PR-gated graph note (step 2.8). It runs
        # on the light background-jobs queue (git write, not HPC), and a failure to
        # publish must not fail the (successful, cached) calculation — so the note
        # write is best-effort and its outcome is not part of the returned result.
        if job.publish_to_graph:
            await publish_note_best_effort(
                write_knowledge_node, [result], label=result.molecule_smiles
            )

        # Wake the launching session (F3-T3): a durable push-back event so the chemist sees the
        # result without polling. Best-effort — a failed notification never fails the cached result.
        if job.session_id:
            await notify_session_best_effort(
                job.session_id,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "molecule_smiles": result.molecule_smiles,
                    "total_energy_hartree": result.total_energy_hartree,
                    "converged": result.converged,
                },
            )
        return result
