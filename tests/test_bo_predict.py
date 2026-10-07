"""Tests for reading the fitted surrogate back.

Two capabilities over one fit: the model's expectation at caller-named points, and how well it
predicts held-out runs (`strategy.surrogate_specs` exposes the surrogate BoFire chose, so
`cross_validate` needs no class named). Every assertion goes through `interrogate_surrogate`, the
path `connectors/bo/server/tools.py::predict_outcome` uses; `_predictions` and `_fit_quality` are
test conveniences.
"""

import asyncio
import re

import pytest

from chemclaw.connectors.bo.server.tools import (
    ExperimentSuggestion,
    ObjectiveScale,
    predict_outcome,
    suggest_next_experiment,
)
from chemclaw.core.config import settings
from chemclaw.science.bo import engine
from chemclaw.science.bo.engine import interrogate_surrogate
from chemclaw.science.bo.problem import (
    Candidate,
    CategoricalParameter,
    ContinuousParameter,
    FitQuality,
    Objective,
    Observation,
    OptimizationProblem,
    Prediction,
    pareto_front,
    point_in_domain,
)


def _problem() -> OptimizationProblem:
    """One continuous knob and one solvent choice — the shape most asks arrive in."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def _runs() -> list[Observation]:
    """Ten runs on a rising trend, enough for a five-fold cross-validation."""
    return [
        Observation(
            params={"temperature": 20.0 + 10 * index, "solvent": ["THF", "toluene"][index % 2]},
            value=30.0 + 2.5 * index + (index % 3),
        )
        for index in range(10)
    ]


def _fit_quality(
    problem: OptimizationProblem,
    observations: list[Observation],
    folds: int | None = None,
    seed: int | None = None,
) -> list[FitQuality]:
    """Cross-validate the surrogate and return the scores — the fit half of one interrogation."""
    return interrogate_surrogate(problem, observations, [], folds=folds, seed=seed)[1]


def _predictions(
    problem: OptimizationProblem,
    observations: list[Observation],
    points: list[dict[str, float | str]],
    seed: int | None = None,
) -> list[Prediction]:
    """The prediction half of one interrogation, with no fit assessed — this file's convenience.

    Production asks for both halves at once through `predict_outcome`.
    """
    return interrogate_surrogate(problem, observations, points, assess_fit=False, seed=seed)[0]


@pytest.fixture(scope="module")
def predicted_at() -> dict[str, Prediction]:
    """One fit of `_problem()` over `_runs()`, read at every point the constant-input tests name.

    The fit dominates the cost, and sharing it is also stronger: GP fits are non-deterministic, so
    comparing sds across two fits would carry that noise. Keyed by name so an assertion says which
    point it means.
    """
    points: dict[str, dict[str, float | str]] = {
        "a_run_already_done": dict(_runs()[4].params),
        "the_first_run": dict(_runs()[0].params),
        "an_unexplored_corner": {"temperature": 119.0, "solvent": "THF"},
        "mid_range": {"temperature": 60.0, "solvent": "THF"},
        "far_outside_the_range": {"temperature": 400.0, "solvent": "THF"},
    }
    predictions = _predictions(_problem(), _runs(), list(points.values()))
    return dict(zip(points, predictions, strict=True))


def test_a_point_among_the_runs_predicts_near_what_was_measured(
    predicted_at: dict[str, Prediction],
) -> None:
    """The floor: a surrogate that cannot reproduce its own training data explains nothing."""
    prediction = predicted_at["a_run_already_done"]
    assert prediction.values["yield"] == pytest.approx(_runs()[4].value, abs=3.0)
    assert prediction.sds["yield"] < 5.0
    assert prediction.in_domain


def test_an_unexplored_corner_carries_a_larger_sd_than_an_observed_point(
    predicted_at: dict[str, Prediction],
) -> None:
    """An unexplored corner carries a larger sd than an observed point.

    Whether the search has been circling one region is a question about the model's uncertainty.
    Both sds come from one fit.
    """
    corner = predicted_at["an_unexplored_corner"]
    assert corner.sds["yield"] > predicted_at["the_first_run"].sds["yield"]
    assert corner.in_domain


def test_an_out_of_range_point_is_answered_and_labelled_rather_than_refused(
    predicted_at: dict[str, Prediction],
) -> None:
    """An out-of-range point is answered and labelled rather than refused.

    BoFire extrapolates (its sd rises sharply); the answer carries `in_domain` false and a summary
    saying the mean is unconstrained there.
    """
    inside, outside = predicted_at["mid_range"], predicted_at["far_outside_the_range"]
    assert inside.in_domain
    assert not outside.in_domain
    assert outside.sds["yield"] > 5 * inside.sds["yield"]
    assert "outside" in outside.summary
    assert "extrapolating" in outside.summary


def test_a_prediction_says_it_is_not_a_recommendation(
    predicted_at: dict[str, Prediction],
) -> None:
    """The whole reason `Prediction` is not `Candidate`.

    A candidate carries an implicit endorsement; a prediction does not. The caveat is a
    `computed_field` so it is serialized.
    """
    assert "not a recommendation" in predicted_at["mid_range"].summary


def test_a_featurized_categorical_is_accepted() -> None:
    """A featurized categorical is accepted.

    `featurize_problem` turns a categorical with `structures` into a `CategoricalDescriptorInput`,
    which `predict` must handle.
    """
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(
                name="ligand",
                categories=["L1", "L2", "L3"],
                descriptors={
                    "L1": {"homo_ev": -6.1, "lumo_ev": -0.4},
                    "L2": {"homo_ev": -5.7, "lumo_ev": -0.9},
                    "L3": {"homo_ev": -6.4, "lumo_ev": -0.2},
                },
            ),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    runs = [
        Observation(
            params={"temperature": 30.0 + 12 * index, "ligand": ["L1", "L2", "L3"][index % 3]},
            value=25.0 + 3.0 * index,
        )
        for index in range(6)
    ]
    # Not `sds > 0` — a GP posterior sd is positive by construction, so that passes even if all
    # three ligands' descriptor rows had collapsed onto one point, which is the thing featurization
    # exists to prevent. Three distinct descriptor rows must give three distinct predictions.
    at_seventy = _predictions(
        problem,
        runs,
        [{"temperature": 70.0, "ligand": ligand} for ligand in ("L1", "L2", "L3")],
    )
    assert len({round(p.values["yield"], 6) for p in at_seventy}) == 3


def _two_objective() -> tuple[OptimizationProblem, list[Observation]]:
    """`_problem()`'s space with an impurity axis, and runs that report both numbers.

    One definition rather than two: the trade-off prediction test and the trade-off fit-quality
    test built this identically, eighty lines apart, and each fitted it separately.
    """
    problem = OptimizationProblem(
        parameters=_problem().parameters,
        objectives=[
            Objective(name="yield", direction="maximize"),
            Objective(name="impurity", direction="minimize"),
        ],
    )
    runs = [
        Observation(
            params=run.params,
            value=run.value,
            values={"yield": run.value, "impurity": 12.0 - 0.4 * index},
        )
        for index, run in enumerate(_runs())
    ]
    return problem, runs


@pytest.fixture(scope="module")
def two_objective_interrogation() -> tuple[list[Prediction], list[FitQuality]]:
    """One interrogation of `_two_objective()` — both halves, the shape `predict_outcome` asks for.

    The fit half is expensive, and the tests read different halves of the same answer.
    """
    problem, runs = _two_objective()
    return interrogate_surrogate(problem, runs, [{"temperature": 60.0, "solvent": "THF"}])


def test_a_trade_off_is_predicted_on_every_axis(
    two_objective_interrogation: tuple[list[Prediction], list[FitQuality]],
) -> None:
    """One fit, one prediction per objective — the W3 shape carried into the what-if."""
    prediction = two_objective_interrogation[0][0]
    assert set(prediction.values) == {"yield", "impurity"}
    assert set(prediction.sds) == {"yield", "impurity"}


def test_predicting_below_the_observation_floor_is_refused() -> None:
    """A surrogate cannot be fitted to one point, and the message says to seed first."""
    with pytest.raises(ValueError, match="at least 2 observations"):
        _predictions(_problem(), _runs()[:1], [{"temperature": 60.0, "solvent": "THF"}])


def test_predicting_at_no_point_is_refused() -> None:
    """An empty ask is a caller mistake, not an empty answer."""
    with pytest.raises(ValueError, match="neither a prediction nor a fit"):
        _predictions(_problem(), _runs(), [])


def test_point_in_domain_reads_both_kinds_of_parameter() -> None:
    """The label, on its own: a bound violation and an unknown category both fall outside."""
    problem = _problem()
    assert point_in_domain(problem, {"temperature": 20.0, "solvent": "THF"})
    assert point_in_domain(problem, {"temperature": 120.0, "solvent": "toluene"})
    assert not point_in_domain(problem, {"temperature": 19.9, "solvent": "THF"})
    assert not point_in_domain(problem, {"temperature": 60.0, "solvent": "DMSO"})


@pytest.fixture(scope="module")
def one_fit() -> list[FitQuality]:
    """One cross-validated fit of `_problem()` over `_runs()`, for the tests that read its fields.

    Cross-validation is expensive and these tests ask the same constant question.
    `test_the_fit_score_does_not_reproduce_and_is_reported_to_the_precision_it_does` must not use
    this: its subject is that repeated fits differ.
    """
    return _fit_quality(_problem(), _runs())


def test_fit_quality_is_finite_and_carries_what_it_was_computed_on(
    one_fit: list[FitQuality],
) -> None:
    """R² and MAE over held-out runs, with the run count and fold count beside them.

    The counts are not decoration: R² 0.95 over ten runs and over two hundred are different
    claims, and only one of them is about the chemistry.
    """
    quality = one_fit[0]
    assert quality.objective == "yield"
    # Content, not a bound the model already enforces: these runs are a rising trend, so a surrogate
    # predicting the mean would score about 0. `r2 is not None` checks the zero-spread branch has
    # not started firing on a real trend.
    assert quality.r2 is not None, "these runs vary; a fit quality must exist for them"
    assert quality.r2 > 0.5
    assert quality.mae < 5.0, "the runs span ~27 points; this is a fit, not a constant"
    assert quality.n_observations == 10
    assert quality.folds == 5


def test_a_score_over_few_runs_carries_the_caveat_that_it_will_be_over_read(
    one_fit: list[FitQuality],
) -> None:
    """The most over-readable number this module produces, so the caveat travels with it.

    A `computed_field` again, for `Prediction.summary`'s reason.
    """
    assert "sanity check, not as accuracy" in one_fit[0].summary


def test_the_fit_quality_names_every_objective(
    two_objective_interrogation: tuple[list[Prediction], list[FitQuality]],
) -> None:
    """One score per objective.

    A trade-off can be modelled well on one axis and badly on the other, and a single number would
    hide exactly that.
    """
    assert [q.objective for q in two_objective_interrogation[1]] == ["yield", "impurity"]


def test_cross_validating_more_folds_than_runs_is_refused_with_the_reason() -> None:
    """Each fold would hold out less than one run, which is not a score."""
    with pytest.raises(ValueError, match="less than one run"):
        _fit_quality(_problem(), _runs()[:4], folds=5)


def test_the_tool_returns_predictions_and_the_fit_behind_them() -> None:
    """The agent-facing path: one call, one fit, both halves."""
    answer = asyncio.run(
        predict_outcome(
            _problem(),
            _runs(),
            [{"temperature": 60.0, "solvent": "THF"}, {"temperature": 400.0, "solvent": "THF"}],
        )
    )
    assert len(answer.predictions) == 2
    assert not answer.predictions[1].in_domain
    assert answer.fit[0].objective == "yield"
    assert "Cross-validated" in answer.summary


def test_the_tool_can_skip_the_fit_assessment() -> None:
    """Cross-validation costs extra fits; a follow-up in the same turn need not repay them.

    When skipped, the summary says the fit was not assessed; a blank caveat would read as "no
    caveat".
    """
    answer = asyncio.run(
        predict_outcome(_problem(), _runs(), [{"temperature": 60.0, "solvent": "THF"}], False)
    )
    assert answer.fit == []
    assert "not assessed" in answer.summary


def test_the_tool_refuses_a_point_that_does_not_name_every_parameter() -> None:
    """Same fault and same sentence as an observation with a missing parameter.

    A prediction goes through `predict` rather than the acquisition step, so the library error
    differs — but the caller's mistake is identical, so the message is shared rather than restated.
    """
    with pytest.raises(ValueError, match=r"points\[0\]"):
        asyncio.run(predict_outcome(_problem(), _runs(), [{"temperature": 60.0}]))


def test_the_tool_refuses_a_point_naming_a_parameter_the_problem_does_not_declare() -> None:
    """The direction that would otherwise succeed silently against a different decision space."""
    with pytest.raises(ValueError, match="does not declare"):
        asyncio.run(
            predict_outcome(
                _problem(), _runs(), [{"temperature": 60.0, "solvent": "THF", "base": "K2CO3"}]
            )
        )


def test_the_tool_accepts_the_arrays_json_encoded_as_one_string() -> None:
    """The tolerance every other tool here has: the model sometimes emits an array as a string."""
    import json

    answer = asyncio.run(
        predict_outcome(
            _problem(),
            json.dumps([run.model_dump(mode="json") for run in _runs()]),
            json.dumps([{"temperature": 60.0, "solvent": "THF"}]),
            False,
        )
    )
    assert len(answer.predictions) == 1


def test_the_tool_the_model_sees_says_a_prediction_endorses_nothing() -> None:
    """Asserted against the served MCP description, which is what travels to the model."""
    from chemclaw.connectors.bo.server.tools import server

    tools = {tool.name: (tool.description or "") for tool in asyncio.run(server.list_tools())}
    description = tools["predict_outcome"]
    assert "endorses nothing" in description
    assert "in_domain" in description
    assert "unexplored corner" in description


# --- one fit, and the fold count that made the tool unusable early on -------------------------


def test_a_short_campaign_is_cross_validated_rather_than_refused() -> None:
    """A short campaign is cross-validated rather than refused.

    A defaulted fold count bends to the run count, so 3- and 4-run campaigns are answered;
    `FitQuality.folds` records what was used.
    """
    for n in (3, 4):
        answer = asyncio.run(
            predict_outcome(_problem(), _runs()[:n], [{"temperature": 60.0, "solvent": "THF"}])
        )
        assert answer.fit[0].folds == n
        assert answer.fit[0].n_observations == n


def test_a_fold_count_the_caller_named_is_still_refused_when_the_runs_cannot_carry_it() -> None:
    """A stated number is a claim; a defaulted one is the system's choice.

    Silently adapting a fold count the caller asked for would answer a different question than the
    one put — the same reasoning that refuses an inert screening knob rather than ignoring it.
    """
    with pytest.raises(ValueError, match="less than one run"):
        _fit_quality(_problem(), _runs()[:3], folds=5)


def test_the_prediction_and_the_score_come_from_one_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`predict_outcome` fits the surrogate exactly once — counted, not inferred.

    GP fits are non-deterministic, so "the score describes the model that made this prediction"
    holds only for one fit; counting is the only assertion that distinguishes one fit from two.
    """
    fits = 0
    original = engine._fitted_strategy

    def _counted(*args: object, **kwargs: object) -> object:
        nonlocal fits
        fits += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine, "_fitted_strategy", _counted)
    answer = asyncio.run(
        predict_outcome(_problem(), _runs(), [{"temperature": 60.0, "solvent": "THF"}])
    )
    assert fits == 1
    assert len(answer.predictions) == 1
    assert len(answer.fit) == 1


