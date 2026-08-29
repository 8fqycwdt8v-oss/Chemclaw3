"""The shape a prescriptive design is allowed to take, at the boundary where it is refused.

Every assertion here is about a validator that *rejects* something, because the models are what
stand between a model's JSON and the store: a `stated` slot with no quote, a continuous factor whose
level carries no number, two arms with one id. A model that accepted those would push the failure
into the checks, the renderer or Postgres, each of which reports it worse.

`design_id_for` is the fourth: an id derived from the ask rather than minted at random is what makes
a restructured request revise a design instead of forking one, so its stability is behaviour and not
an implementation detail.
"""

import pytest
from pydantic import ValidationError

from chemclaw.protocols.models import (
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    FactorLevel,
    ProtocolArm,
    ProtocolStep,
    ProtocolStepKind,
    RequestField,
    design_id_for,
)


def _request(**overrides: object) -> ExperimentRequest:
    """The smallest well-formed ask, with named slots overridden."""
    fields: dict[str, object] = {"title": "SM-3 Suzuki", "goal": "get the chloride to couple"}
    fields.update(overrides)
    return ExperimentRequest.model_validate(fields)


def _step(index: int, text: str) -> ProtocolStep:
    return ProtocolStep(index=index, kind=ProtocolStepKind.STIR, text=text)


def test_a_stated_slot_without_a_quote_is_refused() -> None:
    """`stated` is a claim about the chemist's words, so it cannot be made without them."""
    with pytest.raises(ValidationError, match="verbatim quote"):
        RequestField(value="24 wells", basis="stated")

    # And the same slot with the words in it is accepted — otherwise the refusal above would be
    # proving that `stated` is unusable rather than that it is checked.
    quoted = RequestField(value="24 wells", basis="stated", quote="24 wells")
    assert quoted.quote == "24 wells"


def test_a_stated_slot_whose_quote_is_only_whitespace_is_refused() -> None:
    """A quote of spaces is the same claim with nothing behind it."""
    with pytest.raises(ValidationError, match="verbatim quote"):
        RequestField(value="24 wells", basis="stated", quote="   \n ")


def test_an_absent_slot_carrying_a_value_is_refused() -> None:
    """`absent` means the text did not say; a value beside it is an unmarked inference."""
    with pytest.raises(ValidationError, match="no value"):
        RequestField(value="100 mg", basis="absent")

    assert RequestField(basis="absent").value == ""
    # Whitespace is not a value: the validator strips before it decides.
    assert RequestField(value="  ", basis="absent").value == "  "


def test_an_inferred_slot_needs_no_quote() -> None:
    """Inference is allowed here and marked — that is the whole rule (`protocols/README.md`)."""
    field = RequestField(value="100 mg", basis="inferred")
    assert field.quote == ""


def test_a_continuous_factor_whose_level_has_no_number_is_refused() -> None:
    """A continuous factor with a label-only level is a categorical factor mislabelled."""
    with pytest.raises(ValidationError, match="needs a numeric `value`"):
        Factor(
            name="temperature",
            kind="continuous",
            levels=[
                FactorLevel(label="cold", value=40.0),
                FactorLevel(label="hot"),
            ],
        )

    ok = Factor(
        name="temperature",
        kind="continuous",
        unit="C",
        levels=[FactorLevel(label="40", value=40.0), FactorLevel(label="80", value=80.0)],
    )
    assert [level.value for level in ok.levels] == [40.0, 80.0]


def test_a_categorical_factor_may_leave_its_levels_unnumbered() -> None:
    """The numeric obligation is the continuous kind's alone."""
    factor = Factor(
        name="ligand",
        kind="categorical",
        levels=[FactorLevel(label="XPhos"), FactorLevel(label="SPhos")],
    )
    assert [level.value for level in factor.levels] == [None, None]


def test_a_factor_that_repeats_a_level_label_is_refused() -> None:
    """Two levels with one label make an arm's `{factor: label}` ambiguous."""
    with pytest.raises(ValidationError, match="repeats a level label"):
        Factor(
            name="ligand",
            kind="categorical",
            levels=[
                FactorLevel(label="XPhos"),
                FactorLevel(label="XPhos", rationale="the second one"),
            ],
        )


def test_a_factor_needs_at_least_two_levels() -> None:
    """One level is a setpoint, not something the screen varies."""
    with pytest.raises(ValidationError):
        Factor(name="ligand", kind="categorical", levels=[FactorLevel(label="XPhos")])


