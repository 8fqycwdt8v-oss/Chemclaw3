"""The durable conformer-ensemble workflow runs to a typed result (D-092).

Runs on Temporal's time-skipping test server in CI, skips in the offline sandbox — mirrors
`test_qm_workflow.py`/`test_bo_campaign.py`'s bootstrap exactly.
"""

import asyncio

from temporalio.worker import Worker

from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.conformer_activities import prepare_conformer_input, run_conformer_ensemble
from workflows.conformer_job import ConformerEnsembleWorkflow
from workflows.conformer_models import ConformerJobInput

_TASK_QUEUE = "test-conformer"
_ACTIVITIES = [prepare_conformer_input, run_conformer_ensemble]


def test_conformer_job_runs_to_typed_result() -> None:
    """A submitted job completes durably and returns a Boltzmann-weighted ensemble result."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[ConformerEnsembleWorkflow],
                activities=_ACTIVITIES,
            ):
                result = await client.execute_workflow(
                    ConformerEnsembleWorkflow.run,
                    ConformerJobInput(molecule_smiles="CCCCO"),
                    id="conformer-test-1",
                    task_queue=_TASK_QUEUE,
                )
        assert result.ensemble.smiles == "CCCCO"
        assert result.ensemble.n_conformers_evaluated >= 1
        assert result.requested_by  # the default service actor id, not empty

    asyncio.run(_run())


def test_conformer_job_rejects_bad_smiles_non_retryably() -> None:
    """`prepare_conformer_input`'s validation gate fails the workflow, not the whole worker."""
    from temporalio.client import WorkflowFailureError

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[ConformerEnsembleWorkflow],
                activities=_ACTIVITIES,
            ):
                try:
                    await client.execute_workflow(
                        ConformerEnsembleWorkflow.run,
                        ConformerJobInput(molecule_smiles="%%%not-a-mol%%%"),
                        id="conformer-test-bad",
                        task_queue=_TASK_QUEUE,
                    )
                    raise AssertionError("expected the workflow to fail")
                except WorkflowFailureError:
                    pass

    asyncio.run(_run())
