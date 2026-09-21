"""`protocols.rescale`: the charges move, the ratios do not, and the refusals are the deliverable.

The arithmetic here is four lines and is not what these tests are about. What a 1 g procedure taken
to 2 kg gets wrong is the quantities that do *not* follow the factor, so most of this file asserts
that they were named rather than scaled.
"""

import pytest

from chemclaw.protocols.models import (
    ChargeLine,
    ExperimentDesign,
    ExperimentRequest,
    ProtocolBody,
    ProtocolStep,
    ProtocolStepKind,
    Setpoints,
)
from chemclaw.protocols.rescale import RescaleError, rescale


def _design(**body: object) -> ExperimentDesign:
    """A one-gram Suzuki with one limiting line, overridable part by part."""
    fields: dict[str, object] = {
        "charge": [
            ChargeLine(component="ArBr", limiting=True, mass_mg=1000.0, equivalents=1.0),
            ChargeLine(component="boronic acid", mass_mg=1200.0, equivalents=1.2),
            ChargeLine(component="2-MeTHF", volume_ml=10.0),
        ]
    }
    fields.update(body)
    return ExperimentDesign(
        request=ExperimentRequest(title="SM-3 Suzuki", goal="couple the aryl bromide"),
        base=ProtocolBody.model_validate(fields),
    )


def test_every_charge_moves_by_one_factor_off_the_limiting_line() -> None:
    result = rescale(_design(), target="2 kg")
    assert result.factor == pytest.approx(2000.0)
    by_name = {line.component: line for line in result.design.base.charge}
    assert by_name["ArBr"].mass_mg == pytest.approx(2_000_000.0)
    assert by_name["boronic acid"].mass_mg == pytest.approx(2_400_000.0)
    assert by_name["2-MeTHF"].volume_ml == pytest.approx(20_000.0)


def test_equivalents_are_not_scaled_because_a_ratio_survives_a_change_of_scale() -> None:
    """Multiplying a ratio would change the chemistry rather than the batch size.

    This is the one field where the obvious loop over "every number on the line" is wrong, and it
    is wrong silently: a 1.2 equivalents that became 2400 still validates.
    """
    scaled = rescale(_design(), target="2 kg").design.base.charge
    by_name = {line.component: line for line in scaled}
    assert by_name["boronic acid"].equivalents == pytest.approx(1.2)
    assert by_name["ArBr"].equivalents == pytest.approx(1.0)


def test_the_original_design_is_not_mutated() -> None:
    """A rescale is a proposed revision, so the head it was computed from has to survive it."""
    design = _design()
    rescale(design, target="2 kg")
    assert design.base.charge[0].mass_mg == pytest.approx(1000.0)


def test_an_addition_and_a_filtration_are_named_rather_than_scaled() -> None:
    """The two steps whose duration is least linear in the charge, and most consequential.

    A dose time multiplied by 2000 is absurd and a dose time carried across unchanged is dangerous,
    so this module does neither: it says the number is untouched and why, and names the tools that
    decide it from measurements.
    """
    design = _design(
        steps=[
            ProtocolStep(index=1, kind=ProtocolStepKind("charge"), text="charge all"),
            ProtocolStep(
                index=2,
                kind=ProtocolStepKind("addition"),
                text="dose the acid chloride",
                duration_h=0.5,
            ),
            ProtocolStep(
                index=3, kind=ProtocolStepKind("purification"), text="filter", duration_h=0.3
            ),
        ]
    )
    result = rescale(design, target="2 kg")
    where = {caveat.where for caveat in result.caveats}
    assert {"step 2", "step 3"} <= where
    addition = next(c for c in result.caveats if c.where == "step 2")
    assert "heat removal" in addition.reason and "accumulate" in addition.reason
    assert result.design.base.steps[1].duration_h == pytest.approx(0.5)


def test_a_step_with_no_duration_earns_no_caveat() -> None:
    """A caveat per untouched *quantity*, not per step — an empty field is nothing to warn about."""
    design = _design(
        steps=[ProtocolStep(index=1, kind=ProtocolStepKind("addition"), text="dose it")]
    )
    assert all(caveat.where != "step 1" for caveat in rescale(design, target="2 kg").caveats)


def test_the_reaction_time_is_carried_across_and_said_to_be() -> None:
    design = _design()
    design = design.model_copy(
        update={"base": design.base.model_copy(update={"setpoints": Setpoints(time_h=16.0)})}
    )
    result = rescale(design, target="2 kg")
    assert result.design.base.setpoints.time_h == pytest.approx(16.0)
    assert any(caveat.where == "setpoints.time_h" for caveat in result.caveats)


def test_a_target_in_a_dimension_the_protocol_cannot_convert_is_refused() -> None:
    """Rather than assuming a molar mass, which is how a scaled protocol gains an invented number.

    The limiting line states milligrams; turning "5 mol" into milligrams needs a molar mass this
    design does not carry. The refusal names the unit to restate the target in, so the caller has
    somewhere to go.
    """
    with pytest.raises(RescaleError, match="molar mass or a density"):
        rescale(_design(), target="5 mol")


def test_a_target_that_is_not_a_quantity_is_refused_with_an_example() -> None:
    with pytest.raises(RescaleError, match="not a quantity"):
        rescale(_design(), target="kilo lab")


def test_two_limiting_lines_are_refused_rather_than_resolved_here() -> None:
    """`charge_is_consistent` already reports this; a second opinion would be a second answer."""
    design = _design(
        charge=[
            ChargeLine(component="a", limiting=True, mass_mg=1000.0),
            ChargeLine(component="b", limiting=True, mass_mg=1000.0),
        ]
    )
    with pytest.raises(RescaleError, match="exactly one limiting charge line"):
        rescale(design, target="2 kg")


def test_a_limiting_line_with_no_amount_is_refused() -> None:
    design = _design(charge=[ChargeLine(component="a", limiting=True, equivalents=1.0)])
    with pytest.raises(RescaleError, match="no mass, amount or volume"):
        rescale(design, target="2 kg")


def test_scaling_down_is_the_same_operation() -> None:
    """A kilo-lab procedure brought back to the bench for a repeat is the same arithmetic."""
    result = rescale(_design(), target="100 mg")
    assert result.factor == pytest.approx(0.1)
    assert result.design.base.charge[0].mass_mg == pytest.approx(100.0)
