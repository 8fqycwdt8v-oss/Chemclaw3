"""Tests for reading the fitted surrogate back (W5).

Two capabilities over one fit: what the model expects at a point the *caller* named, and how well
that model predicts runs it was not shown. The second was refused until a measurement reversed the
refusal — the objection was that `cross_validate` forces us to name a surrogate class, and
`strategy.surrogate_specs` turns out to expose the one BoFire itself chose (M-7).
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
from chemclaw.science.bo.engine import (
    interrogate_surrogate,
    predict_at,
    surrogate_fit_quality,
)
from chemclaw.science.bo.problem import (
    Candidate,
    CategoricalParameter,
    ContinuousParameter,
    FitQuality,
    Objective,
    Observation,
    OptimizationProblem,
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


def test_a_point_among_the_runs_predicts_near_what_was_measured() -> None:
    """The floor: a surrogate that cannot reproduce its own training data explains nothing."""
    runs = _runs()
    at = dict(runs[4].params)
    prediction = predict_at(_problem(), runs, [at])[0]
    assert prediction.values["yield"] == pytest.approx(runs[4].value, abs=3.0)
    assert prediction.sds["yield"] < 5.0
    assert prediction.in_domain


def test_an_unexplored_corner_carries_a_larger_sd_than_an_observed_point() -> None:
    """The posterior question `op-13` asks and the run list cannot answer.

    "Is there an unexplored corner, or has the search been circling one region" is a statement
    about the model's uncertainty, so it needs the model. The runs alternate THF and toluene at
    every 10 °C from 20; the corner below asks about a temperature nothing sits near.
    """
    runs = _runs()
    observed, corner = predict_at(
        _problem(),
        runs,
        [dict(runs[0].params), {"temperature": 119.0, "solvent": "THF"}],
    )
    assert corner.sds["yield"] > observed.sds["yield"]
    assert corner.in_domain


def test_an_out_of_range_point_is_answered_and_labelled_rather_than_refused() -> None:
    """Measured (M-6): BoFire does not clamp — it extrapolates, and the sd rises sharply.

    Refusing would withhold a number the chemist can read correctly once told which side of the
    bound they are on, so the point is answered with `in_domain` false and a summary that says the
    mean is unconstrained there.
    """
    runs = _runs()
    inside, outside = predict_at(
        _problem(),
        runs,
        [{"temperature": 60.0, "solvent": "THF"}, {"temperature": 400.0, "solvent": "THF"}],
    )
    assert inside.in_domain
    assert not outside.in_domain
    assert outside.sds["yield"] > 5 * inside.sds["yield"]
    assert "outside" in outside.summary
    assert "extrapolating" in outside.summary


def test_a_prediction_says_it_is_not_a_recommendation() -> None:
    """The whole reason `Prediction` is not `Candidate`.

    A candidate carries an implicit endorsement; an answer to a question carries none. The
    distinction lives in a `computed_field`, not a docstring, because a bare property is not
    serialized and the caveat would never reach the model composing the reply.
    """
    prediction = predict_at(_problem(), _runs(), [{"temperature": 60.0, "solvent": "THF"}])[0]
    assert "not a recommendation" in prediction.summary


def test_a_featurized_categorical_is_accepted() -> None:
    """The shape a problem with molecular options actually reaches the engine as.

    `featurize_problem` turns a categorical carrying `structures` into descriptor values, which
    become a `CategoricalDescriptorInput`. Measured (M-6) that `predict` handles that domain; this
    pins it against our own types rather than against the measurement script.
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
    at_seventy = predict_at(
        problem,
        runs,
        [{"temperature": 70.0, "ligand": ligand} for ligand in ("L1", "L2", "L3")],
    )
    assert len({round(p.values["yield"], 6) for p in at_seventy}) == 3


