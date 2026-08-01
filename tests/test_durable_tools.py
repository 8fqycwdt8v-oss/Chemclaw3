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

import chemclaw.agent.durable_tools as durable_tools
from chemclaw.agent.durable_tools import get_durable_job_status


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
            # Kept in step with what `qm.workflows._envelope` actually renders (F8-T1) — this is
            # fixture data rather than a pin on that format, and a stale sample here is how a
            # reader learns the wrong shape.
            "summary": (
                "B3LYP/def2-SVP on CCO: -154.750000 Hartree (no uncertainty established)"
            ),
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


class _ExpiredHandle:
    """A handle for an id Temporal no longer knows: `describe` fails the way the SDK fails."""

    async def describe(self) -> _Description:
        from temporalio.service import RPCError, RPCStatusCode

        raise RPCError("workflow execution not found", RPCStatusCode.NOT_FOUND, b"")


def _expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the tool at a Temporal that has forgotten every id."""

    class _ExpiredClient:
        def get_workflow_handle(self, job_id: str) -> _ExpiredHandle:
            return _ExpiredHandle()

    async def _connect() -> _ExpiredClient:
        return _ExpiredClient()

    monkeypatch.setattr(durable_tools, "connect", _connect)


def test_a_job_whose_history_expired_is_still_collected_from_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap D-157 closes: an old job id must not read as "no such job".

    Temporal expires a closed workflow's history on the namespace's retention clock, and before
    the durable record that took the campaign's result with it — so the tool told a chemist their
    campaign never existed. Now the record answers, and it answers with the *reason* too, which is
    the context that makes a months-old result interpretable.
    """
    from chemclaw.durable.job_record import JobRecord

    async def _lookup(job_id: str) -> JobRecord:
        return JobRecord(
            job_id=job_id,
            connector="bo",
            job="start_optimization_campaign",
            rationale="the Tuesday batch stalled at 60%",
            requested_by="oid-42",
            summary="campaign finished after 9 evaluation(s)",
            result={"best": {"value": -1.2}, "history": [{"value": -3.0}, {"value": -1.2}]},
        )

    _expired(monkeypatch)
    monkeypatch.setattr(durable_tools, "lookup_job_record", _lookup)

    status = asyncio.run(get_durable_job_status("bo-start_optimization_campaign-abc"))
    assert status.status == "completed"
    assert status.result["history"] == [{"value": -3.0}, {"value": -1.2}]
    assert status.rationale == "the Tuesday batch stalled at 60%"


def test_an_id_nobody_has_a_record_of_is_still_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired and never-existed must stay distinguishable — a typo is not a stored result."""

    async def _lookup(job_id: str) -> None:
        return None

    _expired(monkeypatch)
    monkeypatch.setattr(durable_tools, "lookup_job_record", _lookup)
    with pytest.raises(ValueError, match="no durable job with id"):
        asyncio.run(get_durable_job_status("bo-typo-999"))


def test_finding_past_jobs_reports_what_ran_and_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cross-session entry point: a run is findable by the words its reason used.

    Without this the only handle on a past job was its id, which lives in the transcript of the
    conversation that started it — so a *new* session could not reach a single thing this system
    had ever computed.
    """
    from chemclaw.durable.job_record import JobRecordSummary

    seen: dict[str, str] = {}

    async def _search(text: str, connector: str) -> list[JobRecordSummary]:
        seen.update(text=text, connector=connector)
        return [
            JobRecordSummary(
                job_id="bo-start_optimization_campaign-abc",
                connector="bo",
                job="start_optimization_campaign",
                rationale="the Tuesday batch stalled at 60%",
                summary="campaign finished after 9 evaluation(s)",
            )
        ]

    monkeypatch.setattr(durable_tools, "search_job_records", _search)
    hits = asyncio.run(durable_tools.find_past_jobs("stalled", "bo"))
    assert seen == {"text": "stalled", "connector": "bo"}
    assert hits[0].rationale == "the Tuesday batch stalled at 60%"
    # The listing carries no result blob: a campaign's history is one lookup away, not in every hit.
    assert not hasattr(hits[0], "result")
