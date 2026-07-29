"""`get_durable_job_status` is the one place a finished durable job is collected.

It became the *only* one in D-118: the QM/DFT job was the last runner with a status tool of its
own (`agents/job_status.py`, which knew the `qm-` id prefix and the bespoke `QMJobResult` shape),
and it is a declared `qm` connector job now. Everything a launcher in this system hands an id for
therefore returns `ConnectorJobResult`.

That is what makes the envelope check here a *hard* error rather than a degraded answer, and it is
the behaviour worth a test: reporting `completed` with an empty result would tell a chemist their
week-long calculation finished and then withhold the number.
"""

import asyncio
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus

import agents.durable_tools as durable_tools
from agents.durable_tools import get_durable_job_status


class _Description:
    def __init__(self, status: WorkflowExecutionStatus) -> None:
        self.status = status


class _Handle:
    """A workflow handle with a scripted status and result."""

    def __init__(self, status: WorkflowExecutionStatus, result: Any) -> None:
        self._status = status
        self._result = result

    async def describe(self) -> _Description:
        return _Description(self._status)

    async def result(self) -> Any:
        return self._result


class _Client:
    def __init__(self, handle: _Handle) -> None:
        self._handle = handle

    def get_workflow_handle(self, job_id: str) -> _Handle:
        return self._handle


def _with_result(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    """Point the tool's `connect()` seam at a client whose job completed with `result`."""

    async def _connect() -> _Client:
        return _Client(_Handle(WorkflowExecutionStatus.COMPLETED, result))

    monkeypatch.setattr(durable_tools, "connect", _connect)


def test_a_completed_job_hands_over_its_result_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the envelope: "completed" and the answer arrive together, not in two calls."""
    _with_result(
        monkeypatch,
        {
            "summary": "B3LYP/def2-SVP on CCO: -154.750000 Hartree (converged)",
            "data": {"total_energy_hartree": -154.75, "converged": True},
        },
    )
    status = asyncio.run(get_durable_job_status("qm-compute_dft_energy-abc"))
    assert status.status == "completed"
    assert status.summary is not None and "Hartree" in status.summary
    assert status.result["total_energy_hartree"] == -154.75


def test_a_completed_job_that_is_not_the_envelope_is_a_hard_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign result shape raises instead of degrading to a bare `completed`.

    The fallback existed for exactly one job — the HPC/DFT run, which returned `QMJobResult` and
    was collected by its own tool. With that job on the connector seam nothing legitimately
    returns anything else, so a non-envelope result means the id belongs to a workflow no launcher
    in this system started, and the honest answer is to say so rather than to report a finished
    job with no findings.
    """
    _with_result(monkeypatch, {"scheduler_job_id": "slurm-77"})
    with pytest.raises(ValueError, match="did not return the connector job envelope"):
        asyncio.run(get_durable_job_status("some-foreign-workflow"))


def test_a_running_job_reports_the_status_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A job still running is never asked for a result — a DFT run holds one open for days."""

    async def _connect() -> _Client:
        return _Client(_Handle(WorkflowExecutionStatus.RUNNING, None))

    monkeypatch.setattr(durable_tools, "connect", _connect)
    status = asyncio.run(get_durable_job_status("qm-compute_dft_energy-abc"))
    assert status.status == "running"
    assert status.summary is None and status.result == {}