def test_a_trade_off_is_predicted_on_every_axis() -> None:
    """One fit, one prediction per objective — the W3 shape carried into the what-if."""
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
    prediction = predict_at(problem, runs, [{"temperature": 60.0, "solvent": "THF"}])[0]
    assert set(prediction.values) == {"yield", "impurity"}
    assert set(prediction.sds) == {"yield", "impurity"}


def test_predicting_below_the_observation_floor_is_refused() -> None:
    """A surrogate cannot be fitted to one point, and the message says to seed first."""
    with pytest.raises(ValueError, match="at least 2 observations"):
        predict_at(_problem(), _runs()[:1], [{"temperature": 60.0, "solvent": "THF"}])


def test_predicting_at_no_point_is_refused() -> None:
    """An empty ask is a caller mistake, not an empty answer."""
    with pytest.raises(ValueError, match="at least one point"):
        predict_at(_problem(), _runs(), [])


def test_point_in_domain_reads_both_kinds_of_parameter() -> None:
    """The label, on its own: a bound violation and an unknown category both fall outside."""
    problem = _problem()
    assert point_in_domain(problem, {"temperature": 20.0, "solvent": "THF"})
    assert point_in_domain(problem, {"temperature": 120.0, "solvent": "toluene"})
    assert not point_in_domain(problem, {"temperature": 19.9, "solvent": "THF"})
    assert not point_in_domain(problem, {"temperature": 60.0, "solvent": "DMSO"})


def test_fit_quality_is_finite_and_carries_what_it_was_computed_on() -> None:
    """R² and MAE over held-out runs, with the run count and fold count beside them.

    The counts are not decoration: R² 0.95 over ten runs and over two hundred are different
    claims, and only one of them is about the chemistry.
    """
    quality = surrogate_fit_quality(_problem(), _runs())[0]
    assert quality.objective == "yield"
    # Content, not a bound the model already enforces: `mae >= 0.0` is `Field(ge=0.0)` and cannot
    # fail, and `r2 <= 1.0` is arithmetic. These runs are a deliberate rising trend, so a surrogate
    # that had regressed to predicting the mean would score about 0 and be caught here.
    assert quality.r2 > 0.5
    assert quality.mae < 5.0, "the runs span ~27 points; this is a fit, not a constant"
    assert quality.n_observations == 10
    assert quality.folds == 5


def test_a_score_over_few_runs_carries_the_caveat_that_it_will_be_over_read() -> None:
    """The most over-readable number this module produces, so the caveat travels with it.

    A `computed_field` again, for `Prediction.summary`'s reason.
    """
    summary = surrogate_fit_quality(_problem(), _runs())[0].summary
    assert "sanity check, not as accuracy" in summary


def test_the_fit_quality_names_every_objective() -> None:
    """One score per objective.

    A trade-off can be modelled well on one axis and badly on the other, and a single number would
    hide exactly that.
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
    assert [q.objective for q in surrogate_fit_quality(problem, runs)] == ["yield", "impurity"]


def test_cross_validating_more_folds_than_runs_is_refused_with_the_reason() -> None:
    """Each fold would hold out less than one run, which is not a score."""
    with pytest.raises(ValueError, match="less than one run"):
        surrogate_fit_quality(_problem(), _runs()[:4], folds=5)


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

    The summary used to be empty here and now says the fit was not assessed — a blank caveat reads
    as "no caveat", which is the opposite of what it means.
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
    """The bug the review found: `predict_outcome` raised on every 3- and 4-run campaign.

    `bo_cv_folds` is 5 and the tool always defaulted, so a campaign above the seeding floor but
    below five runs — the early campaign this tool is most useful for — got a `ValueError` instead
    of an answer. A defaulted fold count now bends to the run count, and `FitQuality.folds` records
    what was actually used so the adaptation is visible rather than hidden.
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
        surrogate_fit_quality(_problem(), _runs()[:3], folds=5)


