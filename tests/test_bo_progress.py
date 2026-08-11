"""Tests for the plateau reading and the sd reference scale (W1).

The arithmetic is pure Python over the observations supplied — no BoFire, no Temporal — so these
run in-process like `test_bo_tools.py`. The load-bearing case is the `op-13` replay: a live probe
graded *fabricated* for asserting "the last 1-2% gains are real" against a +/-2% reproducibility
the chemist had stated in the same question. The numbers below are that probe's, verbatim from
`data/evals/probes/optimization.yaml`.
"""

import asyncio

import pytest

from chemclaw.connectors.bo.server.tools import (
    ExperimentSuggestion,
    ObjectiveScale,
    campaign_progress,
)
from chemclaw.core.config import settings
from chemclaw.science.bo.problem import (
    Candidate,
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)
from chemclaw.science.bo.progress import campaign_progress as read_progress

# op-13's twelve runs, ordered by equivalents — the axis the chemist walked. The probe's own
# `direction` field says "the last four results span 87-89% against a stated +/-2% assay
# reproducibility so they are indistinguishable from each other", which is the claim this replay
# has to make computable.
OP13_RUNS = [
    (1.1, 0.0, 54.0),
    (1.1, 25.0, 62.0),
    (1.3, 25.0, 71.0),
    (1.5, 25.0, 74.0),
    (1.5, 0.0, 66.0),
    (1.8, 25.0, 79.0),
    (1.8, 40.0, 72.0),
    (2.0, 25.0, 83.0),
    (2.2, 25.0, 88.0),
    (2.2, 15.0, 87.0),
    (2.3, 10.0, 88.0),
    (2.4, 15.0, 89.0),
]


def _amide_problem() -> OptimizationProblem:
    """op-13's decision space: EDC equivalents against temperature, maximizing yield."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="equiv", lower=1.0, upper=3.0),
            ContinuousParameter(name="temperature", lower=0.0, upper=40.0),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def _op13_observations() -> list[Observation]:
    """The probe's runs as observations, in the order stated above."""
    return [
        Observation(params={"equiv": equiv, "temperature": temperature}, value=value)
        for equiv, temperature, value in OP13_RUNS
    ]


def _series(values: list[float]) -> list[Observation]:
    """A one-parameter series, one observation per value, in order."""
    return [
        Observation(params={"equiv": 1.0 + 0.1 * index}, value=value)
        for index, value in enumerate(values)
    ]


