"""Behavioral tests for the QM durable spine (plan Phase 1, acceptance P1).

Proves the full path runs to a typed result on Temporal's time-skipping test
server, and that the workflow history replays deterministically (the guarantee
CHECKMATE 1's worker-restart spike relies on). Activity edge cases are checked
directly. No running cluster required.
"""

import asyncio

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError
from temporalio.worker import Replayer, Worker

from tests.temporal_env import QM_ACTIVITIES, pydantic_client, start_env_or_skip
from workflows.activities import parse_qm_output, prepare_input
from workflows.models import QMJobInput
from workflows.qm_job import QMJobWorkflow

_TASK_QUEUE = "test-hpc"


def test_qm_job_runs_to_typed_result() -> None:
    """A submitted job completes durably and returns a parsed `QMJobResult`."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[QMJobWorkflow],
                activities=QM_ACTIVITIES,
            ):
                result = await client.execute_workflow(
                    QMJobWorkflow.run,
                    QMJobInput(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP"),
                    id="qm-test-1",
                    task_queue=_TASK_QUEUE,
                )
        assert result.converged is True
        assert result.molecule_smiles == "CCO"
        assert result.total_energy_hartree <= 0.0

    asyncio.run(_run())


def test_workflow_history_replays_deterministically() -> None:
    """Re-running the recorded history must not raise — proves resume-safety."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[QMJobWorkflow],
                activities=QM_ACTIVITIES,
            ):
                handle = await client.start_workflow(
                    QMJobWorkflow.run,
                    QMJobInput(molecule_smiles="c1ccccc1", method="HF", basis_set="STO-3G"),
                    id="qm-test-replay",
                    task_queue=_TASK_QUEUE,
                )
                await handle.result()
                history = await handle.fetch_history()
            await Replayer(
                workflows=[QMJobWorkflow], data_converter=pydantic_data_converter
            ).replay_workflow(history)

    asyncio.run(_run())


def test_prepare_input_rejects_invalid_smiles() -> None:
    """A blank or unparseable SMILES fails fast at the first activity (gate G4).

    `prepare_input` now canonicalizes via RDKit, so both a whitespace-only value and a
    structurally invalid one are rejected here (`InvalidSmilesError`, a `ValueError`)
    rather than flowing through the mock into a stored result.
    """
    with pytest.raises(ValueError, match="invalid SMILES"):
        asyncio.run(prepare_input(QMJobInput(molecule_smiles="   ", method="HF", basis_set="X")))
    with pytest.raises(ValueError, match="invalid SMILES"):
        asyncio.run(prepare_input(QMJobInput(molecule_smiles="???", method="HF", basis_set="X")))


def test_parse_qm_output_rejects_unparseable() -> None:
    """Corrupt HPC output raises rather than yielding a silent zero-energy result."""
    job = QMJobInput(molecule_smiles="CCO", method="HF", basis_set="X")
    with pytest.raises(ValueError, match="unparseable"):
        asyncio.run(parse_qm_output(job, "garbage output, no fields"))


def test_bad_input_surfaces_as_workflow_failure() -> None:
    """A blank SMILES makes the whole job fail loudly (activity error propagates)."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[QMJobWorkflow],
                activities=QM_ACTIVITIES,
            ):
                with pytest.raises(WorkflowFailureError) as excinfo:
                    await client.execute_workflow(
                        QMJobWorkflow.run,
                        QMJobInput(molecule_smiles=" ", method="HF", basis_set="X"),
                        id="qm-test-bad",
                        task_queue=_TASK_QUEUE,
                    )
                # WorkflowFailure → ActivityError → the non-retryable ValueError.
                assert isinstance(excinfo.value.cause, ActivityError)

    asyncio.run(_run())
