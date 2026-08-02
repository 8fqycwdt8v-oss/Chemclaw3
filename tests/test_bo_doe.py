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


# --- the reduced design (the "seven factors, 96 wells" question) ----------------------------


def _seven_two_level_factors() -> OptimizationProblem:
    """Seven two-level factors: 128 runs at full grid, which does not fit a 96-well plate."""
    return OptimizationProblem(
        parameters=[
            CategoricalParameter(name=f"factor_{i}", categories=["low", "high"]) for i in range(7)
        ],
        objective=Objective(name="yield", direction="maximize"),
    )


def test_a_generator_actually_halves_the_design() -> None:
    """The capability itself: 128 runs do not fit 96 wells, 64 do.

    Deliberately not asserted as "n_generators was passed through": BoFire's
    `FractionalFactorialStrategy` fractionates only the *continuous* half of a domain and always
    crosses the categorical half in full, so forwarding `n_generators` to an all-categorical domain
    returns all 128 rows and changes nothing. Only the run count can tell the two apart.
    """
    problem = _seven_two_level_factors()
    assert len(factorial_design(problem).runs) == 128
    assert len(factorial_design(problem, n_generators=1).runs) == 64
    assert len(factorial_design(problem, n_generators=3).runs) == 16


def test_a_reduced_design_still_uses_the_real_levels_and_every_factor() -> None:
    """A halved design must halve the *runs*, not quietly drop a factor or emit encoded values."""
    design = factorial_design(_seven_two_level_factors(), n_generators=3)
    names = {f"factor_{i}" for i in range(7)}
    assert all(set(run) == names for run in design.runs)
    assert {value for run in design.runs for value in run.values()} == {"low", "high"}
    # A fractional design is a set of distinct points; a repeat would be wasted plate space.
    assert len({tuple(sorted(run.items())) for run in design.runs}) == 16


def test_the_stated_resolution_matches_the_textbook_designs() -> None:
    """Resolution is the claim the chemist acts on, so it is pinned against known designs.

    2^(7-1) is resolution VII, 2^(7-3) is IV and 2^(7-4) is III — standard results. Getting this
    wrong is worse than not reporting it: a design claimed resolution IV but really III has main
    effects confounded with two-factor interactions, and every conclusion drawn from it is unsafe.
    """
    problem = _seven_two_level_factors()
    assert factorial_design(problem, n_generators=1).resolution == 7
    assert factorial_design(problem, n_generators=3).resolution == 4
    assert factorial_design(problem, n_generators=4).resolution == 3


def test_the_design_says_whether_it_is_exhaustive() -> None:
    """The point of the whole item: a fraction cannot be presented as the whole screen.

    `summary` is a `computed_field`, so it is in the serialized payload the model composes its
    answer from — not merely on the Python object.
    """
    problem = _seven_two_level_factors()
    full = factorial_design(problem).model_dump()
    reduced = factorial_design(problem, n_generators=3).model_dump()
    assert full["resolution"] is None
    assert "Exhaustive" in full["summary"]
    assert reduced["resolution"] == 4
    assert "NOT exhaustive" in reduced["summary"]
    assert "resolution IV" in reduced["summary"]


def test_a_three_level_factor_is_refused_rather_than_crossed_in_full() -> None:
    """The same standard as the continuous refusal: no design that omits what it claims to cover.

    A two-level fractional design cannot express a three-level factor. Crossing it in full instead
    would return a design whose stated resolution described only part of it.
    """
    problem = OptimizationProblem(
        parameters=[
            CategoricalParameter(name="solvent", categories=["THF", "toluene", "MeCN"]),
            CategoricalParameter(name="base", categories=["K2CO3", "Cs2CO3"]),
            CategoricalParameter(name="ligand", categories=["PPh3", "dppf"]),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )
    with pytest.raises(ValueError, match="solvent"):
        factorial_design(problem, n_generators=1)
    # …and the full grid over the same problem is unaffected.
    assert len(factorial_design(problem).runs) == 12


def test_an_impossible_reduction_is_a_plain_error_not_a_validation_dump() -> None:
    """Two factors cannot be halved: the message has to be readable by the model that retries."""
    problem = OptimizationProblem(
        parameters=[
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
            CategoricalParameter(name="base", categories=["K2CO3", "Cs2CO3"]),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )
    with pytest.raises(ValueError, match="confounded"):
        factorial_design(problem, n_generators=1)


def test_a_negative_generator_count_is_a_plain_error_too() -> None:
    """Left to BoFire it surfaces as a ValidationError about a generator the caller never wrote."""
    with pytest.raises(ValueError, match="n_generators"):
        factorial_design(_seven_two_level_factors(), n_generators=-1)


def test_the_tool_can_ask_for_a_reduced_design() -> None:
    """The agent surface, not just the engine: `n_generators` has to be reachable from a tool call.

    The design was expressible in `science/` and unreachable from the connector before this.
    """
    design = asyncio.run(generate_screening_design(_seven_two_level_factors(), n_generators=1))
    assert len(design.runs) == 64
    assert design.resolution == 7