def _series_problem() -> OptimizationProblem:
    """A single continuous factor, maximizing — the shape `_series` builds points in."""
    return OptimizationProblem(
        parameters=[ContinuousParameter(name="equiv", lower=1.0, upper=3.0)],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def test_op13_last_four_results_are_not_distinguishable_from_each_other() -> None:
    """The exact claim the probe's grader made, now computed instead of asserted.

    The model under test said "the last 1-2% gains are real". Against the +/-2% the chemist stated,
    the last four runs (88, 87, 88, 89) span exactly 2.0 — one noise width — so they say nothing
    about each other, and the reading has to state that.
    """
    progress = read_progress(_amide_problem(), _op13_observations(), assay_noise=2.0, window=4)
    assert progress.window_span == pytest.approx(2.0)
    assert progress.window_indistinguishable is True


def test_op13_names_how_long_since_a_gain_that_beat_the_noise() -> None:
    """Three evaluations, and that number is the honest answer to "have we plateaued".

    Worth pinning as its own case because it refutes the shape this test was first planned in. The
    campaign is **not** plateaued on the default five-evaluation window: the jump from 83 to 88 at
    2.2 equivalents is a real 5-point gain and it happened only four runs from the end. What is
    true is narrower — nothing since has beaten the noise — and a tool that rounded that up to
    "plateaued" would be making the opposite of op-13's error with the same overconfidence.
    """
    progress = read_progress(_amide_problem(), _op13_observations(), assay_noise=2.0)
    assert progress.evaluations_since_improvement == 3
    assert progress.best_value == pytest.approx(89.0)
    assert progress.plateaued is False


def test_a_series_that_stopped_moving_is_plateaued() -> None:
    """Six flat evaluations after a real gain: nothing beats +/-2, so the verdict is given."""
    progress = read_progress(
        _series_problem(),
        _series([50.0, 60.0, 70.0, 70.5, 71.0, 70.8, 71.2, 70.9]),
        assay_noise=2.0,
        window=5,
    )
    assert progress.evaluations_since_improvement == 5
    assert progress.plateaued is True


def test_a_series_still_climbing_is_not_plateaued() -> None:
    """Five-point steps against +/-2 noise are real gains, so the campaign is still moving."""
    progress = read_progress(
        _series_problem(),
        _series([50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0]),
        assay_noise=2.0,
        window=5,
    )
    assert progress.evaluations_since_improvement == 0
    assert progress.plateaued is False


def test_a_series_creeping_past_the_noise_in_small_steps_is_not_plateaued() -> None:
    """A climb of 1-point steps against +/-2 noise is still a climb once it accumulates.

    **This test replaced its own opposite** (D-2026-08-05). The first version asserted `plateaued`
    here, arguing that what makes a gain real is the assay rather than the slope. That is true of a
    single step and false of a series: a chemist comparing run 1 with run 7 measures +6 on a +/-2
    assay, which is a real, repeatable gain and the whole reason to keep going. Calling it a plateau
    tells a lab leader to stop a campaign that is working.

    The counter is anchored at the last real gain, so the third run (+3 over the anchor at 50)
    resets it and the series never accumulates five flat evaluations.
    """
    progress = read_progress(
        _series_problem(),
        _series([50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0]),
        assay_noise=2.0,
        window=5,
    )
    assert progress.plateaued is False
    assert progress.best_value == 56.0


def test_a_long_creep_ten_times_the_noise_is_not_plateaued() -> None:
    """The case that found the defect: +20.9 against +/-2, once reported as a plateau.

    Twelve runs climbing 1.9 each. Every individual step is inside the assay, and the campaign has
    nonetheless gained ten times the noise — which is what a chemist measures when they compare the
    first run with the last. The old semantics returned `evaluations_since_improvement=11` here.
    """
    progress = read_progress(
        _series_problem(),
        _series([50.0 + 1.9 * step for step in range(12)]),
        assay_noise=2.0,
        window=5,
    )
    assert progress.plateaued is False
    # Exactly 1: the anchored counter resets on every second run of a 1.9-step climb against
    # +/-2 noise. `< 5` had five times the slack of the fix it guards.
    assert progress.evaluations_since_improvement == 1
    assert progress.best_value == pytest.approx(70.9)


def test_below_the_observation_floor_it_refuses_instead_of_verdicting() -> None:
    """Too few runs is a refusal, and the refusal says it is not the same as "still improving"."""
    progress = read_progress(_series_problem(), _series([50.0, 60.0, 70.0]), assay_noise=2.0)
    assert progress.enough_observations is False
    assert progress.plateaued is False
    assert "too few to read a trend from" in progress.summary


def test_the_summary_never_claims_a_global_optimum() -> None:
    """The one sentence a plateau verdict may never be rounded up past."""
    progress = read_progress(
        _series_problem(),
        _series([50.0, 60.0, 70.0, 70.5, 71.0, 70.8, 71.2, 70.9]),
        assay_noise=2.0,
    )
    assert "cannot show that a global optimum has been reached" in progress.summary
    assert "untried corner" in progress.summary


def test_a_minimize_campaign_reads_gains_in_its_own_direction() -> None:
    """Falling values are improvements when the objective is minimized."""
    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name="equiv", lower=1.0, upper=3.0)],
        objectives=[Objective(name="impurity", direction="minimize")],
    )
    progress = read_progress(
        problem, _series([9.0, 7.0, 5.0, 3.0, 1.5, 0.5]), assay_noise=0.5, window=3
    )
    assert progress.best_value == pytest.approx(0.5)
    assert progress.evaluations_since_improvement == 0
    assert progress.best_so_far == [9.0, 7.0, 5.0, 3.0, 1.5, 0.5]


