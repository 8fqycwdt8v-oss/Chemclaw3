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
    LinearConstraint,
    Objective,
    OptimalDesign,
    OptimizationProblem,
    ScreeningDesign,
)


def _screening_problem() -> OptimizationProblem:
    """Two categorical factors: solvent (2 levels) x base (3 levels) = 6 combinations."""
    return OptimizationProblem(
        parameters=[
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
            CategoricalParameter(name="base", categories=["K2CO3", "Cs2CO3", "Et3N"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def test_factorial_design_enumerates_every_combination() -> None:
    """The design has exactly the Cartesian product of the categorical levels, each run distinct."""
    design = factorial_design(_screening_problem())
    assert len(design.runs) == 6
    seen = {(run["solvent"], run["base"]) for run in design.runs}
    assert len(seen) == 6
    assert all(run["solvent"] in {"THF", "toluene"} for run in design.runs)
    assert all(run["base"] in {"K2CO3", "Cs2CO3", "Et3N"} for run in design.runs)


def test_a_continuous_factor_is_screened_at_its_two_bounds_and_said_to_be() -> None:
    """A continuous factor is screened at its two bounds, and the design says so.

    BoFire fractionates a continuous factor to its bounds; the result's field and summary disclose
    it. A design that collapsed a range without saying so is the defect this guards against.
    """
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=100.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    design = factorial_design(problem)
    assert {run["temperature"] for run in design.runs} == {20.0, 100.0}
    assert {run["solvent"] for run in design.runs} == {"THF", "toluene"}
    assert len(design.runs) == 4
    assert design.two_level_continuous == ["temperature"]
    assert "temperature" in design.summary
    assert "held at the two ends of the declared range" in design.summary


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
        objectives=[Objective(name="yield", direction="maximize")],
    )


def test_a_generator_actually_halves_the_design() -> None:
    """The capability itself: 128 runs do not fit 96 wells, 64 do.

    Asserted on the run count, because BoFire's `FractionalFactorialStrategy` crosses categorical
    factors in full, so forwarding `n_generators` alone would change nothing.
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

    2^(7-1) is resolution VII, 2^(7-3) IV and 2^(7-4) III. A design claimed IV but really III
    confounds main effects with two-factor interactions.
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
        objectives=[Objective(name="yield", direction="maximize")],
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
        objectives=[Objective(name="yield", direction="maximize")],
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


# --- the knobs that make a screen worth analysing (W2) ---------------------------------------


def _mixed_problem() -> OptimizationProblem:
    """Two continuous factors beside one two-level categorical — the shape M-5 measured."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="T", lower=20.0, upper=120.0),
            ContinuousParameter(name="equiv", lower=1.0, upper=3.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def test_centre_runs_sit_at_the_midpoint_of_every_continuous_factor() -> None:
    """What centre points are *for*: the only rows a two-level screen has that can see curvature."""
    design = factorial_design(_mixed_problem(), n_center=2)
    midpoints = [
        run for run in design.runs if run["T"] == 70.0 and run["equiv"] == pytest.approx(2.0)
    ]
    assert midpoints, "no centre run at the midpoint of both continuous factors"
    assert design.n_center == 2


def test_centre_runs_are_added_per_categorical_combination_not_once() -> None:
    """Centre runs are added per categorical combination, not once.

    The total is `4·2^k + n_center·2^k` over k categorical factors: 12 runs here, not 10.
    """
    corners = factorial_design(_mixed_problem(), n_center=0)
    with_centres = factorial_design(_mixed_problem(), n_center=2)
    assert len(corners.runs) == 8
    assert len(with_centres.runs) == 12


def test_the_default_is_no_centre_runs_although_bofire_defaults_to_one() -> None:
    """BoFire's own `n_center` default is 1; leaving it unset would emit midpoints unasked.

    Every construction site sets it explicitly.
    """
    design = factorial_design(_mixed_problem())
    assert design.n_center == 0
    assert len(design.runs) == 8
    assert {run["T"] for run in design.runs} == {20.0, 120.0}
    assert "centre run" not in design.summary


def test_replication_doubles_the_factorial_part_and_says_why() -> None:
    """Without replication no effect a screen reports has a significance to quote."""
    design = factorial_design(_mixed_problem(), n_repetitions=2)
    assert len(design.runs) == 16
    assert design.n_repetitions == 2


def test_centre_runs_are_refused_on_an_all_categorical_problem() -> None:
    """Measured inert there (M-5), so it is refused rather than threaded into a no-op.

    This is the lesson `n_generators` taught, applied before the same mistake could be made twice:
    an argument that BoFire ignores must not be accepted as though it did something.
    """
    with pytest.raises(ValueError, match="n_center needs at least one continuous factor"):
        factorial_design(_screening_problem(), n_center=2)


def test_replication_is_refused_on_an_all_categorical_problem() -> None:
    """Same measurement, same refusal, and the message says to repeat the runs by hand instead."""
    with pytest.raises(ValueError, match="n_repetitions needs at least one continuous factor"):
        factorial_design(_screening_problem(), n_repetitions=2)


def test_centre_runs_are_refused_on_a_reduced_design_that_still_has_categoricals() -> None:
    """A re-encoded categorical has no midpoint: 0.5 decodes to neither of its two levels."""
    with pytest.raises(ValueError, match="halfway between them"):
        factorial_design(_mixed_problem(), n_generators=1, n_center=1)


def test_a_reduced_design_fractionates_the_continuous_and_categorical_halves_together() -> None:
    """A reduced design fractionates the continuous and categorical halves together.

    Otherwise the stated resolution, derived from all factors, would describe a design that left
    part of them unfractionated.
    """
    full = factorial_design(_mixed_problem())
    reduced = factorial_design(_mixed_problem(), n_generators=1)
    assert len(full.runs) == 8
    assert len(reduced.runs) == 4
    assert reduced.resolution is not None
    # The categorical is decoded back to real labels, not left as the 0/1 it was fractionated as.
    assert {run["solvent"] for run in reduced.runs} == {"THF", "toluene"}
    assert {run["T"] for run in reduced.runs} == {20.0, 120.0}


def test_a_randomized_order_is_reproducible_under_a_seed_and_varies_across_seeds() -> None:
    """A design that differs run to run is not a design anyone can hand to two chemists."""
    first = factorial_design(_screening_problem(), randomize=True, seed=1)
    again = factorial_design(_screening_problem(), randomize=True, seed=1)
    other = factorial_design(_screening_problem(), randomize=True, seed=2)
    assert first.runs == again.runs
    assert first.runs != other.runs
    assert first.randomized is True
    assert sorted(map(str, first.runs)) == sorted(map(str, other.runs)), "shuffle changed the set"
    assert "Run order is randomized" in first.summary


def test_randomization_works_on_an_all_categorical_screen() -> None:
    """The one knob of the four that is *not* inert on an all-categorical domain (M-5)."""
    plain = factorial_design(_screening_problem())
    shuffled = factorial_design(_screening_problem(), randomize=True, seed=3)
    assert plain.randomized is False
    assert len(shuffled.runs) == len(plain.runs)
    assert shuffled.runs != plain.runs


def test_the_tool_reaches_every_knob() -> None:
    """A capability expressible in `science/` and unreachable from a tool call is not shipped."""
    design = asyncio.run(
        generate_screening_design(_mixed_problem(), n_center=1, n_repetitions=2, randomize=True)
    )
    assert design.n_center == 1
    assert design.n_repetitions == 2
    assert design.randomized is True


def _crossed_problem(n_categorical: int, n_continuous: int) -> OptimizationProblem:
    """A mixed domain: `n_categorical` two-level factors and `n_continuous` continuous ones."""
    return OptimizationProblem(
        parameters=[
            *(
                CategoricalParameter(name=f"cat{i}", categories=[f"a{i}", f"b{i}"])
                for i in range(n_categorical)
            ),
            *(
                ContinuousParameter(name=f"cont{i}", lower=0.0, upper=100.0)
                for i in range(n_continuous)
            ),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


@pytest.mark.parametrize(
    ("n_categorical", "n_continuous"), [(1, 1), (2, 1), (1, 2), (2, 2), (3, 1)]
)
def test_a_mixed_full_factorial_is_the_whole_cross_product(
    n_categorical: int, n_continuous: int
) -> None:
    """A mixed full factorial is the whole cross product.

    BoFire's combining step tiles both frames, which is a cross product only when `gcd(N, C) == 1`,
    so a mixed design lost rows, duplicated others and aliased factors. The categorical-only test
    above cannot see this.
    """
    problem = _crossed_problem(n_categorical, n_continuous)
    expected = 2 ** (n_categorical + n_continuous)

    runs = factorial_design(problem).runs
    distinct = {tuple(sorted(run.items())) for run in runs}

    assert len(distinct) == expected, (
        f"{n_categorical} categorical x {n_continuous} continuous: {len(distinct)} distinct "
        f"combinations of {expected}. A design missing combinations is aliased, and the summary "
        f"calls it exhaustive."
    )
    assert len(runs) == expected, "no combination should be duplicated in an unreplicated design"


def test_a_mixed_factorial_crosses_categories_against_both_bounds() -> None:
    """The property behind the counting test: every category meets every bound."""
    problem = _crossed_problem(1, 1)

    pairs = {(run["cat0"], run["cont0"]) for run in factorial_design(problem).runs}

    assert pairs == {("a0", 0.0), ("a0", 100.0), ("b0", 0.0), ("b0", 100.0)}


def test_a_screen_beyond_the_run_ceiling_is_refused_before_it_is_built() -> None:
    """A screen beyond the run ceiling is refused before it is built.

    A full factorial's size is the product of model-written level counts, so twenty two-level
    factors would materialize a million rows. The refusal must use the count, before the list
    exists.
    """
    problem = OptimizationProblem(
        parameters=[
            CategoricalParameter(name=f"f{i}", categories=[f"lo{i}", f"hi{i}"]) for i in range(20)
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    with pytest.raises(ValueError, match="beyond the configured ceiling"):
        factorial_design(problem)


def test_a_screen_inside_the_ceiling_still_builds() -> None:
    """The bound must not refuse a design anybody would actually run.

    Ten two-level factors is 1 024 runs — already far past a plate and well inside the ceiling, so
    the guard's job is to be invisible here.
    """
    problem = OptimizationProblem(
        parameters=[
            CategoricalParameter(name=f"f{i}", categories=[f"lo{i}", f"hi{i}"]) for i in range(10)
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    assert len(factorial_design(problem).runs) == 1024


def test_a_reduced_design_is_measured_after_the_reduction_not_before() -> None:
    """The ceiling counts the design that would actually be built, in both directions.

    Refusing on the unreduced count would refuse designs that fit; stopping the multiplication early
    and then shifting by `n_generators` would admit designs far over the ceiling. Both are asserted.
    """
    problem = OptimizationProblem(
        parameters=[
            CategoricalParameter(name=f"f{i}", categories=[f"lo{i}", f"hi{i}"]) for i in range(40)
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    # 2^40 >> 1 is still half a trillion rows.
    with pytest.raises(ValueError, match="beyond the configured ceiling"):
        factorial_design(problem, n_generators=1)


def test_a_reduced_design_over_a_three_level_factor_reports_the_real_error() -> None:
    """The size guard must not answer ahead of the error that actually applies.

    A reduced design over a non-two-level factor cannot be built at all, so the two-level refusal
    must win over a fictional run count.
    """
    problem = OptimizationProblem(
        parameters=[
            CategoricalParameter(name=f"f{i}", categories=list("abcdefghij")) for i in range(5)
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    with pytest.raises(ValueError, match="two-level design"):
        factorial_design(problem, n_generators=3)


# --- the criterion argument: the design a factorial cannot give ----------------------------------


def test_a_criterion_other_than_factorial_honours_a_constraint_the_grid_refuses() -> None:
    """A non-factorial criterion honours a constraint the factorial grid refuses.

    One problem schema, two design families: folded into this tool rather than a separate one, which
    would have duplicated the `OptimizationProblem` schema in the prompt.
    """
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temp", lower=20.0, upper=80.0),
            ContinuousParameter(name="base_equiv", lower=1.0, upper=3.0),
        ],
        objectives=[Objective(name="yield_pct", direction="maximize")],
        constraints=[
            LinearConstraint(parameters=["temp", "base_equiv"], coefficients=[1.0, 10.0], rhs=100.0)
        ],
    )
    design = asyncio.run(
        generate_screening_design(problem, criterion="d-optimal", n_experiments=8, formula="linear")
    )
    assert isinstance(design, OptimalDesign)
    assert len(design.runs) == 8
    assert all(
        float(run["temp"]) + 10.0 * float(run["base_equiv"]) <= 100.0 + 1e-6 for run in design.runs
    )


def test_the_default_criterion_still_returns_a_factorial_screen() -> None:
    """The fold must not move the behaviour anybody already depends on."""
    problem = OptimizationProblem(
        parameters=[CategoricalParameter(name="ligand", categories=["XPhos", "SPhos"])],
        objectives=[Objective(name="yield_pct", direction="maximize")],
    )
    design = asyncio.run(generate_screening_design(problem))
    assert isinstance(design, ScreeningDesign)
    assert len(design.runs) == 2


def test_a_budget_is_refused_by_the_factorial_rather_than_ignored() -> None:
    """A budget is refused by the factorial rather than ignored.

    A factorial's size is fixed by its level counts; returning 128 rows after a request for 24 would
    be silent.
    """
    problem = OptimizationProblem(
        parameters=[CategoricalParameter(name="ligand", categories=["XPhos", "SPhos"])],
        objectives=[Objective(name="yield_pct", direction="maximize")],
    )
    with pytest.raises(ValueError, match="cannot be honoured here"):
        asyncio.run(generate_screening_design(problem, n_experiments=24))
