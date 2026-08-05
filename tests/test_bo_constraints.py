"""Tests for cross-parameter constraints on a BO problem (W4).

The measurement that decided this wave (M-3) is the reason there is no rejection-sampling path
here: the risk was never `SoboStrategy` but `RandomStrategy`, which seeds every cold-start
campaign. Had it ignored `Domain.constraints`, the schema would have claimed a limit was honoured
while every seed point violated it. Both honour them, so the tests below assert the property
directly on what each path returns.
"""

import asyncio
from typing import Literal

import pytest

from chemclaw.connectors.bo.server.tools import generate_screening_design, suggest_next_experiment
from chemclaw.science.bo.campaign_record import campaign_id_for
from chemclaw.science.bo.engine import factorial_design, initial_candidates, propose_candidates
from chemclaw.science.bo.problem import (
    Candidate,
    CategoricalParameter,
    ContinuousParameter,
    ExcludeConstraint,
    LinearConstraint,
    Objective,
    Observation,
    OptimizationProblem,
    discrete_candidate_count,
    space_exhausted,
)

TOLERANCE = 1e-6


def _capped_problem(
    relation: Literal["<=", ">=", "=="] = "<=", rhs: float = 3.0
) -> OptimizationProblem:
    """Two equivalents that must sum within a budget, beside a solvent choice."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="base", lower=0.0, upper=3.0),
            ContinuousParameter(name="acid", lower=0.0, upper=3.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
        constraints=[
            LinearConstraint(
                parameters=["base", "acid"], coefficients=[1.0, 1.0], relation=relation, rhs=rhs
            )
        ],
    )


def _feasible_runs() -> list[Observation]:
    """Six runs inside `base + acid <= 3`, enough to fit a surrogate."""
    points = [(0.2, 0.3, "THF"), (1.0, 0.5, "toluene"), (1.5, 1.0, "THF")]
    points += [(0.8, 1.8, "toluene"), (2.0, 0.9, "THF"), (0.4, 2.4, "toluene")]
    return [
        Observation(params={"base": base, "acid": acid, "solvent": solvent}, value=40.0 + 5 * index)
        for index, (base, acid, solvent) in enumerate(points)
    ]


def test_the_seeding_path_honours_the_constraint() -> None:
    """`initial_candidates` is `RandomStrategy`, and it seeds every cold start.

    This is the measurement the whole wave turned on. A seeding path that ignored the constraint
    would let the schema claim a limit was honoured while every first point violated it — and a
    chemist would only find out in the lab.
    """
    candidates = initial_candidates(_capped_problem(), 20)
    # Bound and counted first: `for x in []: assert ...` passes, so a strategy that returned
    # nothing would satisfy every assertion below while proving none of them.
    assert len(candidates) == 20
    for candidate in candidates:
        total = float(candidate.params["base"]) + float(candidate.params["acid"])
        assert total <= 3.0 + TOLERANCE, candidate.params


@pytest.mark.timeout(90)
def test_the_proposing_path_honours_the_constraint() -> None:
    """And so does the model-guided ask, on a mixed domain.

    **Two candidates, not five, and the number is measured.** A constraint makes the acquisition
    step several times more expensive — 10.3s against 3.2s for one candidate on this domain, then
    about 9s per further candidate, so five took 46s locally and blew a 180s CI timeout under
    coverage. The property under test is that every returned point satisfies the relation, which two
    points prove as well as five.
    """
    candidates = propose_candidates(_capped_problem(), _feasible_runs(), n=2)
    assert len(candidates) == 2
    for candidate in candidates:
        total = float(candidate.params["base"]) + float(candidate.params["acid"])
        assert total <= 3.0 + TOLERANCE, candidate.params


def test_a_greater_than_constraint_is_honoured_in_the_direction_the_caller_wrote() -> None:
    """The M-3(a) pin, and the one bug here that yields a confident wrong answer.

    BoFire has no `>=` class, so it is the same inequality with every sign flipped. Getting that
    negation backwards would silently invert a limit the chemist stated — the optimizer would
    happily return points *below* a floor they asked to stay above, with no error anywhere.
    """
    candidates = initial_candidates(_capped_problem(relation=">=", rhs=2.0), 20)
    assert len(candidates) == 20
    for candidate in candidates:
        total = float(candidate.params["base"]) + float(candidate.params["acid"])
        assert total >= 2.0 - TOLERANCE, candidate.params


def test_an_equality_puts_every_point_on_the_simplex() -> None:
    """The mixture/formulation case, which comes free as `relation: "=="`."""
    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name=name, lower=0.0, upper=1.0) for name in "xyz"],
        objectives=[Objective(name="yield", direction="maximize")],
        constraints=[
            LinearConstraint(
                parameters=["x", "y", "z"], coefficients=[1.0, 1.0, 1.0], relation="==", rhs=1.0
            )
        ],
    )
    candidates = initial_candidates(problem, 10)
    assert len(candidates) == 10
    for candidate in candidates:
        total = sum(float(candidate.params[name]) for name in "xyz")
        assert total == pytest.approx(1.0, abs=1e-6)


def test_a_constraint_naming_a_categorical_is_refused_with_a_message_to_act_on() -> None:
    """BoFire refuses it too; what this adds is a sentence naming the parameter and the fix."""
    with pytest.raises(ValueError, match="categorical parameter"):
        OptimizationProblem(
            parameters=[
                ContinuousParameter(name="base", lower=0.0, upper=3.0),
                CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
            ],
            objectives=[Objective(name="yield", direction="maximize")],
            constraints=[
                LinearConstraint(parameters=["base", "solvent"], coefficients=[1.0, 1.0], rhs=3.0)
            ],
        )


def test_a_constraint_naming_an_undeclared_parameter_lists_the_declared_ones() -> None:
    """Otherwise the mistake surfaces far from where it was made."""
    with pytest.raises(ValueError, match="undeclared parameter"):
        OptimizationProblem(
            parameters=[ContinuousParameter(name="base", lower=0.0, upper=3.0)],
            objectives=[Objective(name="yield", direction="maximize")],
            constraints=[
                LinearConstraint(parameters=["base", "ligand"], coefficients=[1.0, 1.0], rhs=3.0)
            ],
        )


def test_a_coefficient_count_mismatch_is_refused() -> None:
    """One coefficient per parameter; a missing one would silently drop a term."""
    with pytest.raises(ValueError, match="coefficient"):
        LinearConstraint(parameters=["base", "acid"], coefficients=[1.0], rhs=3.0)


def test_an_unconstrained_problem_keeps_the_campaign_id_it_had() -> None:
    """`constraints` enters the identity only when non-empty (the W3 rule, one field on)."""
    unconstrained = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            ContinuousParameter(name="equiv", lower=1.0, upper=3.0),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    assert campaign_id_for(unconstrained) == "campaign-6958b7edaa261c83"


def test_adding_a_constraint_makes_it_a_different_campaign() -> None:
    """A constraint narrows the space, so the runs mean something different."""
    plain = _capped_problem().model_copy(update={"constraints": []})
    assert campaign_id_for(_capped_problem()) != campaign_id_for(plain)


def test_a_screen_refuses_a_constrained_problem_rather_than_violating_it() -> None:
    """`FractionalFactorialStrategy` honours no constraint; it enumerates corners.

    Measured: BoFire rejects every constraint class for this strategy at construction, linear and
    exclusion alike. So the refusal here is the *message* — raised where the caller can act on it,
    rather than arriving as a pydantic error naming a BoFire class.
    """
    with pytest.raises(ValueError, match="cannot honour a constraint"):
        factorial_design(_capped_problem())
    with pytest.raises(ValueError, match="cannot honour a constraint"):
        asyncio.run(generate_screening_design(_capped_problem()))


@pytest.mark.timeout(90)
def test_the_tool_honours_a_constraint_end_to_end() -> None:
    """The agent-facing path, not just the engine."""
    suggestion = asyncio.run(suggest_next_experiment(_capped_problem(), _feasible_runs(), count=2))
    assert len(suggestion.candidates) == 2
    for candidate in suggestion.candidates:
        total = float(candidate.params["base"]) + float(candidate.params["acid"])
        assert total <= 3.0 + TOLERANCE, candidate.params


def test_the_tool_the_model_sees_states_that_constraints_are_supported_now() -> None:
    """W3 left this half of the refusal verbatim; W4 is what replaces it."""
    from chemclaw.connectors.bo.server.tools import server

    tools = {tool.name: (tool.description or "") for tool in asyncio.run(server.list_tools())}
    description = tools["suggest_next_experiment"]
    assert "problem.constraints" in description
    assert "Constraints are still unrepresentable" not in description


def test_the_note_records_the_limits_as_well_as_the_bounds() -> None:
    """A "Searched over:" block alone would describe a box the campaign never had."""
    from chemclaw.connectors.bo.knowledge import note_from_campaign_result
    from chemclaw.science.bo.problem import CampaignResult

    runs = _feasible_runs()
    note = note_from_campaign_result(
        "yield", _capped_problem(), CampaignResult(best=runs[-1], history=runs)
    )
    assert "Subject to:" in note.body
    assert "base + acid <= 3" in note.body


def _excluding_problem() -> OptimizationProblem:
    """An all-categorical coupling screen where one catalyst/solvent pairing is forbidden."""
    return OptimizationProblem(
        parameters=[
            CategoricalParameter(name="catalyst", categories=["Pd(OAc)2", "Pd2dba3"]),
            CategoricalParameter(name="solvent", categories=["DMSO", "toluene"]),
            CategoricalParameter(name="base", categories=["K2CO3", "Cs2CO3"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
        constraints=[
            ExcludeConstraint(parameters=["catalyst", "solvent"], options=[["Pd(OAc)2"], ["DMSO"]])
        ],
    )


def _forbidden(candidates: list[Candidate] | list[Observation]) -> int:
    """How many points pair the excluded catalyst with the excluded solvent."""
    return sum(
        1
        for candidate in candidates
        if candidate.params["catalyst"] == "Pd(OAc)2" and candidate.params["solvent"] == "DMSO"
    )


def test_the_seeding_path_honours_an_exclusion() -> None:
    """Same argument as the linear case: the seed points are what a cold start ships to the lab.

    Six is the whole feasible space, so this also asks for every point in it — a seeding path that
    ignored the exclusion had eight cells to draw from and would be caught here.
    """
    candidates = initial_candidates(_excluding_problem(), 6)
    assert _forbidden(candidates) == 0
    # The docstring's claim, asserted rather than described: six is the whole feasible space, so
    # these must be exactly the six cells the exclusion leaves. A `_forbidden(...) == 0` over a
    # short list would pass while proving much less.
    assert {tuple(sorted(candidate.params.items())) for candidate in candidates} == {
        tuple(sorted({"catalyst": catalyst, "solvent": solvent, "base": base}.items()))
        for catalyst in ("Pd(OAc)2", "Pd2dba3")
        for solvent in ("DMSO", "toluene")
        for base in ("K2CO3", "Cs2CO3")
        if not (catalyst == "Pd(OAc)2" and solvent == "DMSO")
    }


def test_the_proposing_path_honours_an_exclusion() -> None:
    """And the model-guided ask, over a purely categorical space."""
    problem = _excluding_problem()
    runs = [
        Observation(params=dict(zip(["catalyst", "solvent", "base"], point, strict=True)), value=v)
        for point, v in [
            (("Pd2dba3", "DMSO", "K2CO3"), 41.0),
            (("Pd2dba3", "toluene", "Cs2CO3"), 55.0),
            (("Pd(OAc)2", "toluene", "K2CO3"), 62.0),
            (("Pd(OAc)2", "toluene", "Cs2CO3"), 70.0),
        ]
    ]
    candidates = propose_candidates(problem, runs, n=3)
    # **Fewer than asked for, and that is measured, not assumed.** Four runs are already told, so
    # only two feasible cells remain and BoFire warns "Expected 3 candidates, got 2". Asserting
    # `_forbidden(...) == 0` alone would pass on an empty list; the count is what makes the zero
    # mean something, and pinning the shortfall is what stops it changing unnoticed.
    assert len(candidates) == 2
    assert _forbidden(candidates) == 0


def test_an_exclusion_beside_a_continuous_parameter_is_refused_with_the_reason() -> None:
    """Measured: BoFire needs a pure categorical space to enumerate, and says so obscurely.

    The caller cannot act on "can only be used for pure categorical/discrete search spaces"; they
    can act on being told which of *their* parameters is the continuous one.
    """
    with pytest.raises(ValueError, match="all-categorical problem"):
        OptimizationProblem(
            parameters=[
                CategoricalParameter(name="catalyst", categories=["Pd(OAc)2", "Pd2dba3"]),
                CategoricalParameter(name="solvent", categories=["DMSO", "toluene"]),
                ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            ],
            objectives=[Objective(name="yield", direction="maximize")],
            constraints=[
                ExcludeConstraint(
                    parameters=["catalyst", "solvent"], options=[["Pd(OAc)2"], ["DMSO"]]
                )
            ],
        )


def test_an_exclusion_naming_an_option_the_parameter_does_not_have_is_refused() -> None:
    """A typo in an option name would otherwise exclude nothing at all, silently."""
    with pytest.raises(ValueError, match="does not have"):
        OptimizationProblem(
            parameters=[
                CategoricalParameter(name="catalyst", categories=["Pd(OAc)2", "Pd2dba3"]),
                CategoricalParameter(name="solvent", categories=["DMSO", "toluene"]),
            ],
            objectives=[Objective(name="yield", direction="maximize")],
            constraints=[
                ExcludeConstraint(
                    parameters=["catalyst", "solvent"], options=[["Pd(OAC)2"], ["DMSO"]]
                )
            ],
        )


def test_an_exclusion_naming_a_continuous_parameter_says_which_shape_to_use() -> None:
    """The other way round from the whole-problem check: the constraint itself names a knob."""
    with pytest.raises(ValueError, match="continuous parameter"):
        OptimizationProblem(
            parameters=[
                CategoricalParameter(name="solvent", categories=["DMSO", "toluene"]),
                ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            ],
            objectives=[Objective(name="yield", direction="maximize")],
            constraints=[
                ExcludeConstraint(parameters=["solvent", "temperature"], options=[["DMSO"], ["90"]])
            ],
        )


def test_an_exclusion_shrinks_the_space_every_caller_counts() -> None:
    """The bug this wave could have shipped: an over-counted space.

    2x2x2 is eight cells, but the excluded catalyst/solvent pairing removes two, so the feasible
    space holds six. Both readers of that number act on it — `initial_candidates` refuses `n` above
    it, and `space_exhausted` calls a campaign finished by it — so counting the product would have
    made a loop ask for two points that cannot exist and let BoFire's discrete acquisition raise
    mid-campaign, which is the exact failure the count was introduced to prevent.
    """
    problem = _excluding_problem()
    assert discrete_candidate_count(problem) == 6
    assert not space_exhausted(discrete_candidate_count(problem), [], batch=6)
    assert space_exhausted(discrete_candidate_count(problem), [], batch=7)
    with pytest.raises(ValueError, match="only 6"):
        initial_candidates(problem, 7)


def test_an_exclusion_round_trips_through_the_discriminated_union() -> None:
    """`kind` is what lets a persisted constraint come back as the right type."""
    problem = _excluding_problem()
    revived = OptimizationProblem.model_validate(problem.model_dump(mode="json"))
    assert isinstance(revived.constraints[0], ExcludeConstraint)
    assert revived.constraints[0].describe() == "never catalyst=Pd(OAc)2 with solvent=DMSO"
    assert campaign_id_for(revived) == campaign_id_for(problem)


def test_an_exhausted_space_is_refused_with_a_sentence_instead_of_a_keyerror() -> None:
    """The crash two independent reviews found, and the mystery it turned out to be.

    When every cell of a discrete space has been run, BoFire's `_optimize_acqf_discrete` drops the
    already-run rows, hands an empty frame to `domain.inputs.transform`, and raises
    `KeyError: '<parameter>'`. That is neither a `ValueError` nor one of `_SURROGATE_FAILURES`, so
    `connectors.server` replaced it with "an internal error occurred" — nothing the model can act
    on, and it retries.

    `_require_observed_params_match`'s docstring records a live `KeyError: 'base'` from this exact
    BoFire frame that could not be reproduced and was written up as **unproven**. This is it: the
    cause is exhaustion, not a parameter mismatch, which is why driving mismatched parameters never
    reproduced it. W4 made it reachable sooner, because an exclusion removes cells.
    """
    problem = OptimizationProblem(
        parameters=[CategoricalParameter(name="catalyst", categories=["Pd", "Ni"])],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    both_cells = [
        Observation(params={"catalyst": "Pd"}, value=40.0),
        Observation(params={"catalyst": "Ni"}, value=55.0),
    ]
    with pytest.raises(ValueError, match="no fresh point left"):
        propose_candidates(problem, both_cells, n=1)


def test_a_space_with_room_left_is_still_answered() -> None:
    """The guard must refuse *zero* fresh points, not "cannot fill the batch".

    Borrowing `space_exhausted` here — the durable loop's signal to stop — would refuse an ask for
    three that could honestly answer with two, which is a worse answer than the partial one.
    """
    problem = OptimizationProblem(
        parameters=[CategoricalParameter(name="catalyst", categories=["Pd", "Ni", "Cu", "Fe"])],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    two_run = [
        Observation(params={"catalyst": "Pd"}, value=40.0),
        Observation(params={"catalyst": "Ni"}, value=55.0),
    ]
    proposed = propose_candidates(problem, two_run, n=1)
    assert len(proposed) == 1
    assert proposed[0].params["catalyst"] in {"Cu", "Fe"}