def test_the_prediction_and_the_score_come_from_one_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`predict_outcome` fits the surrogate exactly once — counted, not inferred.

    **This test replaced a version that could not fail.** It used to run `interrogate_surrogate`
    and `predict_at` separately and assert their predictions agreed, which is two fits agreeing —
    the by-construction check it claimed to replace. Reverting the tool to fit twice left it green.

    Counting the fits is the only assertion that distinguishes the two designs, and the distinction
    is load-bearing: the GP's hyperparameter fit is non-deterministic, so two fits are genuinely two
    models, and "the score describes the model that made this prediction" would be false.
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
    predictions, _ = interrogate_surrogate(_problem(), _runs(), point, assess_fit=False)
    assert predict_at(_problem(), _runs(), point)[0].values == pytest.approx(predictions[0].values)


def test_asking_for_neither_a_prediction_nor_a_score_is_refused() -> None:
    """An empty ask is a caller mistake, not an empty answer — the same posture as no points."""
    with pytest.raises(ValueError, match="neither a prediction nor a fit"):
        interrogate_surrogate(_problem(), _runs(), [], assess_fit=False)


def test_a_skipped_fit_says_so_rather_than_returning_an_empty_summary() -> None:
    """A blank summary reads as "no caveat", which is the opposite of what it means."""
    answer = asyncio.run(
        predict_outcome(_problem(), _runs(), [{"temperature": 60.0, "solvent": "THF"}], False)
    )
    assert answer.fit == []
    assert "not assessed" in answer.summary


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

    This used to make two `suggest_next_experiment` calls and a sibling made a third — each fitting
    a GP and running a multi-start acquisition to assert a sentence. That optimizer's run-to-run
    variance is the real timeout risk here (a sibling was measured spiking from 4.3s to 39.9s), so
    the acquisition is paid once, for the one thing only the tool can show: that `assay_noise`
    reaches `pareto_front` as its tolerance.

    The explicit timeout is the point of the marker rather than a guess at a budget: at 60s a spike
    names itself instead of eating the 180s the whole file shares.
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

    Built directly rather than through the tool: `summary` is a pure function of the fields, and an
    acquisition run would cost seconds to assert nothing the constructor cannot. The zero row is the
    one that caught a real bug — `if self.front_tolerance` read an explicit 0.0 as "none given".
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
    """The measurement that changed how this number is printed.

    BoFire fits the GP's hyperparameters by numerical optimization and that fit is **not**
    deterministic — not under a pinned `torch` seed and not with a fresh copy of the surrogate
    spec. Measured over twelve identical calls on this problem: R² spanned 0.906-0.969 and MAE
    spanned 1.16-1.80, so MAE moved by more than half its own value. The first version printed R²
    to three decimals and MAE to three significant figures, stating a stability neither has.

    This test pins the *property*, not a value: repeats must land in a band, and the summary must
    warn a reader off comparing two scores that differ by less than it.
    """
    scores = [surrogate_fit_quality(_problem(), _runs())[0] for _ in range(3)]
    spread = max(s.r2 for s in scores) - min(s.r2 for s in scores)
    # **Both sides.** The upper bound alone would also pass if the fit were accidentally made
    # deterministic, which would make the caveats below false — and the point of this test is that
    # the number moves. Measured spread over 8 samples on this fixture: 0.081.
    assert 0.0 < spread < 0.30, f"three identical calls spread {spread}"
    summary = scores[0].summary
    assert "not deterministic" in summary
    assert "Do not read a small difference" in summary


