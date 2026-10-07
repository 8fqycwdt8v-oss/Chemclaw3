"""What an optimisation campaign's suggested points become as factors and arms.

Driven with real `OptimizationProblem`/`Candidate` shapes, since the module's value is consuming
what the optimiser returns. Assertions target mistakes a chemist would meet as a plate: a factor
holding two variables, a level label not matching its arm, an unmarked repeat, or a setpoint shown
as a varied factor.
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
    """A candidate becomes an arm whose levels the factors declare.

    `factor_levels_declared` compares labels by string equality (`80` vs `80.0` would fail), so one
    function formats both halves. Asserted as the set relation the check tests, not literals.
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
    """A categorical level carries the structure the campaign declared.

    `CategoricalParameter.structures` is the only SMILES on the BO side; without it
    `components_resolve` and `forbidden_absent` get a bare label.
    """
    translated = factors_and_arms(
        _problem(),
        [{"base": "K2CO3", "temperature": 80.0}, {"base": "NEt3", "temperature": 80.0}],
    )
    base = next(factor for factor in translated.factors if factor.name == "base")
    by_label = {level.label: level.smiles for level in base.levels}
    assert by_label == {"K2CO3": "[K+].[K+].[O-]C([O-])=O", "NEt3": "CCN(CC)CC"}


def test_a_parameter_the_runs_never_vary_is_a_setpoint_and_is_reported() -> None:
    """A parameter the runs never vary is a setpoint and is reported, not dropped.

    `Factor.levels` requires two levels, so it cannot be a factor.
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
    """A repeated run is a replicate rather than a second arm.

    `arms_are_distinct` warns on identical arms unless one declares itself a replicate.
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
    """Two parameters that slug to one factor name are refused.

    Merged, the design would pass every check while each arm holds whichever parameter was written
    last.
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
    """The units a BO problem does not carry are named rather than assumed.

    `OptimizationProblem` has no units and `quantities_are_plausible` reads setpoints, not factor
    levels. Asserted on the parameter's presence so the wording can change.
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
    """A translated design clears the factor and arm blockers, and only those.

    `factor_levels_declared` and `layout_fits` are cleared; `evidence_present` is still present,
    because a design assembled from campaign arithmetic has cited nothing.
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
