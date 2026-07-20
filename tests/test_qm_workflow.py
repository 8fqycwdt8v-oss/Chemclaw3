"""Behavioral tests for the QM durable spine (plan Phase 1, acceptance P1).

Proves the full path runs to a typed result on Temporal's time-skipping test
server, and that the workflow history replays deterministically (the guarantee
CHECKMATE 1's worker-restart spike relies on). Activity edge cases are checked
directly. No running cluster required.
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from chemclaw.config import settings
from workflows.activities import (
    parse_qm_output,
    poll_hpc_status,
    prepare_input,
    submit_to_hpc,
)
from workflows.models import QMJobInput
from workflows.qm_job import QMJobWorkflow

_ACTIVITIES: Sequence[Callable[..., Any]] = [
    prepare_input,
    submit_to_hpc,
    poll_hpc_status,
    parse_qm_output,
]
_TASK_QUEUE = "test-hpc"


async def _start_env() -> WorkflowEnvironment:
    """Start the time-skipping test server, or skip if its binary can't be fetched.

    The server binary is downloaded on first use; in a network-restricted sandbox
    that fails, so the server-backed tests skip locally but run fully in CI (where
    the download succeeds). Pure activity tests below never depend on this.
    """
    try:
        return await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Temporal test server unavailable (offline sandbox): {exc}")


@pytest.fixture(autouse=True)
def _fast_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the mock-HPC sleeps so the durable path runs in milliseconds."""
    monkeypatch.setattr(settings, "hpc_mock_submit_seconds", 0.0)
    monkeypatch.setattr(settings, "hpc_mock_run_seconds", 0.02)
    monkeypatch.setattr(settings, "hpc_poll_interval_seconds", 0.01)


def _pydantic_client(env: WorkflowEnvironment) -> Client:
    """Rebuild the env's client with our pydantic data converter."""
    config = env.client.config()
    config["data_converter"] = pydantic_data_converter
    return Client(**config)


def test_qm_job_runs_to_typed_result() -> None:
    """A submitted job completes durably and returns a parsed `QMJobResult`."""

    async def _run() -> None:
        async with await _start_env() as env:
            client = _pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[QMJobWorkflow],
                activities=_ACTIVITIES,
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
        async with await _start_env() as env:
            client = _pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[QMJobWorkflow],
                activities=_ACTIVITIES,
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


def test_prepare_input_rejects_blank_smiles() -> None:
    """Whitespace-only SMILES fails fast at the first activity (gate G4)."""
    with pytest.raises(ValueError, match="must not be blank"):
        asyncio.run(prepare_input(QMJobInput(molecule_smiles="   ", method="HF", basis_set="X")))


def test_parse_qm_output_rejects_unparseable() -> None:
    """Corrupt HPC output raises rather than yielding a silent zero-energy result."""
    job = QMJobInput(molecule_smiles="CCO", method="HF", basis_set="X")
    with pytest.raises(ValueError, match="unparseable"):
        asyncio.run(parse_qm_output(job, "garbage output, no fields"))


def test_bad_input_surfaces_as_workflow_failure() -> None:
    """A blank SMILES makes the whole job fail loudly (activity error propagates)."""

    async def _run() -> None:
        async with await _start_env() as env:
            client = _pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[QMJobWorkflow],
                activities=_ACTIVITIES,
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