def test_the_reported_score_is_not_printed_more_precisely_than_it_repeats() -> None:
    """Two decimals on R², two significant figures on MAE — what survives a repeat."""
    summary = surrogate_fit_quality(_problem(), _runs())[0].summary
    # `(?!\S)` anchors the MAE group: without it, a value formatted as `1e+02` matched just the
    # leading `1`, whose one significant figure passes the check below and asserts nothing.
    matched = re.search(r"R² (\d+\.\d+) and mean absolute error (\S+?)(?=\.\s|\.$)", summary)
    assert matched is not None, summary
    assert len(matched.group(1).split(".")[1]) == 2
    digits = matched.group(2).replace(".", "").replace("-", "").lstrip("0")
    assert digits.isdigit(), f"MAE formatted unexpectedly: {matched.group(2)!r}"
    assert len(digits) <= 2, f"MAE printed to more precision than it repeats: {matched.group(2)!r}"


def test_a_score_over_enough_runs_drops_the_small_sample_caveat_but_keeps_the_repeat_one() -> None:
    """The high-`n` branch of the summary was never rendered by any test.

    Only the "fewer than 20 runs" branch was exercised, so a formatting break in the other half
    would have shipped silently. Constructed directly rather than fitted: the summary is a pure
    function of the fields, and a real fit here would cost seconds to assert nothing extra.
    """
    over_the_threshold = FitQuality(
        objective="yield",
        r2=0.91,
        mae=1.2,
        folds=5,
        n_observations=settings.bo_fit_quality_trustworthy_observations,
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
        surrogate_fit_quality(_problem(), _runs(), folds=1)


def test_the_defaulted_fold_count_clamps_up_to_two_at_the_observation_floor() -> None:
    """`max(2, min(...))` — the floor must clamp *up*, not leave one fold over two runs.

    Two observations is `MIN_SEED_OBSERVATIONS`, so this is the smallest problem that can be
    cross-validated at all, and `min(5, 2)` alone would be right here only by coincidence.
    """
    assert surrogate_fit_quality(_problem(), _runs()[:2])[0].folds == 2


# --- a point's *values*, not only its parameter names ------------------------------------------
#
# `_require_points_match` checked that a point names exactly the declared parameters and never what
# it names them with. The asymmetry that makes this matter is BoFire's, and it is measured: `tell`
# runs `validate_experimental`, so the identical mistake in an **observation** already comes back as
# a plain `ValueError` the connector forwards verbatim; `predict` runs no validation at all, so the
# same mistake in a **point** arrives as a `KeyError`/`TypeError` that `connectors.server` replaces
# with "an internal error occurred" — nothing the model can repair from, so it retries.


def test_the_tool_refuses_a_point_naming_a_category_the_problem_does_not_have() -> None:
    """A ligand nobody declared is not an extrapolation — it is a level with no encoding.

    Measured before the fix: `strategy.predict` on `{"solvent": "DMF"}` over a two-level `solvent`
    raised `KeyError: "None of [Index(['DMF'], dtype='str')] are in the [index]"`, which is neither
    a `ValueError` nor one of `_SURROGATE_FAILURES`.
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
    """The same fault in the other direction, measured as a `TypeError` before the fix.

    `{"temperature": "hot"}` reached torch as an object-dtype array ("can't convert np.ndarray of
    type numpy.object_"), where the same value in an observation already yields BoFire's own "not
    all values of input feature `temperature` are numerical".
    """
    with pytest.raises(ValueError, match=r"points\[0\]"):
        asyncio.run(
            predict_outcome(_problem(), _runs(), [{"temperature": "hot", "solvent": "THF"}])
        )


def test_a_point_outside_a_continuous_bound_is_still_answered() -> None:
    """The documented behaviour the value check must not break.

    "Out-of-range points are answered, not refused" is true of a *range*: the model extrapolates and
    the widened sd is the honest signal. Pinned because the natural over-fix for the category case —
    validating every point through `point_in_domain` — would silently turn this documented answer
    into a refusal, and `point_in_domain` returns False for both.
    """
    answer = asyncio.run(
        predict_outcome(_problem(), _runs(), [{"temperature": 400.0, "solvent": "THF"}], False)
    )
    assert not answer.predictions[0].in_domain
    assert "extrapolating" in answer.predictions[0].summary
