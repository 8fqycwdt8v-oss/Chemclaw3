"""Behavioral tests for the BoFire-backed BO layer.

The engine converges on a known objective, the neutral types validate inputs, and the campaign
honours direction. Real BoFire, kept small so it stays fast.
"""

import asyncio
import warnings
from pathlib import Path
from typing import Any

import numpy.linalg
import pandas as pd
import pytest
import torch
from bofire.strategies import api as bofire_strategies
from botorch.exceptions.errors import BotorchError, ModelFittingError
from linear_operator.utils.errors import NanError, NotPSDError

from chemclaw.science.bo.engine import SurrogateFitError, initial_candidates, propose_candidates
from chemclaw.science.bo.problem import (
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
from tests.bo_harness import optimize

warnings.filterwarnings("ignore")

_PARAMS: list[Parameter] = [
    ContinuousParameter(name="x1", lower=-2.0, upper=2.0),
    ContinuousParameter(name="x2", lower=-2.0, upper=2.0),
]


def test_minimize_converges_toward_known_optimum() -> None:
    """A smooth bowl with minimum at (1, -0.5) is found to near-zero value."""
    problem = OptimizationProblem(
        parameters=_PARAMS, objectives=[Objective(name="y", direction="minimize")]
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
        parameters=_PARAMS, objectives=[Objective(name="y", direction="maximize")]
    )

    async def evaluate(params: dict[str, ParamValue]) -> float:
        return -((float(params["x1"]) - 0.5) ** 2) - (float(params["x2"]) - 0.25) ** 2

    result = asyncio.run(optimize(problem, evaluate, n_initial=6, n_rounds=10))
    assert result.best.value > -0.3  # close to the maximum of 0


@pytest.mark.parametrize("count", [0, 1])
def test_propose_requires_enough_observations(count: int) -> None:
    """SOBO needs >= 2 observations to fit; fewer is a clear error, not BoFire's opaque one (G4)."""
    problem = OptimizationProblem(parameters=_PARAMS, objectives=[Objective(name="y")])
    observations = [Observation(params={"x1": 0.0, "x2": 0.0}, value=1.0)][:count]
    with pytest.raises(ValueError, match="at least 2 observations"):
        propose_candidates(problem, observations, n=1)


def test_best_of_empty_raises() -> None:
    """No observations is a clear error, not a bare IndexError (G4)."""
    problem = OptimizationProblem(parameters=_PARAMS, objectives=[Objective(name="y")])
    with pytest.raises(ValueError, match="no observations"):
        best_of(problem, [])


def test_space_exhausted_predicate() -> None:
    """Exhaustion: finite space with too few fresh candidates left for a full batch."""
    problem = OptimizationProblem(
        parameters=[CategoricalParameter(name="c", categories=["a", "b"])],
        objectives=[Objective(name="y", direction="maximize")],
    )
    history = [Observation(params={"c": "a"}, value=1.0)]
    assert space_exhausted(problem, None, history, 5) is False  # infinite space never exhausts
    assert space_exhausted(problem, 2, history, 1) is False  # 1 seen + 1 <= 2
    assert space_exhausted(problem, 2, history, 2) is True  # 1 seen + 2 > 2


# What pydantic says when a finite-number constraint is missed, up to the value it was handed.
# Quoted rather than derived, so the regex cannot agree with a field that has stopped checking.
_NOT_FINITE = r"\nvalue\n  Input should be a finite number \[type=finite_number"


@pytest.mark.parametrize(
    ("bad", "fires"),
    [
        (float("nan"), _NOT_FINITE + r", input_value=nan"),
        (float("inf"), _NOT_FINITE + r", input_value=inf"),
        (float("-inf"), _NOT_FINITE + r", input_value=-inf"),
    ],
)
def test_observation_rejects_non_finite_value(bad: float, fires: str) -> None:
    """A NaN/inf objective value is rejected at the boundary, *by the value field*.

    NaN compares false both ways, so it would silently win `best_of`. `match=` asserts which field
    refused; a bare `pytest.raises(ValueError)` is satisfied by any validation failure.
    """
    with pytest.raises(ValueError, match=fires):
        Observation(params={"x1": 0.0}, value=bad)


def _library_problem(categories: list[str]) -> OptimizationProblem:
    """A purely discrete (all-categorical) problem over the given labels."""
    return OptimizationProblem(
        parameters=[CategoricalParameter(name="c", categories=categories)],
        objectives=[Objective(name="y")],
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
    problem = OptimizationProblem(parameters=_PARAMS, objectives=[Objective(name="y")])
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
            objectives=[Objective(name="y")],
        )


def test_a_model_guided_proposal_carries_the_surrogate_belief_a_seed_cannot() -> None:
    """A model-guided proposal carries the surrogate belief a seed cannot.

    BoFire returns `<objective>_pred`/`_sd` from `ask()`. Run against real BoFire, because the claim
    is about its column names. A `RandomStrategy` seed reports `None`, not zero; a SOBO proposal
    carries a positive spread.
    """
    problem = OptimizationProblem(
        parameters=_PARAMS, objectives=[Objective(name="y", direction="minimize")]
    )
    seeds = initial_candidates(problem, 4, seed=3)
    assert [c.predicted_sd for c in seeds] == [None] * 4
    assert [c.predicted_value for c in seeds] == [None] * 4

    observed = [
        Observation(params=c.params, value=float(c.params["x1"]) ** 2, provenance="predicted")
        for c in seeds
    ]
    proposed = propose_candidates(problem, observed, 2, seed=3)
    assert all(c.predicted_sd is not None and c.predicted_sd > 0 for c in proposed), (
        "a fitted surrogate always has a posterior spread at the point it proposes"
    )
    assert all(c.predicted_value is not None for c in proposed)


def test_the_surrogate_belief_survives_evaluation_into_the_history() -> None:
    """The sd is recorded against the point it justified, or it never reaches a note.

    `_evaluate` builds the `Observation` that outlives the `Candidate`, so it must carry the sd too.
    Seed points keep `None`, which separates a model recommendation from a lucky first guess.
    """
    problem = OptimizationProblem(
        parameters=_PARAMS, objectives=[Objective(name="y", direction="minimize")]
    )

    async def evaluate(params: dict[str, ParamValue]) -> float:
        return (float(params["x1"]) - 1.0) ** 2 + (float(params["x2"]) + 0.5) ** 2

    result = asyncio.run(optimize(problem, evaluate, n_initial=3, n_rounds=3))
    seeded, guided = result.history[:3], result.history[3:]
    assert [o.surrogate_sd for o in seeded] == [None] * 3
    assert all(o.surrogate_sd is not None and o.surrogate_sd > 0 for o in guided)


class _RaisingStrategy:
    """A fake BoFire strategy whose `tell`/`ask` always raise one chosen error.

    A degenerate GP fit cannot be forced on demand (gpytorch's jitter absorbs duplicates), so this
    tests what `chemclaw.science.bo.engine` owns: each known library failure class is translated to
    `SurrogateFitError` before leaving the module.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    def tell(self, frame: pd.DataFrame) -> None:
        raise self._error

    def ask(self, n: int) -> pd.DataFrame:
        raise self._error


# One instance of every class `_SURROGATE_FAILURES` names, so the parametrization proves the
# *tuple*, not just its first member.
_SURROGATE_ERRORS: list[Exception] = [
    BotorchError("botorch blew up"),
    ModelFittingError("could not fit after all restarts"),
    NotPSDError("matrix not positive definite after repeatedly adding jitter"),
    NanError("cholesky_cpu: elements of the tensor are NaN"),
    numpy.linalg.LinAlgError("singular matrix"),
    torch.linalg.LinAlgError("cholesky_cpu: failed"),  # type: ignore[attr-defined]
]


_SURROGATE_ERROR_IDS = [type(e).__name__ for e in _SURROGATE_ERRORS]


@pytest.mark.parametrize("error", _SURROGATE_ERRORS, ids=_SURROGATE_ERROR_IDS)
def test_propose_candidates_translates_every_known_surrogate_failure(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Every library exception the fit/acquisition step can raise becomes `SurrogateFitError`.

    So a caller's `except` clause (or a Temporal retry policy) has one name to match.
    """
    problem = OptimizationProblem(parameters=_PARAMS, objectives=[Objective(name="y")])
    observations = [
        Observation(params={"x1": 0.0, "x2": 0.0}, value=1.0),
        Observation(params={"x1": 0.0, "x2": 0.0}, value=1.0),
    ]
    monkeypatch.setattr(bofire_strategies, "map", lambda data_model: _RaisingStrategy(error))
    with pytest.raises(SurrogateFitError, match="duplicate or near-duplicate"):
        propose_candidates(problem, observations, n=1)


def test_initial_candidates_also_translates_surrogate_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seeding path shares the same boundary as the model-guided one.

    `initial_candidates` has no GP, but the translation is written around exception types, so a
    future change to seeding is covered.
    """
    problem = OptimizationProblem(parameters=_PARAMS, objectives=[Objective(name="y")])
    monkeypatch.setattr(
        bofire_strategies, "map", lambda data_model: _RaisingStrategy(ModelFittingError("boom"))
    )
    with pytest.raises(SurrogateFitError):
        initial_candidates(problem, n=1)


def test_propose_candidates_does_not_swallow_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The translation is scoped to the known failure classes, not a blanket `except Exception`.

    A programming bug must surface as itself, not be misdiagnosed as bad data.
    """
    problem = OptimizationProblem(parameters=_PARAMS, objectives=[Objective(name="y")])
    observations = [
        Observation(params={"x1": 0.0, "x2": 0.0}, value=1.0),
        Observation(params={"x1": 0.0, "x2": 0.0}, value=1.0),
    ]

    class _BoomStrategy:
        def tell(self, frame: Any) -> None:
            raise KeyError("unrelated bug")

    monkeypatch.setattr(bofire_strategies, "map", lambda data_model: _BoomStrategy())
    with pytest.raises(KeyError):
        propose_candidates(problem, observations, n=1)


def test_the_in_process_campaign_loop_has_no_definition_under_src() -> None:
    """The in-process campaign loop has no definition under `src/`.

    `optimize` and `molecule_library_problem` had no production caller, so they live in
    `tests/bo_harness.py` (`D-2026-09-07-a-driver-with-no-caller-is-not-a-capability`). Nothing else
    would notice them returning to `src/`; they would import, type-check and be tested.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src"
    defined: list[str] = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name in {
                "optimize",
                "molecule_library_problem",
            }:
                defined.append(f"{path.relative_to(src)}: {node.name}")

    assert not defined, (
        f"an in-process BO driver is back under src/ with no configuration reaching it: {defined}. "
        "A campaign ships as `connectors/bo/workflows.BoCampaignWorkflow`; if an in-process loop "
        "is wanted again, bring the caller that enters it in the same change."
    )