def test_the_prediction_itself_is_deterministic() -> None:
    """The half that *is* reproducible, so the non-determinism below is scoped rather than assumed.

    `strategy.predict` on an already-fitted strategy is arithmetic. Only the fit that produced the
    strategy varies, which is why the score carries a caveat and the prediction does not.
    """
    point: list[dict[str, float | str]] = [{"temperature": 60.0, "solvent": "THF"}]
    predictions, _ = interrogate_surrogate(_problem(), _runs(), point, assess_fit=False, seed=7)
    again, _ = interrogate_surrogate(_problem(), _runs(), point, assess_fit=False, seed=7)
    assert again[0].values == pytest.approx(predictions[0].values)


def test_asking_for_neither_a_prediction_nor_a_score_is_refused() -> None:
    """An empty ask is a caller mistake, not an empty answer — the same posture as no points."""
    with pytest.raises(ValueError, match="neither a prediction nor a fit"):
        interrogate_surrogate(_problem(), _runs(), [], assess_fit=False)


# --- the front and the assay it was drawn with -------------------------------------------------


def _trade_off() -> tuple[OptimizationProblem, list[Observation]]:
    """Two runs whose impurity differs by less than any real assay can resolve."""
    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name="temperature", lower=20.0, upper=120.0)],
        objectives=[
            Objective(name="yield", direction="maximize"),
            Objective(name="impurity", direction="minimize"),
        ],
    )
    runs = [
        Observation(
            params={"temperature": 60.0}, value=80.0, values={"yield": 80.0, "impurity": 5.00}
        ),
        # Same yield, impurity better by 0.01 — real to a float, invisible to an assay.
        Observation(
            params={"temperature": 61.0}, value=80.0, values={"yield": 80.0, "impurity": 4.99}
        ),
    ]
    return problem, runs