def test_the_design_space_is_sized_for_a_finite_space_and_omitted_for_an_infinite_one() -> None:
    """3.6's defensible half: proposals spent against the grid the screen would have needed."""
    categorical = OptimizationProblem(
        parameters=[
            CategoricalParameter(name="solvent", categories=["THF", "toluene", "dioxane"]),
            CategoricalParameter(name="base", categories=["K2CO3", "Cs2CO3"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    observations = [
        Observation(params={"solvent": solvent, "base": base}, value=value)
        for solvent, base, value in [
            ("THF", "K2CO3", 40.0),
            ("THF", "Cs2CO3", 55.0),
            ("toluene", "K2CO3", 48.0),
            ("toluene", "Cs2CO3", 61.0),
            ("dioxane", "K2CO3", 44.0),
            ("dioxane", "K2CO3", 45.0),
        ]
    ]
    progress = read_progress(categorical, observations, assay_noise=2.0)
    assert progress.design_space == 6
    assert progress.n_distinct == 5

    infinite = read_progress(_series_problem(), _series([1.0] * 6), assay_noise=0.5)
    assert infinite.design_space is None
    assert "out of the" not in infinite.summary


def test_a_repeated_condition_counts_once_against_the_grid() -> None:
    """`n_distinct` is what an efficiency claim divides by; replicates are not extra coverage."""
    progress = read_progress(
        _series_problem(),
        [Observation(params={"equiv": 1.5}, value=value) for value in (60.0, 61.0, 59.0, 60.5)]
        + _series([62.0, 63.0]),
        assay_noise=2.0,
    )
    assert progress.n_observations == 6
    # Exactly 3 — equiv=1.5 four times plus the series' 1.0 and 1.1. `< n_observations` passed for
    # anything from 1 to 5, so it could not catch a count that collapsed or barely deduplicated.
    assert progress.n_distinct == 3


def test_assay_noise_must_be_positive() -> None:
    """No noise, no verdict — the argument exists precisely so it cannot be skipped."""
    with pytest.raises(ValueError, match="assay_noise must be positive"):
        read_progress(_series_problem(), _series([1.0] * 6), assay_noise=0.0)


def test_the_tool_accepts_plain_dicts_as_maf_delivers_them() -> None:
    """This function is handed JSON, never model instances — the bridge the sibling tool has."""
    problem = {
        "parameters": [
            {"kind": "continuous", "name": "equiv", "lower": 1.0, "upper": 3.0},
            {"kind": "continuous", "name": "temperature", "lower": 0.0, "upper": 40.0},
        ],
        "objective": {"name": "yield", "direction": "maximize"},
    }
    observations = [
        {"params": {"equiv": equiv, "temperature": temperature}, "value": value}
        for equiv, temperature, value in OP13_RUNS
    ]
    progress = asyncio.run(campaign_progress(problem, observations, 2.0, 4))
    assert progress.window_indistinguishable is True


def test_the_tool_accepts_observations_json_encoded_as_a_string() -> None:
    """The same live-observed shape `suggest_next_experiment` already tolerates."""
    import json

    observations = json.dumps(
        [
            {"params": {"equiv": equiv, "temperature": temperature}, "value": value}
            for equiv, temperature, value in OP13_RUNS
        ]
    )
    progress = asyncio.run(campaign_progress(_amide_problem(), observations, 2.0, 4))
    assert progress.n_observations == 12


def test_the_tool_refuses_an_observation_that_does_not_match_the_declared_space() -> None:
    """The suggestion tool's boundary check, for its reason: a caller-fixable message."""
    observations = [Observation(params={"equiv": 1.5}, value=60.0)] * 6
    with pytest.raises(ValueError, match="temperature"):
        asyncio.run(campaign_progress(_amide_problem(), observations, 2.0))


def test_the_tool_the_model_sees_demands_the_assay_noise() -> None:
    """The description the model receives must say where the noise comes from and to ask for it.

    Asserted against the served MCP description rather than the Python docstring, because that is
    what actually travels to the model — the same reason the sibling assertion in
    `test_bo_tools.py` is written that way.
    """
    from chemclaw.connectors.bo.server.tools import server

    tools = {tool.name: (tool.description or "") for tool in asyncio.run(server.list_tools())}
    description = tools["campaign_progress"]
    assert "required and you must get it from the chemist" in description
    assert "ask before calling" in description
    assert "global optimum" in description


def test_the_default_window_and_floor_come_from_config() -> None:
    """Both knobs are settings, not magic numbers, and the default reading uses them."""
    progress = read_progress(_series_problem(), _series([50.0] * 8), assay_noise=1.0)
    assert progress.window == settings.bo_plateau_window
    assert settings.bo_plateau_min_observations == 6


def _scale(values: list[float]) -> ObjectiveScale:
    """An `ObjectiveScale` over the given observed values."""
    return ObjectiveScale(
        name="yield",
        direction="maximize",
        n=len(values),
        observed_min=min(values) if values else None,
        observed_max=max(values) if values else None,
        observed_sd=None,
    )


def test_a_small_sd_against_a_wide_spread_reads_as_an_exploit() -> None:
    """±3 on a 40-point spread is refinement, and the summary has to say which it is."""
    suggestion = ExperimentSuggestion(
        campaign_id="campaign-test",
        candidates=[Candidate(params={"equiv": 2.0}, predicted_value=90.0, predicted_sd=3.0)],
        scale=_scale([50.0, 90.0]),
    )
    assert "exploit of a region the model has learned" in suggestion.summary


def test_the_same_sd_against_a_narrow_spread_reads_as_an_excursion() -> None:
    """±3 on a 4-point spread is a leap. Only the spread changed between this and the case above."""
    suggestion = ExperimentSuggestion(
        campaign_id="campaign-test",
        candidates=[Candidate(params={"equiv": 2.0}, predicted_value=90.0, predicted_sd=3.0)],
        scale=_scale([86.0, 90.0]),
    )
    assert "excursion into chemistry the model has not seen" in suggestion.summary


def test_a_seed_point_is_named_as_one_rather_than_read_as_confident() -> None:
    """A missing sd is a claim — no surrogate had an opinion — not a small uncertainty."""
    suggestion = ExperimentSuggestion(
        campaign_id="campaign-test",
        candidates=[Candidate(params={"equiv": 2.0})],
        scale=_scale([50.0, 90.0]),
    )
    assert "space-filling seed point" in suggestion.summary
    assert "not an endorsement" in suggestion.summary


def test_the_suggestion_carries_the_scale_of_the_runs_it_was_given() -> None:
    """End to end: the tool computes the spread from the observations the caller supplied."""
    from chemclaw.connectors.bo.server.tools import suggest_next_experiment

    observations = [
        Observation(params={"equiv": 1.2, "temperature": 20.0}, value=55.0),
        Observation(params={"equiv": 2.0, "temperature": 30.0}, value=78.0),
        Observation(params={"equiv": 2.5, "temperature": 25.0}, value=64.0),
    ]
    suggestion = asyncio.run(suggest_next_experiment(_amide_problem(), observations))
    assert suggestion.scale is not None
    assert suggestion.scale.observed_min == pytest.approx(55.0)
    assert suggestion.scale.observed_max == pytest.approx(78.0)
    assert suggestion.scale.spread == pytest.approx(23.0)
    assert suggestion.summary


def test_the_scale_survives_serialization_because_summary_is_a_computed_field() -> None:
    """A bare property would not reach the model; this is why both summaries are computed fields."""
    suggestion = ExperimentSuggestion(
        campaign_id="campaign-test",
        candidates=[Candidate(params={"equiv": 2.0}, predicted_sd=3.0)],
        scale=_scale([50.0, 90.0]),
    )
    dumped = suggestion.model_dump(mode="json")
    assert "summary" in dumped
    progress = read_progress(_series_problem(), _series([50.0] * 8), assay_noise=1.0)
    assert "summary" in progress.model_dump(mode="json")


def test_a_plateau_on_a_trade_off_must_name_which_objective() -> None:
    """A trade-off plateaus per axis: yield can stop moving while the impurity is still falling.

    Silently reading the lead objective would answer a different question from the one put, which
    is the same failure `best_of` refuses one module over.
    """
    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name="equiv", lower=1.0, upper=3.0)],
        objectives=[
            Objective(name="yield", direction="maximize"),
            Objective(name="impurity", direction="minimize"),
        ],
    )
    runs = [
        Observation(
            params={"equiv": 1.0 + 0.1 * index},
            value=50.0 + index,
            values={"yield": 50.0 + index, "impurity": 10.0 - index},
        )
        for index in range(8)
    ]
    with pytest.raises(ValueError, match="name which one"):
        read_progress(problem, runs, assay_noise=2.0)
    # Named, it reads that axis and says so.
    impurity = read_progress(problem, runs, assay_noise=0.5, objective="impurity")
    assert impurity.objective == "impurity"
    assert impurity.direction == "minimize"
    assert impurity.best_value == pytest.approx(3.0)


def test_an_unknown_objective_lists_the_ones_the_problem_declares() -> None:
    """A typo gets the names back rather than a KeyError from inside the arithmetic."""
    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name="equiv", lower=1.0, upper=3.0)],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    with pytest.raises(ValueError, match=r"unknown objective 'yeild'.*\['yield'\]"):
        read_progress(problem, _series([50.0] * 8), assay_noise=1.0, objective="yeild")


