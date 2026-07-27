"""The conformer-ensemble agent tools drive a real durable job (D-092).

Mirrors `test_qm_tools.py`'s bootstrap and coverage: submit returns an id without blocking,
status reports the completed result, and re-submitting an identical job is idempotent.
"""

import asyncio
from typing import Any

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

import agents.conformer_tools as conformer_tools
from agents.conformer_tools import get_conformer_job_status, submit_conformer_ensemble_job
from chemclaw.config import settings
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.conformer_activities import prepare_conformer_input, run_conformer_ensemble
from workflows.conformer_job import ConformerEnsembleWorkflow

_ACTIVITIES = [prepare_conformer_input, run_conformer_ensemble]


def test_submit_returns_id_and_status_yields_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """submit_conformer_ensemble_job returns an id immediately; status later has the result."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            monkeypatch.setattr(conformer_tools, "connect", lambda: _ready(client))

            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[ConformerEnsembleWorkflow],
                activities=_ACTIVITIES,
            ):
                job_id = await submit_conformer_ensemble_job("CCCCO")
                assert job_id.startswith("conformer-")

                # Idempotent: same inputs → same id, no duplicate job.
                again = await submit_conformer_ensemble_job("CCCCO")
                assert again == job_id

                await client.get_workflow_handle(job_id).result()
                status = await get_conformer_job_status(job_id)
                assert status.status == WorkflowExecutionStatus.COMPLETED.name
                assert status.result is not None
                assert status.result.ensemble.smiles == "CCCCO"

    asyncio.run(_run())


def test_status_of_unknown_job_raises() -> None:
    """Polling a non-existent id is a clear error, not a crash (gate G4)."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            import unittest.mock as mock

            with mock.patch.object(conformer_tools, "connect", lambda: _ready(client)):
                with pytest.raises(ValueError, match="no conformer job"):
                    await get_conformer_job_status("conformer-does-not-exist")

    asyncio.run(_run())


def test_submit_pins_completed_safe_reuse_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit pins ALLOW_DUPLICATE_FAILED_ONLY so a completed job is never recomputed."""
    captured: dict[str, Any] = {}

    class _FakeHandle:
        id = "conformer-fake"

    class _FakeClient:
        async def start_workflow(self, *args: Any, **kwargs: Any) -> _FakeHandle:
            captured.update(kwargs)
            return _FakeHandle()

    async def _fake_connect() -> Any:
        return _FakeClient()

    monkeypatch.setattr(conformer_tools, "connect", _fake_connect)
    assert asyncio.run(submit_conformer_ensemble_job("CCO")) == "conformer-fake"
    assert captured["id_reuse_policy"] is WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    assert captured["task_queue"] == settings.background_task_queue


def test_idempotent_resubmit_returns_existing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate-start error is treated as the existing job's id, not a crash."""

    class _FakeClient:
        async def start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise WorkflowAlreadyStartedError("dup", kwargs["id"], run_id=None)

    async def _fake_connect() -> Any:
        return _FakeClient()

    monkeypatch.setattr(conformer_tools, "connect", _fake_connect)
    job_id = asyncio.run(submit_conformer_ensemble_job("CCO"))
    assert job_id.startswith("conformer-")


def test_status_of_foreign_workflow_is_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid non-conformer workflow id is a clear error, not an opaque pydantic crash (G4)."""

    class _FakeDescription:
        status = WorkflowExecutionStatus.COMPLETED

    class _FakeHandle:
        async def describe(self) -> _FakeDescription:
            return _FakeDescription()

        async def result(self) -> dict[str, Any]:
            return {"best": {"params": {}, "value": 1.0}}  # a BO result, not a conformer one

    class _FakeClient:
        def get_workflow_handle(self, job_id: str) -> _FakeHandle:
            return _FakeHandle()

    async def _fake_connect() -> Any:
        return _FakeClient()

    monkeypatch.setattr(conformer_tools, "connect", _fake_connect)
    with pytest.raises(ValueError, match="not a conformer job"):
        asyncio.run(get_conformer_job_status("bo-campaign-1"))


async def _ready(client: Client) -> Client:
    """Adapt an already-connected client to the async `connect()` signature."""
    return client