def test_the_front_at_exact_precision_splits_runs_no_assay_could_tell_apart() -> None:
    """The behaviour that prompted the tolerance, pinned so the default is a decision, not drift."""
    problem, runs = _trade_off()
    assert len(pareto_front(problem, runs)) == 1


def test_a_tolerance_keeps_both_runs_the_assay_cannot_separate() -> None:
    """With the chemist's own reproducibility, a 0.01 difference is not a difference."""
    problem, runs = _trade_off()
    assert len(pareto_front(problem, runs, tolerance=0.5)) == 2


def test_the_default_tolerance_reproduces_the_front_exactly() -> None:
    """`0.0` must be the old behaviour to the last bit — no persisted front moves."""
    problem, runs = _trade_off()
    # Pinned against the pre-tolerance *result*, not against the other call: comparing the two
    # calls only restates that the default literal is 0.0, and would hold for any implementation.
    assert pareto_front(problem, runs, 0.0) == [runs[1]]
    assert pareto_front(problem, runs) == [runs[1]]


def test_a_negative_tolerance_is_refused() -> None:
    """It is an assay reproducibility, and a negative one would invert the comparison."""
    problem, runs = _trade_off()
    with pytest.raises(ValueError, match="cannot be negative"):
        pareto_front(problem, runs, tolerance=-1.0)


