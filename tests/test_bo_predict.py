"""Tests for reading the fitted surrogate back (W5).

Two capabilities over one fit: what the model expects at a point the *caller* named, and how well
that model predicts runs it was not shown. The second was refused until a measurement reversed the
refusal — the objection was that `cross_validate` forces us to name a surrogate class, and
`strategy.surrogate_specs` turns out to expose the one BoFire itself chose (M-7).
"""

import asyncio

import pytest

from chemclaw.connectors.bo.server.tools import predict_outcome
from chemclaw.science.bo.engine import predict_at, surrogate_fit_quality
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
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
    prediction = predict_at(problem, runs, [{"temperature": 70.0, "ligand": "L3"}])[0]
    assert prediction.sds["yield"] > 0.0


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
    assert -1.0 <= quality.r2 <= 1.0
    assert quality.mae >= 0.0
    assert quality.n_observations == 10
    assert quality.folds == 5


def test_a_score_over_few_runs_carries_the_caveat_that_it_will_be_over_read() -> None:
    """The most over-readable number this module produces, so the caveat travels with it.

    A `computed_field` again, for `Prediction.summary`'s reason.
    """
    summary = surrogate_fit_quality(_problem(), _runs())[0].summary
    assert "sanity check, not as accuracy" in summary
    assert "10 run(s)" in summary


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
    """Cross-validation costs extra fits; a follow-up in the same turn need not repay them."""
    answer = asyncio.run(
        predict_outcome(_problem(), _runs(), [{"temperature": 60.0, "solvent": "THF"}], False)
    )
    assert answer.fit == []
    assert answer.summary == ""


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
