"""Tests for the full-factorial categorical screening design (D-092).

BoFire runs in-process (no Temporal), the same discipline as `test_bo_tools.py`.
"""

import asyncio

import pytest

from chemclaw.connectors.bo.server.tools import generate_screening_design
from chemclaw.science.bo.engine import factorial_design
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    OptimizationProblem,
)


def _screening_problem() -> OptimizationProblem:
    """Two categorical factors: solvent (2 levels) x base (3 levels) = 6 combinations."""
    return OptimizationProblem(
        parameters=[
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
            CategoricalParameter(name="base", categories=["K2CO3", "Cs2CO3", "Et3N"]),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )


def test_factorial_design_enumerates_every_combination() -> None:
    """The design has exactly the Cartesian product of the categorical levels, each run distinct."""
    design = factorial_design(_screening_problem())
    assert len(design.runs) == 6
    seen = {(run["solvent"], run["base"]) for run in design.runs}
    assert len(seen) == 6
    assert all(run["solvent"] in {"THF", "toluene"} for run in design.runs)
    assert all(run["base"] in {"K2CO3", "Cs2CO3", "Et3N"} for run in design.runs)


def test_factorial_design_rejects_a_continuous_parameter() -> None:
    """A continuous factor is rejected rather than silently dropped from the design (gate G4)."""
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=100.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )
    with pytest.raises(ValueError, match="temperature"):
        factorial_design(problem)


def test_generate_screening_design_tool_matches_the_engine() -> None:
    """The agent tool wraps `factorial_design` directly (off the event loop)."""
    design = asyncio.run(generate_screening_design(_screening_problem()))
    assert len(design.runs) == 6