@pytest.mark.timeout(60)
def test_the_suggestion_wires_the_assay_noise_through_to_the_front() -> None:
    """One acquisition, for the plumbing. The three summary readings are checked without one.

    Acquisition variance is the timeout risk, so it is paid once, to show `assay_noise` reaches
    `pareto_front` as its tolerance. The 60 s marker makes a spike fail here rather than consume the
    file's budget.
    """
    problem, runs = _trade_off()
    tolerant = asyncio.run(suggest_next_experiment(problem, runs, count=1, assay_noise=0.5))
    assert tolerant.front_tolerance == 0.5
    # Both runs survive, which they do not at exact precision — so the number reached the front.
    assert len(tolerant.front) == 2


@pytest.mark.parametrize(
    ("tolerance", "present", "absent"),
    [
        (None, "every numeric difference counted as real", "indistinguishable"),
        (0.0, "0 or less were treated as indistinguishable", "No assay reproducibility"),
        (0.5, "0.5 or less were treated as indistinguishable", "No assay reproducibility"),
    ],
)
def test_the_suggestion_says_which_front_it_drew(
    tolerance: float | None, present: str, absent: str
) -> None:
    """A reader cannot tell a strict front from a tolerant one by looking at it, so it is stated.

    Built directly: `summary` is a pure function of the fields. The zero row checks an explicit 0.0
    is not read as "none given".
    """
    suggestion = ExperimentSuggestion(
        campaign_id="campaign-test",
        candidates=[Candidate(params={"temperature": 60.0}, predicted_sd=0.5)],
        scale=_scale("yield"),
        scales=[_scale("yield"), _scale("impurity")],
        front_tolerance=tolerance,
    )
    assert present in suggestion.summary
    assert absent not in suggestion.summary


