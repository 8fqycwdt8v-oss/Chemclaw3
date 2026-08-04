"""The durable smoke's own logic, offline.

`make live-jobs` is by definition a thing you run against a live stack, so what is testable here is
not "does the job run" — that is the smoke's job and it needs a broker. What is testable is the
part that decides *whether a green result means anything*, and that part has already been wrong
once: the first version of the wedged-worker check reused the smoke's symmetry numbers on a
different equation, and the engine rejected the payload. The lane reported the failure correctly,
which is the argument for the lane, but a bad probe that reads as a system fault is worth catching
before it ships.

So these pin the invariants that make the smoke honest: the payloads are valid for their own
equations, a run only reports success when every check passed, and — the one that matters most —
the payload varies between runs, because a fixed one would rejoin the previous run's workflow and
pass every check against residue.
"""

from __future__ import annotations

import importlib
from unittest import mock

from chemclaw.cli import live_jobs
from chemclaw.cli.live_jobs import SMOKE_PAYLOAD, WEDGE_PAYLOAD, Check, SmokeRun, report


def _species(payload: dict[str, object]) -> set[str]:
    """Every SMILES the payload's equation names, on both sides of the arrow."""
    reactants: list[str] = payload["reactants"]  # type: ignore[assignment]
    products: list[str] = payload["products"]  # type: ignore[assignment]
    return set(reactants) | set(products)


def test_each_payload_names_a_symmetry_number_for_every_species_in_its_own_equation() -> None:
    """The check that the first wedged-worker payload failed.

    `science.calc.reaction._checked_symmetry_numbers` rejects a map naming a species the equation
    does not contain, and reports no free energy for a species the map omits. Either way the smoke
    stops measuring the durable path and starts measuring its own input.
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
    """The subtlest way this lane could go green while testing nothing.

    A durable job's workflow id is a hash of its payload and a duplicate launch deliberately
    rejoins the existing run rather than recomputing (D-011). With a payload fixed across runs, the
    *second* `make live-jobs` against the same database would start nothing and pass every check
    against the first run's rows — a lane that reports a working durable path it never exercised.
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
