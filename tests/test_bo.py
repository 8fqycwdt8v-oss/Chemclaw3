"""Behavioral tests for the BoFire-backed BO layer (plan Phase 1d).

Proves the engine converges on a known objective (the CHECKMATE 1d spike), that
our neutral types validate inputs, and that the campaign honors direction. Real
BoFire runs; kept small so it stays fast.
"""

import asyncio
import warnings

import pytest

from bo.campaign import optimize
from bo.engine import propose_candidates
from bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
    Parameter,
    ParamValue,
    best_of,
    space_exhausted,
)

warnings.filterwarnings("ignore")

_PARAMS: list[Parameter] = [
    ContinuousParameter(name="x1", lower=-2.0, upper=2.0),
    ContinuousParameter(name="x2", lower=-2.0, upper=2.0),
]


def test_minimize_converges_toward_known_optimum() -> None:
    """A smooth bowl with minimum at (1, -0.5) is found to near-zero value."""
    problem = OptimizationProblem(
        parameters=_PARAMS, objective=Objective(name="y", direction="minimize")
    )

    async def evaluate(params: dict[str, ParamValue]) -> float:
        return (float(params["x1"]) - 1.0) ** 2 + (float(params["x2"]) + 0.5) ** 2

    result = asyncio.run(optimize(problem, evaluate, n_initial=6, n_rounds=10))
    assert result.best.value < 0.3  # well below a random guess in this box
    assert len(result.history) == 16  # 6 seed + 10 rounds x batch 1
    assert result.best.provenance == "predicted"


def test_maximize_direction_is_honored() -> None:
    """Maximizing a concave function finds a high value near its peak of 0."""
    problem = OptimizationProblem(
        parameters=_PARAMS, objective=Objective(name="y", direction="maximize")
    )

    async def evaluate(params: dict[str, ParamValue]) -> float:
        return -((float(params["x1"]) - 0.5) ** 2) - (float(params["x2"]) - 0.25) ** 2

    result = asyncio.run(optimize(problem, evaluate, n_initial=6, n_rounds=10))
    assert result.best.value > -0.3  # close to the maximum of 0


def test_propose_requires_observations() -> None:
    """SOBO needs data to fit; empty observations is a clear error (gate G4)."""
    problem = OptimizationProblem(parameters=_PARAMS, objective=Objective(name="y"))
    with pytest.raises(ValueError, match="at least one observation"):
        propose_candidates(problem, [], n=1)


def test_best_of_empty_raises() -> None:
    """No observations is a clear error, not a bare IndexError (G4)."""
    problem = OptimizationProblem(parameters=_PARAMS, objective=Objective(name="y"))
    with pytest.raises(ValueError, match="no observations"):
        best_of(problem, [])


def test_space_exhausted_predicate() -> None:
    """Exhaustion: finite space with too few fresh candidates left for a full batch."""
    history = [Observation(params={"c": "a"}, value=1.0)]
    assert space_exhausted(None, history, 5) is False  # infinite space never exhausts
    assert space_exhausted(2, history, 1) is False  # 1 seen + 1 <= 2
    assert space_exhausted(2, history, 2) is True  # 1 seen + 2 > 2


def test_problem_validation() -> None:
    """Inverted bounds and duplicate parameter names are rejected up front."""
    with pytest.raises(ValueError, match="lower must be < upper"):
        ContinuousParameter(name="x", lower=1.0, upper=1.0)
    with pytest.raises(ValueError, match="categories must be unique"):
        CategoricalParameter(name="cat", categories=["a", "a"])
    with pytest.raises(ValueError, match="unique"):
        OptimizationProblem(
            parameters=[
                ContinuousParameter(name="x", lower=0.0, upper=1.0),
                ContinuousParameter(name="x", lower=0.0, upper=1.0),
            ],
            objective=Objective(name="y"),
        )