def _scale(name: str) -> ObjectiveScale:
    """A minimal scale, so `summary` has the spread it reads a candidate's sd against."""
    return ObjectiveScale(name=name, direction="maximize", n=2, observed_min=1.0, observed_max=9.0)


def test_the_fit_score_does_not_reproduce_and_is_reported_to_the_precision_it_does() -> None:
    """The fit score does not reproduce, and is reported to the precision it does.

    GP hyperparameter fitting is not deterministic even under a pinned seed, so repeated R² and MAE
    vary. This pins the property — repeats land in a band, and the summary warns against comparing
    scores that differ by less — not a value.
    """
    scores = [_fit_quality(_problem(), _runs())[0] for _ in range(3)]
    values = [score.r2 for score in scores]
    assert all(value is not None for value in values), "this fixture's runs vary; each has a score"
    spread = max(filter(None, values)) - min(filter(None, values))
    # **Both sides.** The upper bound alone would also pass if the fit were accidentally made
    # deterministic, which would make the caveats below false — and the point of this test is that
    # the number moves. Measured spread over 8 samples on this fixture: 0.081.
    assert 0.0 < spread < 0.30, f"three identical calls spread {spread}"
    summary = scores[0].summary
    assert "not deterministic" in summary
    assert "Do not read a small difference" in summary


