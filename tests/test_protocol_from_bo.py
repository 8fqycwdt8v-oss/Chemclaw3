"""What an optimisation campaign's suggested points become as factors and arms.

Driven against real `OptimizationProblem` / `Candidate` shapes rather than fixtures shaped like
them, because the whole value of this module is that it consumes what the optimiser actually
returns — a translation tested against a hand-written dict would agree with the test's idea of a
candidate forever.

Every assertion here is about a mistake a chemist would meet as a plate: a factor holding two
variables, a level label that does not match the arm that cites it, a repeat nobody marked, or a
setpoint quietly presented as something the screen varies.
"""

import pytest

from chemclaw.protocols.checks import blockers, run_checks
from chemclaw.protocols.from_bo import BoTranslationError, factors_and_arms
from chemclaw.protocols.models import (
    ExperimentDesign,
    ExperimentRequest,
    ProtocolBody,
    ProtocolStep,
    ProtocolStepKind,
    RequestField,
)
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    OptimizationProblem,
)


def _problem() -> OptimizationProblem:
    """A two-parameter campaign whose categorical options are real species."""
    return OptimizationProblem(
        parameters=[
            CategoricalParameter(
                name="base",
                categories=["K2CO3", "Cs2CO3", "NEt3"],
                structures={
                    "K2CO3": "[K+].[K+].[O-]C([O-])=O",
                    "Cs2CO3": "[Cs+].[Cs+].[O-]C([O-])=O",
                    "NEt3": "CCN(CC)CC",
                },
            ),
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def test_a_candidate_becomes_an_arm_whose_levels_the_factors_declare() -> None:
    """The blocker `factor_levels_declared` enforces, produced correctly by construction.

    That check is the reason one function formats both halves: it matches an arm's level against
    the factor's labels by **string equality**, so a float written `80` in the factor and `80.0` in
    the arm fails a design that is in fact correct. Asserted as the set relation the check tests
    rather than against literals, so a change to the formatting fails here only if it breaks the
    agreement — which is the property, not the spelling.
    """
    translated = factors_and_arms(
        _problem(),
        [
            {"base": "K2CO3", "temperature": 80.0},
            {"base": "Cs2CO3", "temperature": 100.0},
            {"base": "NEt3", "temperature": 80.0},
        ],
    )

    declared = {
        factor.name: {level.label for level in factor.levels} for factor in translated.factors
    }
    assert declared.keys() == {"base", "temperature"}
    for arm in translated.arms:
        assert arm.levels.keys() == declared.keys(), (
            f"{arm.arm_id} does not set every declared factor, which `factor_levels_declared` "
            "refuses as a blocker"
        )
        for name, label in arm.levels.items():
            assert label in declared[name], (
                f"{arm.arm_id} cites level {label!r} of factor {name!r}, which the factor does not "
                "declare — the two halves were formatted by different code"
            )


def test_a_categorical_level_carries_the_structure_the_campaign_declared() -> None:
    """A level that is a species reaches the hazard screen and the precedent questions as one.

    `CategoricalParameter.structures` is the only place a SMILES exists on the BO side, so a
    translation that dropped it would hand `components_resolve` and `forbidden_absent` a bare label
    — and a base nobody can parse is a base nobody can screen.
    """
    translated = factors_and_arms(
        _problem(),
        [{"base": "K2CO3", "temperature": 80.0}, {"base": "NEt3", "temperature": 80.0}],
    )
    base = next(factor for factor in translated.factors if factor.name == "base")
    by_label = {level.label: level.smiles for level in base.levels}
    assert by_label == {"K2CO3": "[K+].[K+].[O-]C([O-])=O", "NEt3": "CCN(CC)CC"}


def test_a_parameter_the_runs_never_vary_is_a_setpoint_and_is_reported() -> None:
    """One value across every run is not a factor, and silently dropping it loses a condition.

    `Factor.levels` has `min_length=2`, so such a parameter cannot be expressed as a factor at all
    — which makes the decision between *reporting* it and *dropping* it the only one available, and
    dropping it would take a real setpoint out of the design with nothing saying so.
    """
    translated = factors_and_arms(
        _problem(),
        [{"base": "K2CO3", "temperature": 80.0}, {"base": "K2CO3", "temperature": 100.0}],
    )
    assert [factor.name for factor in translated.factors] == ["temperature"]
    assert translated.constants == {"base": "K2CO3"}
    assert any("setpoints rather than" in note for note in translated.notes)
    assert all("base" not in arm.levels for arm in translated.arms)


def test_a_repeated_run_is_a_replicate_rather_than_a_second_arm() -> None:
    """A centre point run twice is one experiment repeated, and the design has to say so.

    `arms_are_distinct` warns about two arms at identical settings unless one declares itself a
    replicate. Marking it here is the point of the module: the model reading a run table cannot see
    that run 3 repeats run 1 without comparing every column by eye.
    """
    translated = factors_and_arms(
        _problem(),
        [
            {"base": "K2CO3", "temperature": 80.0},
            {"base": "NEt3", "temperature": 120.0},
            {"base": "K2CO3", "temperature": 80.0},
        ],
    )
    by_id = {arm.arm_id: arm for arm in translated.arms}
    assert by_id["arm1"].replicate_of == ""
    assert by_id["arm2"].replicate_of == ""
    assert by_id["arm3"].replicate_of == "arm1", (
        "the repeated run was emitted as an independent arm, so the design claims three distinct "
        "experiments where the campaign asked for two and a replicate"
    )
    assert by_id["arm3"].levels == by_id["arm1"].levels


def test_two_parameters_that_slug_to_one_factor_name_are_refused() -> None:
    """The failure nothing downstream could catch, because by then there is only one factor.

    Merging them produces a design that is internally consistent and passes every check while
    describing experiments nobody planned: each arm would carry one factor column holding whichever
    of the two parameters was written last.
    """
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="Pd source", lower=0.0, upper=1.0),
            ContinuousParameter(name="pd_source", lower=0.0, upper=1.0),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    with pytest.raises(BoTranslationError, match="both become the factor name"):
        factors_and_arms(
            problem,
            [
                {"Pd source": 0.1, "pd_source": 0.2},
                {"Pd source": 0.3, "pd_source": 0.4},
            ],
        )


def test_runs_from_another_campaign_are_refused_in_both_directions() -> None:
    """An extra key is ignored by every dict read; a missing one fails a blocker much later.

    Both are the same mistake — runs and problem from different campaigns — and only one of them
    would ever surface, against a design the model had already written a protocol body for.
    """
    problem = _problem()
    with pytest.raises(BoTranslationError, match="does not declare"):
        factors_and_arms(
            problem,
            [
                {"base": "K2CO3", "temperature": 80.0, "ligand": "XPhos"},
                {"base": "NEt3", "temperature": 90.0, "ligand": "SPhos"},
            ],
        )
    with pytest.raises(BoTranslationError, match="it omits"):
        factors_and_arms(problem, [{"base": "K2CO3"}, {"base": "NEt3"}])


def test_a_parameter_over_more_levels_than_a_factor_may_declare_names_itself() -> None:
    """96 is `Factor.levels`' ceiling, and the model must be told which parameter broke it.

    Without this the failure is a pydantic error on a field the model never wrote, in a list it did
    not build, naming neither the parameter nor the count.
    """
    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name="temperature", lower=0.0, upper=200.0)],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    with pytest.raises(BoTranslationError, match="temperature.*97 distinct"):
        factors_and_arms(problem, [{"temperature": float(step)} for step in range(97)])


