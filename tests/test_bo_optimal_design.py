"""`engine.optimal_design`: the design a factorial cannot give, and the one BoFire will.

`factorial_design` enumerates corners and refuses a constrained problem outright, so a chemist with
a real limit and a fixed run budget had nowhere to go. This is that gap closed with BoFire's
`DoEStrategy`. Most of this file is about the two things the returned rows cannot say for
themselves: which model the design is optimal *for*, and whether it has enough runs to estimate it.
"""

import pytest

from chemclaw.science.bo.engine import (
    _CONSTRAINT_TOLERANCE,
    SurrogateFitError,
    _constraint_breaches,
    optimal_design,
)
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    ExcludeConstraint,
    LinearConstraint,
    Objective,
    OptimizationProblem,
)


def _problem(*, constrained: bool = False, categorical: bool = True) -> OptimizationProblem:
    """A Suzuki-shaped decision space, optionally with the limit a factorial refuses."""
    parameters: list[ContinuousParameter | CategoricalParameter] = [
        ContinuousParameter(name="temp", lower=20.0, upper=80.0),
        ContinuousParameter(name="base_equiv", lower=1.0, upper=3.0),
    ]
    if categorical:
        parameters.append(
            CategoricalParameter(name="ligand", categories=["XPhos", "SPhos", "RuPhos"])
        )
    return OptimizationProblem(
        parameters=parameters,
        objectives=[Objective(name="yield_pct", direction="maximize")],
        constraints=(
            [
                LinearConstraint(
                    parameters=["temp", "base_equiv"], coefficients=[1.0, 10.0], rhs=100.0
                )
            ]
            if constrained
            else []
        ),
    )


def test_every_run_satisfies_a_constraint_a_factorial_refuses_outright() -> None:
    """The whole reason this function exists.

    `factorial_design` raises on a constrained problem, saying so in its own message — it
    enumerates the corners of the space and most corners violate the limit. Here every run is
    feasible, which is what makes the design runnable rather than a list to hand-filter.
    """
    design = optimal_design(_problem(constrained=True), n_experiments=12, seed=5)
    assert len(design.runs) == 12
    assert all(
        float(run["temp"]) + 10.0 * float(run["base_equiv"]) <= 100.0 + _CONSTRAINT_TOLERANCE
        for run in design.runs
    )
    assert design.honoured_constraints == 1


def test_a_budget_too_small_for_its_model_is_refused_although_bofire_returns_one() -> None:
    """The refusal this wrapper exists to add.

    Measured on bofire 0.4.1: a `fully-quadratic` criterion over three continuous factors, asked
    for three runs against a ten-term model, returns three rows and no error. The information
    matrix is singular, so no coefficient of that model is estimable and nothing in the frame says
    so — a chemist runs them, fits nothing, and concludes the chemistry is noisy.
    """
    with pytest.raises(ValueError, match="singular"):
        optimal_design(_problem(), n_experiments=4, formula="fully-quadratic")


def test_the_refusal_names_the_term_count_and_a_simpler_formula() -> None:
    """A refusal that does not say what would work is one the caller cannot act on."""
    with pytest.raises(ValueError) as caught:
        optimal_design(_problem(categorical=False), n_experiments=3, formula="fully-quadratic")
    message = str(caught.value)
    assert "6 term(s)" in message
    assert "'linear' has 3 term(s)" in message
    assert "residual degrees of freedom" in message


def test_a_design_at_exactly_its_term_count_is_allowed_and_says_what_it_costs() -> None:
    """The bound is estimability, not a recommendation, and the two must not be conflated.

    A saturated design fits its model perfectly and can say nothing about how well — which is worth
    telling the caller and is not grounds for refusing a design they may have good reason to want.
    """
    design = optimal_design(_problem(categorical=False), n_experiments=3, formula="linear", seed=1)
    assert len(design.runs) == 3
    assert design.n_terms == 3


def test_space_filling_assumes_no_model_and_says_so() -> None:
    """The other question: not "estimate this model" but "what does this space look like"."""
    design = optimal_design(_problem(constrained=True), n_experiments=8, criterion="space-filling")
    assert design.formula is None
    assert design.n_terms == 0
    assert "assumes no model" in design.summary
    assert all(
        float(run["temp"]) + 10.0 * float(run["base_equiv"]) <= 100.0 + _CONSTRAINT_TOLERANCE
        for run in design.runs
    )


def test_space_filling_is_not_refused_for_a_small_budget() -> None:
    """There is no model to be singular against, so the estimability bound must not apply."""
    assert len(optimal_design(_problem(), n_experiments=2, criterion="space-filling").runs) == 2