def test_the_reported_score_is_not_printed_more_precisely_than_it_repeats(
    one_fit: list[FitQuality],
) -> None:
    """Two decimals on R², two significant figures on MAE — what survives a repeat.

    Uses the shared fit: repeatability is asserted by the sibling above, and the formatting is the
    same for any one sample.
    """
    summary = one_fit[0].summary
    # `(?!\S)` anchors the MAE group: without it, a value formatted as `1e+02` matched just the
    # leading `1`, whose one significant figure passes the check below and asserts nothing.
    matched = re.search(r"R² (\d+\.\d+) and mean absolute error (\S+?)(?=\.\s|\.$)", summary)
    assert matched is not None, summary
    assert len(matched.group(1).split(".")[1]) == 2
    digits = matched.group(2).replace(".", "").replace("-", "").lstrip("0")
    assert digits.isdigit(), f"MAE formatted unexpectedly: {matched.group(2)!r}"
    assert len(digits) <= 2, f"MAE printed to more precision than it repeats: {matched.group(2)!r}"


def test_a_score_over_enough_runs_drops_the_small_sample_caveat_but_keeps_the_repeat_one() -> None:
    """A score over enough runs drops the small-sample caveat but keeps the repeat one.

    Constructed directly, since the summary is a pure function of the fields.
    """
    over_the_threshold = FitQuality(
        objective="yield",
        r2=0.91,
        mae=1.2,
        folds=5,
        n_observations=settings.bo_fit_quality_trustworthy_observations,
        response_range=50.0,
    )
    assert "sanity check, not as accuracy" not in over_the_threshold.summary
    # The repeatability caveat is not about sample size, so it survives at any n.
    assert "not deterministic" in over_the_threshold.summary

    under = over_the_threshold.model_copy(
        update={"n_observations": settings.bo_fit_quality_trustworthy_observations - 1}
    )
    assert "sanity check, not as accuracy" in under.summary


def test_a_fold_count_below_two_is_refused() -> None:
    """One fold holds nothing out, so it is not cross-validation."""
    with pytest.raises(ValueError, match="at least 2 folds"):
        _fit_quality(_problem(), _runs(), folds=1)


def test_the_defaulted_fold_count_clamps_up_to_two_at_the_observation_floor() -> None:
    """`max(2, min(...))` — the floor must clamp *up*, not leave one fold over two runs.

    Two observations is `MIN_SEED_OBSERVATIONS`, so this is the smallest problem that can be
    cross-validated at all, and `min(5, 2)` alone would be right here only by coincidence.
    """
    assert _fit_quality(_problem(), _runs()[:2])[0].folds == 2


# --- a point's *values*, not only its parameter names ------------------------------------------
#
# BoFire validates observations in `tell` but runs no validation in `predict`, so a bad point value
# arrives as a `KeyError`/`TypeError` that `connectors.server` hides as an internal error. Points
# are checked here instead.


def test_the_tool_refuses_a_point_naming_a_category_the_problem_does_not_have() -> None:
    """A ligand nobody declared is not an extrapolation — it is a level with no encoding.

    Unchecked, `strategy.predict` raises a `KeyError` that is neither a `ValueError` nor a known
    surrogate failure.
    """
    with pytest.raises(ValueError, match=r"points\[0\]"):
        asyncio.run(predict_outcome(_problem(), _runs(), [{"temperature": 60.0, "solvent": "DMF"}]))