def test_the_units_a_bo_problem_does_not_carry_are_named_rather_than_assumed() -> None:
    """The one gap this translation cannot close, said out loud instead of left blank.

    An `OptimizationProblem` has no units anywhere, and `quantities_are_plausible` reads the
    *setpoints* rather than a factor's levels — so nothing downstream catches a temperature factor
    whose 80 might be °C or mol%. Asserted against the continuous parameter's presence rather than
    against the wording, so the sentence can be improved.
    """
    translated = factors_and_arms(
        _problem(),
        [{"base": "K2CO3", "temperature": 80.0}, {"base": "NEt3", "temperature": 120.0}],
    )
    temperature = next(f for f in translated.factors if f.name == "temperature")
    assert temperature.unit == ""
    assert all(level.unit == "" for level in temperature.levels)
    assert any("no units" in note and "temperature" in note for note in translated.notes), (
        f"a factor came back unitless with nothing saying so: {translated.notes}"
    )


def test_a_translated_design_clears_the_factor_and_arm_blockers() -> None:
    """End to end: the two collections really are what `draft_experiment_protocol` needs.

    The point is *which* blockers this is allowed to clear. `factor_levels_declared` and
    `layout_fits` are the ones the translation is responsible for, and they are asserted absent.
    `evidence_present` is asserted **present**, because a design assembled from a campaign's
    arithmetic has cited nothing and must not look as though it had — the module supplies arms, not
    grounds.
    """
    translated = factors_and_arms(
        _problem(),
        [
            {"base": "K2CO3", "temperature": 80.0},
            {"base": "Cs2CO3", "temperature": 80.0},
            {"base": "NEt3", "temperature": 120.0},
        ],
    )
    design = ExperimentDesign(
        request=ExperimentRequest(
            title="Base and temperature screen",
            goal="Find the base and temperature that maximise yield",
            mode="screen",
            scale=RequestField(value="", basis="absent", quote=""),
            plate_format=RequestField(value="", basis="absent", quote=""),
            max_runs=RequestField(value="", basis="absent", quote=""),
            deadline=RequestField(value="", basis="absent", quote=""),
        ),
        base=ProtocolBody(
            steps=[ProtocolStep(index=1, kind=ProtocolStepKind.CHARGE, text="Charge the base.")]
        ),
        factors=translated.factors,
        arms=translated.arms,
    )

    failing = {check.check_id for check in blockers(run_checks(design))}
    assert "factor_levels_declared" not in failing, (
        "the arms do not set the factors the translation declared, which is the one blocker this "
        "module exists to make impossible"
    )
    assert "layout_fits" not in failing
    assert "evidence_present" in failing, (
        "a design built from a campaign's arithmetic cites nothing; if this passes, the blocker "
        "that makes a chemist's citation mandatory has stopped working"
    )
