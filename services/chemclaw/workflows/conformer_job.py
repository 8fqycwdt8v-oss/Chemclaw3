"""The durable conformer-ensemble workflow — research follow-up, D-092.

A Boltzmann-weighted GFN2-xTB conformer ensemble (tens of xTB single points via
`calc.conformer_ensemble`) is materially heavier than the sub-second budget the inline
fast-calculator pattern assumes, but it is pure local CPU work, not a remote HPC submission — the
same situation `BoCampaignWorkflow` is in. So this follows *that* workflow's shape (deterministic
orchestration over local, CPU-bound activities on the light `background-jobs` queue) rather than
`QMJobWorkflow`'s submit/poll shape, which exists specifically to model a remote scheduler.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from workflows.conformer_activities import prepare_conformer_input, run_conformer_ensemble
    from workflows.conformer_models import ConformerJobInput, ConformerJobResult
    from workflows.notify import notify_session_best_effort
    from workflows.registry import durable_workflow

from workflows.publish import BAD_DATA_RETRY


@durable_workflow("background")
@workflow.defn
class ConformerEnsembleWorkflow:
    """Runs one Boltzmann-weighted conformer ensemble as a durable job."""

    @workflow.run
    async def run(self, job: ConformerJobInput) -> ConformerJobResult:
        """Validate, run the ensemble, and notify the launching session; safe to replay/resume."""
        timeout = timedelta(seconds=settings.conformer_activity_timeout_seconds)

        prepared = await workflow.execute_activity(
            prepare_conformer_input,
            job,
            start_to_close_timeout=timeout,
            retry_policy=BAD_DATA_RETRY,
        )
        ensemble = await workflow.execute_activity(
            run_conformer_ensemble,
            prepared,
            start_to_close_timeout=timeout,
            retry_policy=BAD_DATA_RETRY,
        )
        result = ConformerJobResult(ensemble=ensemble, requested_by=prepared.requested_by)

        # Wake the launching session (mirrors QMJobWorkflow), best-effort: a failed notification
        # never fails the (successful, cached-by-workflow-id) ensemble result.
        if job.session_id:
            await notify_session_best_effort(
                job.session_id,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "molecule_smiles": ensemble.smiles,
                    "boltzmann_weighted_energy_hartree": ensemble.boltzmann_weighted_energy_hartree,
                    "n_conformers_evaluated": ensemble.n_conformers_evaluated,
                },
            )
        return result