# --- replay safety: what a validator may and may not refuse (review follow-up) -------------------


def test_a_legacy_problem_whose_objective_shares_a_parameter_name_still_validates() -> None:
    """The rule that must NOT live in a validator, because stored data can violate it.

    Nothing forbade an objective sharing a parameter's name before `objectives` became a list, so a
    campaign launched earlier may carry one — and `OptimizationProblem`'s validators re-run wherever
    that data is read back: `BoCampaignWorkflow` revalidates its `CampaignSpec` on **every replay**,
    and `read_campaign_thread` revalidates the stored problem on every resume. A model-level rule
    would strand an in-flight campaign and make a stored one permanently unreadable, which is the
    hazard `require_rounds_within_ceiling` was moved out of the model to avoid.
    """
    legacy = {
        "parameters": [
            {"kind": "continuous", "name": "yield", "lower": 0.0, "upper": 100.0},
            {"kind": "continuous", "name": "temperature", "lower": 20.0, "upper": 120.0},
        ],
        "objective": {"name": "yield", "direction": "maximize"},
    }
    problem = OptimizationProblem.model_validate(legacy)
    assert problem.objective.name == "yield"


def test_a_legacy_campaign_spec_carrying_that_problem_replays() -> None:
    """The shape actually sitting in an in-flight Temporal history — spec around legacy problem."""
    from chemclaw.science.bo.problem import CampaignSpec

    spec = CampaignSpec.model_validate(
        {
            "problem": {
                "parameters": [
                    {"kind": "continuous", "name": "yield", "lower": 0.0, "upper": 100.0},
                    {"kind": "continuous", "name": "temperature", "lower": 20.0, "upper": 120.0},
                ],
                "objective": {"name": "yield", "direction": "maximize"},
            },
            "objective_name": "demo",
            "n_initial": 2,
            "n_rounds": 1,
        }
    )
    assert spec.problem.objective.name == "yield"


def test_the_clash_is_refused_where_data_enters_instead() -> None:
    """Refused at the boundary, so a new campaign cannot be created with the collision."""
    from chemclaw.science.bo.problem import require_names_do_not_clash

    problem = OptimizationProblem.model_validate(
        {
            "parameters": [{"kind": "continuous", "name": "yield", "lower": 0.0, "upper": 100.0}],
            "objective": {"name": "yield", "direction": "maximize"},
        }
    )
    with pytest.raises(ValueError, match="both a parameter and an objective"):
        require_names_do_not_clash(problem)


def test_the_tool_refuses_a_clashing_problem() -> None:
    """And the boundary is wired: the tool raises rather than fitting against its own input."""
    with pytest.raises(ValueError, match="both a parameter and an objective"):
        asyncio.run(
            campaign_progress(
                OptimizationProblem.model_validate(
                    {
                        "parameters": [
                            {"kind": "continuous", "name": "yield", "lower": 0.0, "upper": 100.0}
                        ],
                        "objective": {"name": "yield", "direction": "maximize"},
                    }
                ),
                [{"params": {"yield": 50.0}, "value": 50.0}],
                assay_noise=2.0,
            )
        )
