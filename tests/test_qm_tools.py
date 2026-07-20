"""The agent tools drive a real durable job (plan steps 1.5, 1.6).

Server-backed: runs on Temporal's time-skipping server in CI, skips in the
offline sandbox. Proves submit returns a job id without blocking, status reports
the completed result, and re-submitting an identical job is idempotent.
"""

import asyncio
from typing import Any

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.worker import Worker

import agents.qm_tools as qm_tools
from agents.qm_tools import get_qm_job_status, submit_qm_job
from chemclaw.config import settings
from tests.temporal_env import QM_ACTIVITIES, pydantic_client, start_env_or_skip
from workflows.qm_job import QMJobWorkflow


def test_submit_returns_id_and_status_yields_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """submit_qm_job returns an id immediately; get_qm_job_status later has the result."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            # The tools open their own client via connect(); point it at the env.
            monkeypatch.setattr(qm_tools, "connect", lambda: _ready(client))

            async with Worker(
                client,
                task_queue=settings.hpc_task_queue,
                workflows=[QMJobWorkflow],
                activities=QM_ACTIVITIES,
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

                # Idempotent after completion too (D-011): the stored result is
                # returned by id, never recomputed by a fresh workflow run.
                after_done = await submit_qm_job("CCO", "B3LYP", "def2-SVP")
                assert after_done == job_id
                still_done = await get_qm_job_status(job_id)
                assert still_done.status == WorkflowExecutionStatus.COMPLETED.name

    asyncio.run(_run())


def test_status_of_unknown_job_raises() -> None:
    """Polling a non-existent id is a clear error, not a crash (gate G4)."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            import unittest.mock as mock

            with mock.patch.object(qm_tools, "connect", lambda: _ready(client)):
                with pytest.raises(ValueError, match="no QM job"):
                    await get_qm_job_status("qm-does-not-exist")

    asyncio.run(_run())


def test_submit_pins_completed_safe_reuse_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit pins ALLOW_DUPLICATE_FAILED_ONLY so a completed job is never recomputed.

    Offline-runnable pin of the D-011 guarantee: the default reuse policy rejects a
    duplicate id only while the workflow is OPEN and would silently restart (and
    fully recompute) a completed job on re-submit.
    """
    captured: dict[str, Any] = {}

    class _FakeHandle:
        id = "qm-fake"

    class _FakeClient:
        async def start_workflow(self, *args: Any, **kwargs: Any) -> _FakeHandle:
            captured.update(kwargs)
            return _FakeHandle()

    async def _fake_connect() -> Any:
        return _FakeClient()

    monkeypatch.setattr(qm_tools, "connect", _fake_connect)
    assert asyncio.run(submit_qm_job("CCO", "B3LYP", "def2-SVP")) == "qm-fake"
    assert captured["id_reuse_policy"] is WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY


def test_status_of_foreign_workflow_is_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid non-QM workflow id is a clear error, not an opaque pydantic crash (G4)."""

    class _FakeDescription:
        status = WorkflowExecutionStatus.COMPLETED

    class _FakeHandle:
        async def describe(self) -> _FakeDescription:
            return _FakeDescription()

        async def result(self) -> dict[str, Any]:
            return {"best": {"params": {}, "value": 1.0}}  # a BO result, not a QM one

    class _FakeClient:
        def get_workflow_handle(self, job_id: str) -> _FakeHandle:
            return _FakeHandle()

    async def _fake_connect() -> Any:
        return _FakeClient()

    monkeypatch.setattr(qm_tools, "connect", _fake_connect)
    with pytest.raises(ValueError, match="not a QM job"):
        asyncio.run(get_qm_job_status("bo-campaign-1"))


async def _ready(client: Client) -> Client:
    """Adapt an already-connected client to the async `connect()` signature."""
    return client
