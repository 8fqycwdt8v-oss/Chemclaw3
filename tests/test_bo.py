"""Behavioral tests for the BoFire-backed BO layer (plan Phase 1d).

Proves the engine converges on a known objective (the CHECKMATE 1d spike), that
our neutral types validate inputs, and that the campaign honors direction. Real
BoFire runs; kept small so it stays fast.
"""

import asyncio
import warnings

import pytest

from bo.campaign import optimize
from bo.engine import initial_candidates, propose_candidates
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


@pytest.mark.parametrize("count", [0, 1])
def test_propose_requires_enough_observations(count: int) -> None:
    """SOBO needs >= 2 observations to fit; fewer is a clear error, not BoFire's opaque one (G4)."""
    problem = OptimizationProblem(parameters=_PARAMS, objective=Objective(name="y"))
    observations = [Observation(params={"x1": 0.0, "x2": 0.0}, value=1.0)][:count]
    with pytest.raises(ValueError, match="at least 2 observations"):
        propose_candidates(problem, observations, n=1)


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


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_observation_rejects_non_finite_value(bad: float) -> None:
    """A NaN/inf objective value is rejected at the boundary (G4).

    NaN compares false in both directions, so it would silently win `best_of`
    and poison the campaign result instead of failing the bad evaluation.
    """
    with pytest.raises(ValueError):
        Observation(params={"x1": 0.0}, value=bad)


def _library_problem(categories: list[str]) -> OptimizationProblem:
    """A purely discrete (all-categorical) problem over the given labels."""
    return OptimizationProblem(
        parameters=[CategoricalParameter(name="c", categories=categories)],
        objective=Objective(name="y"),
    )


def test_initial_candidates_are_distinct_in_discrete_space() -> None:
    """Seeding a finite space never proposes the same candidate twice.

    A duplicate seed wastes evaluation budget (a repeated wet-lab experiment for
    a measured campaign) and fits the surrogate on fewer points than paid for.
    """
    candidates = initial_candidates(_library_problem(["a", "b", "c"]), 3)
    assert {c.params["c"] for c in candidates} == {"a", "b", "c"}


def test_initial_candidates_reject_overdrawn_discrete_space() -> None:
    """Asking for more seed points than the space holds is a clear error, not silence."""
    with pytest.raises(ValueError, match="discrete space"):
        initial_candidates(_library_problem(["a", "b", "c"]), 5)


def test_initial_candidates_seed_is_a_per_call_seam() -> None:
    """Different seeds give different seed designs; the default stays reproducible."""
    problem = OptimizationProblem(parameters=_PARAMS, objective=Objective(name="y"))
    default = initial_candidates(problem, 3)
    assert initial_candidates(problem, 3) == default  # config default is stable
    assert initial_candidates(problem, 3, seed=7) != default  # replicates can vary
    assert initial_candidates(problem, 3, seed=7) == initial_candidates(problem, 3, seed=7)


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