def test_the_refusal_names_the_parameter_the_value_and_the_levels_that_exist() -> None:
    """Which one, and what may it be: that is the whole repair — a bare refusal is a dead end."""
    with pytest.raises(ValueError) as raised:
        asyncio.run(predict_outcome(_problem(), _runs(), [{"temperature": 60.0, "solvent": "DMF"}]))
    message = str(raised.value)
    assert "'solvent'" in message
    assert "'DMF'" in message
    assert "THF" in message and "toluene" in message


def test_the_tool_refuses_a_point_whose_continuous_value_is_not_a_number() -> None:
    """A continuous value that is not a number is refused rather than reaching torch."""
    with pytest.raises(ValueError, match=r"points\[0\]"):
        asyncio.run(
            predict_outcome(_problem(), _runs(), [{"temperature": "hot", "solvent": "THF"}])
        )


def test_a_point_outside_a_continuous_bound_is_still_answered() -> None:
    """A point outside a continuous bound is still answered.

    The value check must not turn documented extrapolation into a refusal; `point_in_domain` is
    False for both cases, so it cannot be the check.
    """
    answer = asyncio.run(
        predict_outcome(_problem(), _runs(), [{"temperature": 400.0, "solvent": "THF"}], False)
    )
    assert not answer.predictions[0].in_domain
    assert "extrapolating" in answer.predictions[0].summary


def test_an_objective_with_no_spread_at_all_refuses_an_r2_instead_of_reporting_a_perfect_fit() -> (
    None
):
    """An objective with no spread refuses an R² instead of reporting a perfect fit.

    With no variance R² has no denominator, and BoFire scores it 1.0. A flatlined assay is exactly
    when a chemist asks whether to trust the model.
    """
    problem = _problem()
    flat = [
        Observation(params={"temperature": 20.0 + 10.0 * i, "solvent": "THF"}, value=42.0)
        for i in range(8)
    ]
    quality = _fit_quality(problem, flat, folds=5, seed=11)[0]
    assert quality.r2 is None, "a constant target cannot be predicted well or badly"
    # "no **usable** variance", because the guard this asserts is no longer exact equality: see
    # `test_a_systematic_sub_noise_drift_is_not_a_response_either` for the input that made the word
    # necessary — those runs do differ, by an amount no assay resolves.
    assert "no usable variance" in quality.summary
    assert "R²" not in quality.summary
    assert quality.response_range == 0.0


def test_a_systematic_sub_noise_drift_is_not_a_response_either() -> None:
    """A systematic sub-noise drift is not a response either.

    `spread == 0.0` misses a trend in the last decimal, which BoFire scores near-perfect. The
    threshold is relative to the response's magnitude, set by `bo_flat_response_relative_spread`.
    """
    problem = _problem()
    drifting = [
        Observation(
            params={"temperature": 20.0 + 10.0 * i, "solvent": "THF"}, value=42.0 + i * 1.7e-10
        )
        for i in range(8)
    ]
    quality = _fit_quality(problem, drifting, folds=5, seed=11)[0]
    assert quality.r2 is None, "a nanounit of drift on 42.0 is not a response a model explains"
    assert 0.0 < quality.response_range < 1e-8


def test_a_score_states_the_range_it_is_a_fraction_of() -> None:
    """A score states the range it is a fraction of.

    R² is scale-free, so the summary must state the range. Constructed directly; the summary is a
    pure function of the fields.
    """
    quality = FitQuality(
        objective="yield", r2=0.91, mae=1.2, folds=5, n_observations=8, response_range=48.5
    )
    assert "response range of 48.5" in quality.summary


def test_the_defaulted_fold_count_never_asks_for_more_folds_than_there_are_runs() -> None:
    """The defaulted fold count never asks for more folds than there are runs.

    A defaulted count bends down only to 2, which is safe only because `interrogate_surrogate`
    refuses below `MIN_SEED_OBSERVATIONS == 2`; asserted from the constant so lowering it fails
    here.
    """
    from chemclaw.science.bo.engine import _resolve_folds
    from chemclaw.science.bo.problem import MIN_SEED_OBSERVATIONS

    assert _resolve_folds(None, MIN_SEED_OBSERVATIONS) <= MIN_SEED_OBSERVATIONS
