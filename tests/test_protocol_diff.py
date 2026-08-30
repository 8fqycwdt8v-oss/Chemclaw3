"""What changed between two revisions — the product, not a debugging aid.

The claim worth proving is the `_KEYED_LISTS` one: a list of arms reordered is **not** fourteen
changes, and flattening by index would say it was. The complement matters just as much and is
easy to lose in a refactor — `base.steps` is *not* keyed, because position is identity in a
procedure, so a swapped pair of instructions has to read as a change.

If the first half broke, a chemist's plate-map reshuffle would drown every real edit they made; if
the second broke, reordering a procedure would be invisible to the miner that reads these paths.
"""

from typing import Any

from chemclaw.protocols.diff import diff_designs, flatten
from chemclaw.protocols.models import (
    Analytic,
    ChargeLine,
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    FactorLevel,
    ProtocolArm,
    ProtocolStep,
    ProtocolStepKind,
    Setpoints,
)


def _request(**overrides: object) -> ExperimentRequest:
    fields: dict[str, object] = {"title": "SM-3 Suzuki", "goal": "couple the aryl chloride"}
    fields.update(overrides)
    return ExperimentRequest.model_validate(fields)


def _design(**overrides: object) -> ExperimentDesign:
    fields: dict[str, object] = {"request": _request()}
    fields.update(overrides)
    return ExperimentDesign.model_validate(fields)


def _steps(*texts: str) -> list[dict[str, Any]]:
    """A procedure numbered 1..n, since the model refuses anything else."""
    return [
        ProtocolStep(index=index, kind=ProtocolStepKind.STIR, text=text).model_dump()
        for index, text in enumerate(texts, start=1)
    ]


def _arm(arm_id: str, **overrides: object) -> ProtocolArm:
    fields: dict[str, object] = {"arm_id": arm_id}
    fields.update(overrides)
    return ProtocolArm.model_validate(fields)


def test_a_changed_scalar_is_one_dotted_path_carrying_both_values() -> None:
    before = _design(base={"setpoints": Setpoints(temperature_c=80.0).model_dump()})
    after = _design(base={"setpoints": Setpoints(temperature_c=60.0).model_dump()})

    changes = diff_designs(before, after, from_revision=2, to_revision=3)
    assert changes.paths == ["base.setpoints.temperature_c"]
    assert changes.changes[0].kind == "changed"
    assert (changes.changes[0].before, changes.changes[0].after) == ("80.0", "60.0")
    assert (changes.from_revision, changes.to_revision) == (2, 3)


def test_nested_paths_are_dotted_all_the_way_down() -> None:
    """The form both consumers want.

    A UI marker beside one field, and a miner asking how often `base.setpoints.temperature_c` moves.
    """
    before = _design(
        base={"setpoints": Setpoints(solvent="toluene", concentration_molar=0.2).model_dump()}
    )
    after = _design(
        base={"setpoints": Setpoints(solvent="THF", concentration_molar=0.1).model_dump()}
    )
    assert diff_designs(before, after).paths == [
        "base.setpoints.concentration_molar",
        "base.setpoints.solvent",
    ]


def test_an_added_path_is_reported_as_added_with_no_before_value() -> None:
    before = _design(arms=[_arm("A1")])
    after = _design(arms=[_arm("A1"), _arm("A2", note="the new one")])

    changes = diff_designs(before, after)
    added = {change.path: change for change in changes.changes}
    assert all(change.kind == "added" for change in changes.changes)
    assert added["arms.A2.arm_id"].after == "A2"
    assert added["arms.A2.note"].after == "the new one"
    assert added["arms.A2.arm_id"].before == ""


def test_a_removed_path_is_reported_as_removed_with_no_after_value() -> None:
    before = _design(arms=[_arm("A1"), _arm("A2")])
    after = _design(arms=[_arm("A1")])

    changes = diff_designs(before, after)
    assert all(change.kind == "removed" for change in changes.changes)
    removed = {change.path: change for change in changes.changes}
    assert removed["arms.A2.arm_id"].before == "A2"
    assert removed["arms.A2.arm_id"].after == ""


def test_reordering_the_arms_produces_no_changes_at_all() -> None:
    """The `_KEYED_LISTS` contract.

    A chemist reshuffling a plate map must not bury their one real edit under a change for every
    field of every arm.
    """
    arms = [
        _arm("A1", levels={"ligand": "XPhos"}),
        _arm("A2", levels={"ligand": "SPhos"}),
        _arm("A3", levels={"ligand": "RuPhos"}),
    ]
    before = _design(arms=arms)
    after = _design(arms=[arms[2], arms[0], arms[1]])

    assert diff_designs(before, after).changes == []


