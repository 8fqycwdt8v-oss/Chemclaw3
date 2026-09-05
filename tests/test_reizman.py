"""Behavioral tests for the Reizman Suzuki reaction benchmark (plan step 1d.3).

Proves the real dataset wires into our BO layer: the surrogate learns the data,
the problem has the expected mixed variables, and a BoFire campaign over it finds
a high-yield region (beats the dataset median) — reaction-condition optimization
end to end.
"""

import asyncio
import warnings

import pytest

from chemclaw.science.bo.benchmarks.reizman_suzuki import (
    YieldSurrogate,
    build_problem,
    load_benchmark,
    load_dataset,
)
from chemclaw.science.bo.campaign import optimize
from chemclaw.science.bo.problem import CategoricalParameter

warnings.filterwarnings("ignore")


def test_problem_has_mixed_variables() -> None:
    """The problem exposes the catalyst as categorical and three continuous conditions."""
    problem = build_problem(load_dataset())
    by_name = {p.name: p for p in problem.parameters}
    assert isinstance(by_name["catalyst"], CategoricalParameter)
    assert len(by_name["catalyst"].categories) == 8
    assert {"t_res", "temperature", "catalyst_loading"} <= set(by_name)
    assert problem.objective.direction == "maximize"


def test_surrogate_learns_the_data() -> None:
    """The surrogate reproduces a high-yield training row reasonably well."""
    frame = load_dataset()
    surrogate = YieldSurrogate.fit(frame)
    best_row = frame.loc[frame["yld"].idxmax()]
    predicted = surrogate.predict(best_row.to_dict())
    assert predicted > 0.7 * best_row["yld"]  # RF recovers the high-yield region


@pytest.mark.timeout(600)
def test_bo_campaign_finds_high_yield() -> None:
    """A BoFire campaign over the surrogate beats the dataset's median yield.

    **Slow, and measured to be slow rather than hung** — the distinction
    `test_bo_knowledge.py::test_campaign_publishes_recommendation_to_graph` draws, where the same
    reasoning was applied without measuring and kept `main` red over what turned out to be a real
    hang. This one fits six rounds of a real BoTorch GP over the Reizman benchmark and burns CPU
    the whole way: `pytest tests/test_reizman.py::test_bo_campaign_finds_high_yield --timeout=0`
    measured **279 s** on an idle box and 213 s on a loaded one, against the 180 s global cap. So it
    fails the gate on wall clock, and it did — this was the one red test in the full run that opened
    this branch.

    `pyproject.toml`'s comment justified that 180 s cap by naming *this* test as the slowest at
    "~37s", which was stale by 7.5x; the marker is here and the claim is corrected there, because a
    cap justified by a number nobody re-derives is the failure this repository keeps finding in its
    own prose.

    600 s rather than a tighter number: the scale factor `tests/conftest.py::timeout_scale` applies
    to markers, so a slower CI runner is handled by scaling rather than by a value chosen with no
    headroom, and the point of the cap is to name a *hang*, which this is not.
    """
    problem, objective = load_benchmark()
    median_yield = float(load_dataset()["yld"].median())

    result = asyncio.run(optimize(problem, objective, n_initial=6, n_rounds=6))

    catalysts = sorted(load_dataset()["catalyst"].unique().tolist())
    assert result.best.value > median_yield
    assert result.best.params["catalyst"] in catalysts