def test_the_summary_states_the_formula_the_design_is_optimal_for() -> None:
    """Calling a design optimal, without the model it is optimal for, states nothing about it.

    The same factors and budget give a different design for a linear model than a quadratic one,
    and a linear design is blind to curvature — which is usually what a process chemist is looking
    for. `summary` is a `computed_field` so this reaches the model composing the answer.
    """
    design = optimal_design(_problem(), n_experiments=12, formula="linear-and-interactions", seed=2)
    assert "linear-and-interactions" in design.summary
    assert "blind to any effect the formula omits" in design.summary


def test_a_repeated_row_is_counted_and_named_rather_than_looking_like_a_fault() -> None:
    """Replication at an informative corner is what minimises the criterion.

    Two identical rows are the design working. A chemist who reads them as a duplicate and deletes
    one has changed the design, so the count is carried and the summary says to run them as
    written. Driven over a budget generous enough that the optimizer reuses points.
    """
    design = optimal_design(_problem(categorical=False), n_experiments=10, formula="linear", seed=3)
    keyed = [tuple(sorted(run.items())) for run in design.runs]
    assert design.duplicate_runs == len(keyed) - len(set(keyed))
    if design.duplicate_runs:
        assert "run them as written" in design.summary


def test_a_mixed_categorical_and_continuous_space_is_handled() -> None:
    """A ligand is not a number, and the design still has to place it."""
    design = optimal_design(_problem(), n_experiments=12, seed=7)
    assert {str(run["ligand"]) for run in design.runs} <= {"XPhos", "SPhos", "RuPhos"}
    assert all(20.0 <= float(run["temp"]) <= 80.0 for run in design.runs)


def test_the_term_count_follows_bofire_over_a_categorical_rather_than_our_arithmetic() -> None:
    """A categorical contributes one column per level minus one.

    Which is where a re-derivation would diverge from the truth on the case a chemist brings.
    Two continuous factors plus a three-level categorical is 1 + 2 + 2 = 5 linear terms, not 6.
    """
    assert optimal_design(_problem(), n_experiments=8, formula="linear", seed=1).n_terms == 5


def test_an_unknown_criterion_names_what_this_deployment_offers() -> None:
    with pytest.raises(ValueError, match="d-optimal"):
        optimal_design(_problem(), n_experiments=8, criterion="g-optimal")


def test_an_unknown_formula_is_refused_before_the_solver_sees_it() -> None:
    with pytest.raises(ValueError, match="unknown formula"):
        optimal_design(_problem(), n_experiments=8, formula="cubic")


