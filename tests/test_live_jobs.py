"""The durable smoke's own logic, offline.

Pins what makes `make live-jobs` honest: payloads are valid for their own equations, a run
reports success only when every check passed, and the payload varies between runs so a rerun
cannot rejoin the previous workflow and pass against residue.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from temporalio.client import WorkflowExecutionStatus

from chemclaw.cli import live_jobs
from chemclaw.cli.live_jobs import SMOKE_PAYLOAD, WEDGE_PAYLOAD, Check, SmokeRun, report


def _species(payload: dict[str, object]) -> set[str]:
    """Every SMILES the payload's equation names, on both sides of the arrow."""
    reactants: list[str] = payload["reactants"]  # type: ignore[assignment]
    products: list[str] = payload["products"]  # type: ignore[assignment]
    return set(reactants) | set(products)


def test_each_payload_names_a_symmetry_number_for_every_species_in_its_own_equation() -> None:
    """Each payload names a symmetry number for every species in its own equation.

    `science.calc.reaction._checked_symmetry_numbers` rejects an extra species and omits free energy
    for a missing one; either way the smoke would measure its own input.
    """
    for name, payload in (("smoke", SMOKE_PAYLOAD), ("wedge", WEDGE_PAYLOAD)):
        sigmas = payload["symmetry_numbers"]
        assert isinstance(sigmas, dict)
        assert set(sigmas) == _species(payload), f"{name} payload: symmetry numbers ≠ species"


def test_the_two_payloads_are_different_reactions() -> None:
    """The wedged-worker check must not be answerable from the cache the smoke just filled.

    Sharing an equation would make it derive the same workflow id, rejoin the completed run and
    return a result immediately — so it would assert the pending path while never reaching it.
    """
    assert _species(SMOKE_PAYLOAD) != _species(WEDGE_PAYLOAD)


def test_the_payload_varies_between_runs_so_a_rerun_cannot_pass_on_residue() -> None:
    """The payload varies between runs, so a rerun cannot pass on residue.

    A workflow id is a hash of its payload and a duplicate launch rejoins the existing run, so a
    fixed payload would start nothing on a second run and pass against the first run's rows.
    """
    assert SMOKE_PAYLOAD["temperature_k"] == live_jobs._RUN_TEMPERATURE_K
    assert WEDGE_PAYLOAD["temperature_k"] == live_jobs._RUN_TEMPERATURE_K

    # Reimport under two different clocks and require two different payloads. Recomputing the
    # expression by hand here would only assert that this test can do arithmetic; reloading proves
    # the *module* produces a different launch on a later run, which is the actual claim.
    temperatures = set()
    for stamp in (1_700_000_000, 1_700_000_007):
        with mock.patch("time.time", lambda s=stamp: float(s)):
            reloaded = importlib.reload(live_jobs)
            temperatures.add(reloaded.SMOKE_PAYLOAD["temperature_k"])
    importlib.reload(live_jobs)
    assert len(temperatures) == 2, "two runs at different times must launch different workflows"


def test_a_run_is_ok_only_when_every_check_passed() -> None:
    """One failed check fails the run — the exit code follows this and nothing else."""
    passing = Check(name="a", passed=True, observed="")
    failing = Check(name="b", passed=False, observed="")
    assert SmokeRun(checks=[passing, passing]).ok is True
    assert SmokeRun(checks=[passing, failing]).ok is False


def test_the_report_names_every_check_and_its_observation() -> None:
    """A green run that cannot say what it saw is a green run nobody can audit later."""
    run = SmokeRun(
        workflow_id="calc-compute_reaction_energy-abc",
        checks=[
            Check(name="workflow reached COMPLETED", passed=True, observed="COMPLETED, started x"),
            Check(name="audit chain verifies", passed=False, observed="chain broken at row 3"),
        ],
        seconds=1.5,
    )
    text = report(run)
    assert "calc-compute_reaction_energy-abc" in text
    assert "COMPLETED, started x" in text
    assert "chain broken at row 3" in text
    assert "**FAIL**" in text
    assert "1/2 checks passed" in text


class _Broker:
    """A Temporal stand-in that reports a scripted sequence of states, one per describe."""

    def __init__(self, states: list[WorkflowExecutionStatus]) -> None:
        self.states = states
        self.asked = 0

    async def connect(self) -> _Broker:
        return self

    def get_workflow_handle(self, workflow_id: str) -> _Broker:
        return self

    async def describe(self) -> SimpleNamespace:
        state = self.states[min(self.asked, len(self.states) - 1)]
        self.asked += 1
        return SimpleNamespace(status=state, start_time=datetime(2026, 9, 27, tzinfo=UTC))


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(seconds: float) -> None:
        return None

    monkeypatch.setattr("chemclaw.cli.live_jobs.asyncio.sleep", instant)


def test_a_launch_that_returned_pending_is_waited_for_before_it_is_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-09-27 false FAIL: a 20.2 s first launch came back pending and read RUNNING.

    The launcher returning a bare id past `inline_wait_seconds` is the designed pending outcome, so
    the check has to poll the broker to a terminal state before it describes one.
    """
    running, done = WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.COMPLETED
    broker = _Broker([running, running, done])
    monkeypatch.setattr(live_jobs, "temporal_connect", broker.connect)
    _no_sleep(monkeypatch)

    check = asyncio.run(live_jobs.check_workflow_completed(SmokeRun(workflow_id="wf")))
    assert check.passed, check.observed
    assert check.observed.startswith("COMPLETED")


def test_the_wait_is_bounded_by_the_setting_and_reports_the_state_it_ended_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow that never finishes is reported stuck, not waited on forever."""
    from chemclaw.core.config import settings

    broker = _Broker([WorkflowExecutionStatus.RUNNING])
    monkeypatch.setattr(live_jobs, "temporal_connect", broker.connect)
    monkeypatch.setattr(settings, "live_jobs_terminal_wait_seconds", 0.05)

    check = asyncio.run(live_jobs.check_workflow_completed(SmokeRun(workflow_id="wf")))
    assert not check.passed
    assert "RUNNING after waiting" in check.observed


def test_a_terminal_failure_ends_the_wait_rather_than_being_polled_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED is an answer: it is reported as itself, at once, not as a timeout."""
    broker = _Broker([WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.FAILED])
    monkeypatch.setattr(live_jobs, "temporal_connect", broker.connect)
    _no_sleep(monkeypatch)

    status, _ = asyncio.run(live_jobs._await_terminal("wf"))
    assert status == WorkflowExecutionStatus.FAILED
    assert broker.asked == 2
