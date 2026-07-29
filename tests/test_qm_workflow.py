"""Behavioral tests for the QM durable spine (plan Phase 1, acceptance P1).

Proves the full path runs to a typed result on Temporal's time-skipping test server, and that the
workflow history replays deterministically (the guarantee CHECKMATE 1's worker-restart spike relies
on). Activity edge cases are checked directly. No running cluster required.

The workflow is a `qm` connector job now (D-118), so what it returns is the `ConnectorJobResult`
envelope and what it takes is the bare `QmJobSpec` — the actor arrives on the run's memo, stamped
by `ConnectorJobWorkflow`, and is deliberately not a field the model could author.
"""

import asyncio

import httpx
import pytest
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker

from chemclaw.connectors.qm.activities import parse_qm_output, poll_hpc_status, prepare_input
from chemclaw.connectors.qm.hpc import nextflow
from chemclaw.connectors.qm.specs import HpcJobHandle, QMJobInput, QmJobSpec
from chemclaw.connectors.qm.workflows import QMJobWorkflow
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import ConnectorJobResult
from tests.temporal_env import QM_ACTIVITIES, pydantic_client, start_env_or_skip

_TASK_QUEUE = "test-connector-qm"


def test_qm_job_runs_to_the_connector_envelope() -> None:
    """A submitted job completes durably and comes back in the envelope core reads.

    The envelope is the whole cross-process contract: one line the chat shows, the job's own
    structured result, and the note core PR-gates. A QM run that returned its bespoke
    `QMJobResult` instead would poll to `completed` in `get_durable_job_status` and then hand the
    chemist nothing — the failure the envelope was adopted to end.
    """

    async def _run() -> ConnectorJobResult:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[QMJobWorkflow],
                activities=QM_ACTIVITIES,
            ):
                result: ConnectorJobResult = await client.execute_workflow(
                    QMJobWorkflow.run,
                    QmJobSpec(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP"),
                    id="qm-test-1",
                    task_queue=_TASK_QUEUE,
                )
                return result

    result = asyncio.run(_run())
    assert result.data["converged"] is True
    assert result.data["molecule_smiles"] == "CCO"
    assert result.data["total_energy_hartree"] <= 0.0
    assert "B3LYP/def2-SVP on CCO" in result.summary


def test_the_actor_reaches_the_hpc_side_from_the_run_memo() -> None:
    """`requested_by` survives the move to a declared job — by memo, not by spec field.

    The HPC cluster runs under a shared service identity, so the requesting user is the only thing
    that makes a run attributable (F4-T3). `ConnectorJobWorkflow` stamps it on the child's memo;
    this asserts the reading half against a real server, with the memo set the way the wrapper
    sets it. Losing it would silently anonymize every cluster submission.
    """

    async def _run() -> ConnectorJobResult:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=_TASK_QUEUE,
                workflows=[QMJobWorkflow],
                activities=QM_ACTIVITIES,
            ):
                result: ConnectorJobResult = await client.execute_workflow(
                    QMJobWorkflow.run,
                    QmJobSpec(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP"),
                    id="qm-test-memo",
                    task_queue=_TASK_QUEUE,
                    memo={"requested_by": "oid-from-the-turn"},
                )
                return result

    result = asyncio.run(_run())
    assert result.data["requested_by"] == "oid-from-the-turn"
    assert result.note is not None
    assert result.note.source == "qm:oid-from-the-turn"  # and into the audit trail on the note


def test_the_model_cannot_author_the_actor() -> None:
    """The spec the manifest advertises carries no identity field — offline, always runs.

    `params_model` becomes the JSON schema the LLM fills in, so a `requested_by` on `QmJobSpec`
    would let a model attribute a cluster run to anyone. The activities still need it, which is
    why it lives one subclass down on `QMJobInput`, reachable only from the memo.
    """
    assert "requested_by" not in QmJobSpec.model_fields
    assert "requested_by" in QMJobInput.model_fields
    # And nothing ambient to the turn leaked in with it.
    assert set(QmJobSpec.model_fields) == {"molecule_smiles", "method", "basis_set"}


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
                    QmJobSpec(molecule_smiles="c1ccccc1", method="HF", basis_set="STO-3G"),
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
                        QmJobSpec(molecule_smiles=" ", method="HF", basis_set="X"),
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
