"""The agent tools drive a real durable job (plan steps 1.5, 1.6).

Server-backed: runs on Temporal's time-skipping server in CI, skips in the
offline sandbox. Proves submit returns a job id without blocking, status reports
the completed result, and re-submitting an identical job is idempotent.
"""

import asyncio

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

import agents.qm_tools as qm_tools
from agents.qm_tools import get_qm_job_status, submit_qm_job
from chemclaw.config import settings
from tests.test_qm_workflow import _ACTIVITIES, _start_env
from workflows.qm_job import QMJobWorkflow


@pytest.fixture(autouse=True)
def _fast_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink mock-HPC sleeps so the job completes near-instantly."""
    monkeypatch.setattr(settings, "hpc_mock_submit_seconds", 0.0)
    monkeypatch.setattr(settings, "hpc_mock_run_seconds", 0.02)
    monkeypatch.setattr(settings, "hpc_poll_interval_seconds", 0.01)


def test_submit_returns_id_and_status_yields_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """submit_qm_job returns an id immediately; get_qm_job_status later has the result."""

    async def _run() -> None:
        async with await _start_env() as env:
            config = env.client.config()
            config["data_converter"] = pydantic_data_converter
            client = Client(**config)
            # The tools open their own client via connect(); point it at the env.
            monkeypatch.setattr(qm_tools, "connect", lambda: _ready(client))

            async with Worker(
                client,
                task_queue=settings.hpc_task_queue,
                workflows=[QMJobWorkflow],
                activities=_ACTIVITIES,
            ):
                job_id = await submit_qm_job("CCO", "B3LYP", "def2-SVP")
                assert job_id.startswith("qm-")

                # Idempotent: same inputs → same id, no duplicate job.
                again = await submit_qm_job("CCO", "B3LYP", "def2-SVP")
                assert again == job_id

                # Wait for completion, then status carries the parsed result.
                await client.get_workflow_handle(job_id).result()
                status = await get_qm_job_status(job_id)
                assert status.status == WorkflowExecutionStatus.COMPLETED.name
                assert status.result is not None
                assert status.result.molecule_smiles == "CCO"

    asyncio.run(_run())


def test_status_of_unknown_job_raises() -> None:
    """Polling a non-existent id is a clear error, not a crash (gate G4)."""

    async def _run() -> None:
        async with await _start_env() as env:
            config = env.client.config()
            config["data_converter"] = pydantic_data_converter
            client = Client(**config)
            import unittest.mock as mock

            with mock.patch.object(qm_tools, "connect", lambda: _ready(client)):
                with pytest.raises(ValueError, match="no QM job"):
                    await get_qm_job_status("qm-does-not-exist")

    asyncio.run(_run())


async def _ready(client: Client) -> Client:
    """Adapt an already-connected client to the async `connect()` signature."""
    return client
