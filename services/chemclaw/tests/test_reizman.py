"""Behavioral tests for the Reizman Suzuki reaction benchmark (plan step 1d.3).

Proves the real dataset wires into our BO layer: the surrogate learns the data,
the problem has the expected mixed variables, and a BoFire campaign over it finds
a high-yield region (beats the dataset median) — reaction-condition optimization
end to end.
"""

import asyncio
import warnings

from bo.benchmarks.reizman_suzuki import (
    YieldSurrogate,
    build_problem,
    load_benchmark,
    load_dataset,
)
from bo.campaign import optimize
from bo.problem import CategoricalParameter

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


def test_bo_campaign_finds_high_yield() -> None:
    """A BoFire campaign over the surrogate beats the dataset's median yield."""
    problem, objective = load_benchmark()
    median_yield = float(load_dataset()["yld"].median())

    result = asyncio.run(optimize(problem, objective, n_initial=6, n_rounds=6))

    catalysts = sorted(load_dataset()["catalyst"].unique().tolist())
    assert result.best.value > median_yield
    assert result.best.params["catalyst"] in catalysts