def test_a_budget_over_the_deployments_ceiling_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ceiling `factorial_design` honours, named so an operator can move it."""
    monkeypatch.setattr("chemclaw.core.config.settings.bo_max_design_runs", 10)
    with pytest.raises(ValueError, match="bo_max_design_runs"):
        optimal_design(_problem(), n_experiments=11)


def test_a_non_positive_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="1 or more"):
        optimal_design(_problem(), n_experiments=0)


def test_one_seed_gives_one_design() -> None:
    """A design a chemist cannot reproduce is one they cannot defend in a report."""
    first = optimal_design(_problem(), n_experiments=10, seed=11)
    second = optimal_design(_problem(), n_experiments=10, seed=11)
    assert first.runs == second.runs


def test_an_unsolvable_space_is_translated_rather_than_raised_as_bofires_own_error() -> None:
    """Translated, for the reason the rest of this module translates.

    A numerical failure from somebody else's optimizer must reach the caller as an actionable
    sentence rather than as a class name from a library they did not choose.
    """
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="a", lower=0.0, upper=1.0),
            ContinuousParameter(name="b", lower=0.0, upper=1.0),
        ],
        objectives=[Objective(name="y", direction="maximize")],
        # Infeasible: a + b <= -1 with both in [0, 1].
        constraints=[LinearConstraint(parameters=["a", "b"], coefficients=[1.0, 1.0], rhs=-1.0)],
    )
    with pytest.raises((SurrogateFitError, ValueError)):
        optimal_design(problem, n_experiments=6)


def test_the_tolerance_is_the_one_the_engine_enforces_not_a_tighter_one() -> None:
    """The bug this pair of tests had, and the reason the constant is shared rather than repeated.

    The first version asserted 1e-6 against a solver that only ever promised its own tolerance. It
    passed locally and failed on CI, whose different scipy build landed on the other side — a flaky
    assertion, not a flaky solver. Measured over 20 seeds x 4 criteria, the worst excursion was
    7.5e-06, so the engine refuses at 1e-4 and these tests assert the same number by importing it.
    """
    assert _CONSTRAINT_TOLERANCE == pytest.approx(1e-4)
    design = optimal_design(_problem(constrained=True), n_experiments=8, seed=5)
    assert _constraint_breaches(_problem(constrained=True), design.runs) == []


def test_a_run_outside_a_constraint_is_reported_rather_than_returned() -> None:
    """The check is not vacuous: a genuinely infeasible run is named, with the constraint.

    Driven with a hand-built run rather than by hoping the solver misbehaves, because the whole
    point of the tolerance above is that it does not — and a check that only ever sees feasible
    input is one nothing proves.
    """
    breaches = _constraint_breaches(
        _problem(constrained=True), [{"temp": 80.0, "base_equiv": 3.0, "ligand": "XPhos"}]
    )
    assert len(breaches) == 1
    assert "110" in breaches[0]


def test_a_breach_inside_the_tolerance_is_not_reported() -> None:
    """Otherwise the engine would refuse every design the solver actually returns."""
    assert (
        _constraint_breaches(
            _problem(constrained=True),
            [{"temp": 70.00000746, "base_equiv": 3.0, "ligand": "XPhos"}],
        )
        == []
    )


def test_an_exclusion_is_refused_by_name_rather_than_blamed_on_the_budget() -> None:
    """BoFire's DoE solver cannot take a categorical exclusion, whatever the run count.

    Measured before this: `cat x solv` gave 8 runs unconstrained, and adding "no a in x" raised
    `SurrogateFitError` telling the chemist to "try more runs" — a remedy no budget satisfies.
    """
    problem = OptimizationProblem(
        parameters=[
            CategoricalParameter(name="cat", categories=["a", "b", "c"]),
            CategoricalParameter(name="solv", categories=["x", "y"]),
        ],
        objectives=[Objective(name="yield_pct", direction="maximize")],
        constraints=[ExcludeConstraint(parameters=["cat", "solv"], options=[["a"], ["x"]])],
    )
    with pytest.raises(ValueError, match="linear constraints only") as refused:
        optimal_design(problem, n_experiments=8, seed=0)
    assert "strike the excluded pairings" in str(refused.value)


def test_replicated_corners_are_counted_and_read_as_the_bound_they_are() -> None:
    """Solver noise is snapped onto the bound it meant, so a replicate compares equal.

    Measured before this on seed 0: `(3, 0)` came back three times as `2.999999999999995`,
    `1.17e-15` and friends, and the design reported `duplicate_runs=0`.
    """
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="a", lower=0.0, upper=3.0),
            ContinuousParameter(name="b", lower=0.0, upper=3.0),
        ],
        objectives=[Objective(name="yield_pct", direction="maximize")],
        constraints=[LinearConstraint(parameters=["a", "b"], coefficients=[1.0, 1.0], rhs=3.0)],
    )
    design = optimal_design(problem, n_experiments=8, seed=0)

    assert design.duplicate_runs > 0
    values = {float(run[name]) for run in design.runs for name in ("a", "b")}
    assert values <= {0.0, 3.0}, f"solver noise reached the chemist: {sorted(values)}"


def test_rounding_a_feasible_solve_at_a_large_magnitude_is_not_read_as_a_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The breach check reads the solver's values; only the returned runs are cleaned.

    Ten significant digits move a value by up to 5e-11 of itself, which at ~1e6 and times a
    coefficient is past the absolute `_CONSTRAINT_TOLERANCE`. Checked after rounding, a solve
    sitting exactly on its limit was refused as infeasible — measured on a space-filling design
    over `7·a + 13·b <= 3.1e7`. The solver is replaced so the point is fixed rather than
    depending on one scipy build's arithmetic.
    """
    from types import SimpleNamespace

    import pandas as pd

    from chemclaw.science.bo import engine

    on_the_limit = 1234567.89151  # rounds *up* to 1234567.892 at ten significant digits

    class _Solver:
        def ask(self, candidate_count: int) -> pd.DataFrame:
            return pd.DataFrame(
                {"a": [on_the_limit] * candidate_count, "b": [0.0] * candidate_count}
            )

    monkeypatch.setattr(engine, "strategies", SimpleNamespace(map=lambda spec: _Solver()))
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="a", lower=0.0, upper=3e6),
            ContinuousParameter(name="b", lower=0.0, upper=3e6),
        ],
        objectives=[Objective(name="yield_pct", direction="maximize")],
        constraints=[
            LinearConstraint(
                parameters=["a", "b"], coefficients=[13.0, 1.0], rhs=13.0 * on_the_limit
            )
        ],
    )

    design = optimal_design(problem, n_experiments=2, criterion="space-filling", seed=0)

    assert [run["a"] for run in design.runs] == [1234567.892, 1234567.892]
    assert design.duplicate_runs == 1
