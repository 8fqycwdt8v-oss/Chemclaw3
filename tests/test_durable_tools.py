"""`get_durable_job_status` is the one *status tool* for a finished durable job.

It became the only one in D-118: the QM/DFT job was the last runner with a tool of its own
(`agents/job_status.py`, which knew the `qm-` id prefix and the bespoke `QMJobResult` shape), and
it is a declared `qm` connector job now. Everything a launcher in this system hands an id for
therefore returns `ConnectorJobResult`.

That is what makes the envelope check a *hard* error rather than a degraded answer, and it is the
behaviour worth a test: reporting `completed` with an empty result would tell a chemist their
week-long calculation finished and then withhold the number.

This file used to open "the one place a finished durable job is collected", and so did the module
it tests. That was false — three sites collect one, including the in-turn wait in
`chemclaw.connectors.jobs`, which is a different subsystem and cannot route through an agent tool.
What is single is the decode, and the last test here is the one that keeps it that way.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus

import chemclaw.agent.durable_tools as durable_tools
import chemclaw.connectors.jobs as jobs_module
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
            "summary": ("B3LYP/def2-SVP on CCO: -154.750000 Hartree (no uncertainty established)"),
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


def test_every_collector_of_a_finished_job_answers_a_foreign_result_identically() -> None:
    """The three sites that collect a finished job must not disagree about what a bad one is.

    They did. `completed_job_status` raised a written sentence; `connectors.jobs._await_briefly`
    called `ConnectorJobResult.model_validate` itself and let pydantic's `ValidationError` out.
    That is not an internal detail: a `ValidationError` **is** a `ValueError`, and `ValueError` is
    the family `connectors.server._sanitize_tool_errors` deliberately passes through untouched as
    "a deliberately-worded, caller-safe message" — so the second path relayed
    "2 validation errors for ConnectorJobResult" and pydantic's field dump to a chemist, while the
    first said which id was foreign and why.

    Measured before the fix, on one bad result: path A `ValueError: durable job 'job-1' completed
    but did not return the connector job envelope...`, path C `ValidationError: 2 validation errors
    for ConnectorJobResult`. Both go through `envelope_from_result` now.
    """
    foreign = {"scheduler_job_id": "slurm-77"}

    class _Finished:
        async def result(self) -> Any:
            return foreign

    with pytest.raises(ValueError) as from_the_status_tool:
        durable_tools.completed_job_status("job-1", foreign)
    with pytest.raises(ValueError) as from_the_inline_wait:
        asyncio.run(jobs_module._await_briefly(_Finished(), 5.0, "compare", "job-1"))

    assert type(from_the_status_tool.value) is type(from_the_inline_wait.value)
    assert str(from_the_status_tool.value) == str(from_the_inline_wait.value)
    assert "did not return the connector job envelope" in str(from_the_inline_wait.value)
    # Not pydantic's, which is what used to reach the model from the second path.
    assert "validation error" not in str(from_the_inline_wait.value)


def test_the_envelope_decode_has_exactly_one_definition() -> None:
    """Structural, because the defect was a second copy rather than a wrong one.

    A behavioural test only covers the collectors it knows about; a fourth one added tomorrow
    would reintroduce the divergence silently. `model_validate` on the envelope belongs in
    `envelope_from_result` and nowhere else.
    """
    src = Path(durable_tools.__file__).resolve().parents[1]
    offenders = [
        path.relative_to(src).as_posix()
        for path in sorted(src.rglob("*.py"))
        if path.name != "connector_job.py"
        and "ConnectorJobResult.model_validate" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} decode the connector job envelope themselves; call "
        "`chemclaw.durable.connector_job.envelope_from_result` so every collector answers a "
        "foreign result with the same sentence"
    )