def test_reordering_a_keyed_list_still_shows_the_one_field_that_moved() -> None:
    """Reordering is invisible and an edit inside a reordered list is not.

    Otherwise the contract above would be indistinguishable from ignoring the list.
    """
    before = _design(arms=[_arm("A1", note="first"), _arm("A2", note="second")])
    after = _design(arms=[_arm("A2", note="second"), _arm("A1", note="changed")])

    changes = diff_designs(before, after)
    assert changes.paths == ["arms.A1.note"]
    assert changes.changes[0].after == "changed"


def test_every_keyed_list_is_keyed_by_its_own_identifier() -> None:
    """`arms`, `factors`, `base.charge`, `base.analytics` and `evidence` all reorder for free."""
    factors = [
        Factor(
            name="ligand",
            kind="categorical",
            levels=[FactorLevel(label="XPhos"), FactorLevel(label="SPhos")],
        ),
        Factor(
            name="base",
            kind="categorical",
            levels=[FactorLevel(label="K3PO4"), FactorLevel(label="Cs2CO3")],
        ),
    ]
    charge = [
        ChargeLine(component="aryl chloride", limiting=True).model_dump(),
        ChargeLine(component="boronic acid").model_dump(),
    ]
    analytics = [Analytic(name="HPLC").model_dump(), Analytic(name="LCMS").model_dump()]
    evidence = [
        EvidenceRef(kind="precedent", summary="a run like this gave 72%"),
        EvidenceRef(kind="tool", tool="predict_pka", summary="the base is strong enough"),
    ]
    before = _design(
        factors=factors,
        base={"charge": charge, "analytics": analytics},
        evidence=evidence,
    )
    after = _design(
        factors=[factors[1], factors[0]],
        base={"charge": [charge[1], charge[0]], "analytics": [analytics[1], analytics[0]]},
        evidence=[evidence[1], evidence[0]],
    )
    assert diff_designs(before, after).changes == []


def test_reordering_the_procedure_does_produce_changes() -> None:
    """Position **is** identity in a procedure.

    `base.steps.0.text` is the first instruction and has to stay the first instruction, so
    `base.steps` is deliberately not in `_KEYED_LISTS`.
    """
    before = _design(base={"steps": _steps("charge the vessel", "stir at 80 C")})
    after = _design(base={"steps": _steps("stir at 80 C", "charge the vessel")})

    changes = diff_designs(before, after)
    assert changes.paths == ["base.steps.0.text", "base.steps.1.text"]
    assert changes.changes[0].before == "charge the vessel"
    assert changes.changes[0].after == "stir at 80 C"


def test_an_unkeyed_list_of_scalars_is_indexed_by_position() -> None:
    """`base.hazards` is prose in the chemist's words, and its order is what they wrote."""
    before = _design(base={"hazards": ["peroxide former", "lachrymator"]})
    after = _design(base={"hazards": ["lachrymator", "peroxide former"]})
    assert diff_designs(before, after).paths == ["base.hazards.0", "base.hazards.1"]


def test_flatten_keys_a_keyed_list_by_the_member_identifier_and_not_by_index() -> None:
    """The mechanism the reorder contract rests on, asserted directly."""
    flat = flatten(_design(arms=[_arm("A1"), _arm("A2")]).model_dump(mode="json"))
    assert "arms.A1.arm_id" in flat and "arms.A2.arm_id" in flat
    assert not any(path.startswith("arms.0") for path in flat)
    # A stated field has a path; an unstated one has none. `None` used to be stored as a leaf,
    # which is what inverted `added`/`removed` for every optional sub-model — adding a plate
    # layout was reported as `layout removed`.
    assert "base.setpoints.solvent" in flat
    assert "base.setpoints.temperature_c" not in flat


def test_an_identical_design_diffs_to_nothing() -> None:
    """Empty when nothing moved, or every revision would look edited."""
    design = _design(arms=[_arm("A1")], base={"steps": _steps("charge the vessel")})
    assert diff_designs(design, design).changes == []
    assert diff_designs(design, design).paths == []


def test_none_and_the_empty_string_both_render_as_nothing() -> None:
    """Deliberate: a UI showing `None` beside a cleared field would read as a stored value."""
    before = _design(
        base={"setpoints": Setpoints(temperature_c=80.0, atmosphere="N2").model_dump()}
    )
    after = _design(base={"setpoints": Setpoints().model_dump()})

    rendered = {
        change.path: (change.before, change.after) for change in diff_designs(before, after).changes
    }
    assert rendered["base.setpoints.temperature_c"] == ("80.0", "")
    assert rendered["base.setpoints.atmosphere"] == ("N2", "")


def test_a_boolean_renders_as_a_word_rather_than_a_python_repr() -> None:
    before = _design(base={"charge": [ChargeLine(component="a", limiting=False).model_dump()]})
    after = _design(base={"charge": [ChargeLine(component="a", limiting=True).model_dump()]})
    change = diff_designs(before, after).changes[0]
    assert (change.path, change.before, change.after) == ("base.charge.a.limiting", "false", "true")