def test_a_design_with_a_repeated_arm_id_is_refused() -> None:
    """`arm_id` is the key a well, a CSV row and a reported result all use."""
    with pytest.raises(ValidationError, match="arm_id repeats"):
        ExperimentDesign(
            request=_request(),
            arms=[ProtocolArm(arm_id="A1"), ProtocolArm(arm_id="A1")],
        )

    ok = ExperimentDesign(
        request=_request(), arms=[ProtocolArm(arm_id="A1"), ProtocolArm(arm_id="A2")]
    )
    assert [arm.arm_id for arm in ok.arms] == ["A1", "A2"]


def test_a_design_whose_steps_are_out_of_order_is_refused() -> None:
    """Steps are 1..n *in order*, so a reader can follow the list top to bottom."""
    with pytest.raises(ValidationError, match="numbered 1..n"):
        ExperimentDesign.model_validate(
            {
                "request": _request().model_dump(),
                "base": {
                    "steps": [_step(2, "second").model_dump(), _step(1, "first").model_dump()]
                },
            }
        )


def test_a_design_whose_steps_skip_a_number_is_refused() -> None:
    """A gap is the same defect as a swap: the indices no longer name the positions."""
    with pytest.raises(ValidationError, match="numbered 1..n"):
        ExperimentDesign.model_validate(
            {
                "request": _request().model_dump(),
                "base": {"steps": [_step(1, "first").model_dump(), _step(3, "third").model_dump()]},
            }
        )


def test_steps_numbered_in_order_are_accepted() -> None:
    """The passing direction, so the two refusals above are about order and not about steps."""
    design = ExperimentDesign.model_validate(
        {
            "request": _request().model_dump(),
            "base": {"steps": [_step(1, "first").model_dump(), _step(2, "second").model_dump()]},
        }
    )
    assert [step.index for step in design.base.steps] == [1, 2]


def test_arm_lookup_and_setpoint_fallback() -> None:
    """An arm without its own setpoints runs the shared body's — the whole point of one body."""
    design = ExperimentDesign(
        request=_request(),
        base={"setpoints": {"temperature_c": 80.0}},  # type: ignore[arg-type]
        arms=[
            ProtocolArm(arm_id="A1"),
            ProtocolArm(arm_id="A2", setpoints={"temperature_c": 100.0}),  # type: ignore[arg-type]
        ],
    )
    a1, a2 = design.arm("A1"), design.arm("A2")
    assert a1 is not None and a2 is not None
    assert design.setpoints_for(a1).temperature_c == 80.0
    assert design.setpoints_for(a2).temperature_c == 100.0
    assert design.arm("A9") is None


def test_design_id_is_stable_for_the_same_request() -> None:
    """The same ask restructured reaches the same design instead of forking one."""
    first = _request(reaction_smiles="CCO>>CCBr")
    second = _request(reaction_smiles="CCO>>CCBr")
    assert design_id_for(first) == design_id_for(second)
    assert design_id_for(first).startswith("design-")


def test_design_id_ignores_case_and_surrounding_whitespace_in_the_ask() -> None:
    """`design_id_for` normalises the two things a re-typed title differs by."""
    assert design_id_for(_request(title="  sm-3 SUZUKI  ")) == design_id_for(_request())


def test_design_id_differs_for_a_different_request() -> None:
    """Each identity slot moves the id, or two different asks would share a design."""
    base = _request()
    assert design_id_for(_request(goal="something else")) != design_id_for(base)
    assert design_id_for(_request(reaction_smiles="CCO>>CCBr")) != design_id_for(base)
    assert design_id_for(_request(mode="screen")) != design_id_for(base)


def test_design_id_differs_when_the_salt_differs() -> None:
    """`salt` is how a chemist deliberately opens a second design for one ask."""
    base = _request()
    assert design_id_for(base, salt="second") != design_id_for(base)
    assert design_id_for(base, salt="second") == design_id_for(base, salt="second")
    assert design_id_for(base, salt="third") != design_id_for(base, salt="second")


def test_a_design_refuses_a_field_it_does_not_declare() -> None:
    """`extra="forbid"` is what makes a misspelt key a refusal rather than silent data loss."""
    with pytest.raises(ValidationError):
        ExperimentDesign.model_validate({"request": _request().model_dump(), "armz": []})
