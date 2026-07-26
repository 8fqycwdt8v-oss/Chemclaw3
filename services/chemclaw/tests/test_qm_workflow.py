"""Behavioral tests for the QM durable spine (plan Phase 1, acceptance P1).

Proves the full path runs to a typed result on Temporal's time-skipping test
server, and that the workflow history replays deterministically (the guarantee
CHECKMATE 1's worker-restart spike relies on). Activity edge cases are checked
directly. No running cluster required.
"""

import asyncio

import httpx
import pytest
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker

from chemclaw.config import settings
from tests.temporal_env import QM_ACTIVITIES, pydantic_client, start_env_or_skip
from workflows.activities import parse_qm_output, poll_hpc_status, prepare_input
from workflows.hpc import nextflow
from workflows.models import HpcJobHandle, QMJobInput
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


class _ScriptedPoll:
    """A scripted `nextflow.poll_run` stand-in: pops one outcome per call (exception or state)."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def __call__(self, handle: object) -> object:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def _nextflow_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route `poll_hpc_status` to the nextflow path with a near-zero poll interval."""
    monkeypatch.setattr(settings, "hpc_launch_interface", "nextflow")
    monkeypatch.setattr(settings, "hpc_poll_interval_seconds", 0.001)


def test_poll_survives_transient_launcher_blips(
    _nextflow_poll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP blips mid-poll keep polling instead of failing the attempt (the run is still fine).

    A 24h DFT run sees a handful of launcher restarts/network blips; each one must not burn
    one of the activity's shared retry attempts — five blips over a day would otherwise
    permanently fail a job whose HPC run actually succeeds.
    """
    poll = _ScriptedPoll(
        [
            nextflow.NextflowError("poll failed: 502 launcher restarting"),
            httpx.ConnectError("connection refused"),
            nextflow.RunState.RUNNING,
            nextflow.RunState.SUCCEEDED,
        ]
    )
    monkeypatch.setattr(nextflow, "poll_run", poll)

    async def _fetch(handle: object) -> str:
        return "energy=-1.500000 converged=True"

    monkeypatch.setattr(nextflow, "fetch_artifacts", _fetch)
    output = asyncio.run(
        ActivityEnvironment().run(poll_hpc_status, HpcJobHandle(scheduler_job_id="run-77"))
    )
    assert output == "energy=-1.500000 converged=True"
    assert poll.calls == 4  # both blips were absorbed by the loop, not surfaced as attempt failures


def test_failed_run_is_non_retryable(_nextflow_poll: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminally FAILED run raises a non-retryable error — re-polling it cannot help."""
    monkeypatch.setattr(nextflow, "poll_run", _ScriptedPoll([nextflow.RunState.FAILED]))
    with pytest.raises(ApplicationError, match="failed") as excinfo:
        asyncio.run(
            ActivityEnvironment().run(poll_hpc_status, HpcJobHandle(scheduler_job_id="run-78"))
        )
    assert excinfo.value.non_retryable is True


def test_persistent_launcher_outage_still_fails(
    _nextflow_poll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive poll errors beyond the configured bound surface (no silent 24h error loop)."""
    monkeypatch.setattr(settings, "hpc_poll_max_consecutive_errors", 3)
    poll = _ScriptedPoll([nextflow.NextflowError(f"poll failed: {i}") for i in range(5)])
    monkeypatch.setattr(nextflow, "poll_run", poll)
    with pytest.raises(nextflow.NextflowError, match="poll failed"):
        asyncio.run(
            ActivityEnvironment().run(poll_hpc_status, HpcJobHandle(scheduler_job_id="run-79"))
        )
    assert poll.calls == 3  # gave up at the bound, not on the first blip


def test_a_success_resets_the_consecutive_error_count(
    _nextflow_poll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blips spread across a long run never accumulate — only *consecutive* failures count."""
    monkeypatch.setattr(settings, "hpc_poll_max_consecutive_errors", 2)
    poll = _ScriptedPoll(
        [
            nextflow.NextflowError("blip 1"),
            nextflow.RunState.RUNNING,
            nextflow.NextflowError("blip 2"),
            nextflow.RunState.RUNNING,
            nextflow.NextflowError("blip 3"),
            nextflow.RunState.SUCCEEDED,
        ]
    )
    monkeypatch.setattr(nextflow, "poll_run", poll)

    async def _fetch(handle: object) -> str:
        return "energy=-2.000000 converged=True"

    monkeypatch.setattr(nextflow, "fetch_artifacts", _fetch)
    output = asyncio.run(
        ActivityEnvironment().run(poll_hpc_status, HpcJobHandle(scheduler_job_id="run-80"))
    )
    assert output == "energy=-2.000000 converged=True"
