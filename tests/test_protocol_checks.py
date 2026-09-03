"""Every deterministic verdict `checks.py` produces, each proven in both directions.

**A check that can only pass is not a check**, so every one below is exercised against a design it
lets through *and* a design it stops. That is the property the whole module is for: `checks.py`
exists rather than a paragraph in a `SKILL.md` precisely because a prompt asking for evidence can be
ignored on the turn where the model already has an answer it likes.

`coverage_is_stated` is the one exception and it is asserted as such: it is a `note` that never
fails, so what is proven there is that both of its branches are reached and that neither is a
refusal — an assertion that fails whoever quietly promotes it to a blocker.
"""

import pytest
from pydantic import ValidationError

from chemclaw.protocols.checks import (
    _AGREEMENT_FRACTION,
    arms_are_distinct,
    atom_balance,
    blockers,
    charge_is_consistent,
    check_ids,
    components_resolve,
    controls_present,
    coverage_is_stated,
    evidence_present,
    factor_levels_declared,
    forbidden_absent,
    hazard_screen_ran,
    is_a_protocol,
    layout_fits,
    objectives_are_measured,
    quantities_are_plausible,
    run_checks,
)
from chemclaw.protocols.models import (
    Analytic,
    ChargeLine,
    CheckStage,
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    FactorLevel,
    PlateLayout,
    ProtocolArm,
    ProtocolCheck,
    ProtocolStep,
    ProtocolStepKind,
    RequestedComponent,
    Setpoints,
    Well,
)
from chemclaw.science.labels.vocabulary import SpeciesRole

# DMF written the two ways a chemist and a cheminformatics tool write it. Both canonicalise to the
# same string, which is what `forbidden_absent` has to see through.
_DMF = "CN(C)C=O"
_DMF_REVERSED = "O=CN(C)C"


def _request(**overrides: object) -> ExperimentRequest:
    """The smallest well-formed ask, with named slots overridden."""
    fields: dict[str, object] = {"title": "SM-3 Suzuki", "goal": "couple the aryl chloride"}
    fields.update(overrides)
    return ExperimentRequest.model_validate(fields)


def _design(**overrides: object) -> ExperimentDesign:
    """A design over the smallest ask, with named parts overridden."""
    fields: dict[str, object] = {"request": _request()}
    fields.update(overrides)
    return ExperimentDesign.model_validate(fields)


def _protocol(**overrides: object) -> ExperimentDesign:
    """A design that clears every blocker: one arm, and both kinds of citation."""
    fields: dict[str, object] = {"arms": [ProtocolArm(arm_id="A1")], "evidence": _cited()}
    fields.update(overrides)
    return _design(**fields)


def _cited() -> list[EvidenceRef]:
    """Evidence that satisfies `evidence_present` — one precedent and one tool."""
    return [
        EvidenceRef(kind="precedent", ref="reaction-1", summary="a run like this gave 72%"),
        EvidenceRef(kind="tool", tool="predict_pka", summary="the base is strong enough"),
    ]


def _well(label: str, row: int, column: int, arm_id: str, run_order: int) -> Well:
    return Well(label=label, row=row, column=column, arm_id=arm_id, run_order=run_order)


# --- components_resolve -------------------------------------------------------------------------


def test_components_resolve_passes_when_every_structure_parses() -> None:
    design = _design(
        request=_request(components=[RequestedComponent(name_as_written="ethanol", smiles="CCO")]),
        base={"charge": [ChargeLine(component="ethanol", smiles="CCO").model_dump()]},
    )
    verdict = components_resolve(design)
    assert verdict.passed and verdict.severity == "blocker"


def test_components_resolve_blocks_a_structure_rdkit_cannot_read() -> None:
    """The blocker: a structure nobody can read makes every downstream answer about nothing."""
    design = _design(
        request=_request(
            components=[RequestedComponent(name_as_written="mystery", smiles="not-a-smiles")]
        )
    )
    verdict = components_resolve(design)
    assert not verdict.passed and verdict.severity == "blocker"
    assert "not-a-smiles" in verdict.detail


def test_components_resolve_reports_an_unresolved_name_as_a_failed_warning() -> None:
    """A name with no structure is a finding a chemist fixes in one word, not a refusal.

    **Failed, not passing, and the old name of this test was the defect.** `render_markdown` and
    `summarise` both list only failed checks, so a passing warning put "checked and fine" in front
    of a reader about a species nobody resolved — the sentence never reached the page.
    `_unreadable`'s docstring describes that exact failure as fixed; it was live in four other
    branches of this file.
    """
    design = _design(
        request=_request(components=[RequestedComponent(name_as_written="the new ligand")])
    )
    verdict = components_resolve(design)
    assert not verdict.passed and verdict.severity == "warning"
    assert "the new ligand" in verdict.detail


def test_components_resolve_reads_factor_levels_too() -> None:
    """A screen's structures live on its levels, which is where an unreadable one hides."""
    design = _design(
        factors=[
            Factor(
                name="ligand",
                kind="categorical",
                levels=[
                    FactorLevel(label="good", smiles="CCO"),
                    FactorLevel(label="bad", smiles="Q"),
                ],
            )
        ]
    )
    verdict = components_resolve(design)
    assert not verdict.passed
    assert "ligand/bad" in verdict.detail


# --- charge_is_consistent -----------------------------------------------------------------------


def test_charge_is_consistent_passes_a_table_whose_equivalents_agree() -> None:
    design = _design(
        base={
            "charge": [
                ChargeLine(
                    component="aryl chloride", limiting=True, equivalents=1.0, amount_mmol=1.0
                ).model_dump(),
                ChargeLine(component="boronic acid", equivalents=1.2, amount_mmol=1.2).model_dump(),
            ]
        }
    )
    verdict = charge_is_consistent(design)
    assert verdict.passed and verdict.severity == "blocker"


#: `(label, limiting mmol, the line's equivalents, the mmol a chemist writes, does it pass)`.
#: Four scales and five loadings, each verdict the one a chemist would give reading the table. This
#: is the sweep `_agreement_tolerance` cites, and it exists because that function's previous rule
#: was argued from one worked example whose arithmetic was wrong.
_AGREEMENT_SWEEP: list[tuple[str, float, float, float, bool]] = [
    ("catalyst 5 mol% of 1.37 mmol, two decimals", 1.37, 0.05, 0.07, True),
    ("the same catalyst with one digit too many", 1.37, 0.05, 0.10, False),
    ("the same catalyst a factor of ten out", 1.37, 0.05, 0.685, False),
    ("ligand 6 mol% of 1.37 mmol, two decimals", 1.37, 0.06, 0.08, True),
    ("2.0 equivalents of base, exact", 1.37, 2.0, 2.74, True),
    ("2.0 equivalents of base, one decimal", 1.37, 2.0, 2.7, True),
    ("2.0 equivalents written as one equivalent's worth", 1.37, 2.0, 1.37, False),
    ("1.2 equivalents at a 10 mmol scale", 10.0, 1.2, 12.0, True),
    ("the same line 20% out", 10.0, 1.2, 14.4, False),
    ("0.5 mol% Pd at a 20 mmol scale", 20.0, 0.005, 0.1, True),
    ("the same Pd line written ten times over", 20.0, 0.005, 1.0, False),
    ("a trace charge rounded to three decimals", 0.16, 0.005, 0.001, True),
    ("two decimals where two decimals is 20% of the line", 0.25, 0.05, 0.01, False),
    ("the same line written to three decimals", 0.25, 0.05, 0.013, True),
    ("the limiting reagent against itself", 1.37, 1.0, 1.37, True),
]


def test_the_agreement_tolerance_over_the_scales_a_bench_uses() -> None:
    """Every row of the sweep gets the verdict a chemist would give.

    **Row two is the one this test was written for.** `_agreement_tolerance` used to read the
    written precision back out of the figure with `Decimal(repr(…))`, and a float carries no
    trailing zero — so a catalyst written `0.10` against an implied `0.0685` got half a unit in the
    *first* decimal as slack, and a 46% error passed a blocker. The function's own docstring
    asserted that same line fails by six times the slack. One worked example is not a sweep, which
    is why this is one.
    """
    wrong = []
    for label, reference_mmol, equivalents, written_mmol, expected in _AGREEMENT_SWEEP:
        design = _design(
            base={
                "charge": [
                    ChargeLine(
                        component="limiting",
                        limiting=True,
                        equivalents=1.0,
                        amount_mmol=reference_mmol,
                    ).model_dump(),
                    ChargeLine(
                        component="line",
                        equivalents=equivalents,
                        amount_mmol=written_mmol,
                    ).model_dump(),
                ]
            }
        )
        verdict = charge_is_consistent(design)
        if verdict.passed is not expected:
            wrong.append(
                f"{label}: {equivalents} eq of {reference_mmol} mmol implies "
                f"{equivalents * reference_mmol:.4g}, table says {written_mmol:.4g} — "
                f"passed={verdict.passed}, expected {expected}"
            )
    assert not wrong, "\n".join(wrong)


def test_charge_is_consistent_blocks_a_table_with_no_limiting_reagent() -> None:
    design = _design(
        base={"charge": [ChargeLine(component="aryl chloride", equivalents=1.0).model_dump()]}
    )
    verdict = charge_is_consistent(design)
    assert not verdict.passed and verdict.severity == "blocker"
    assert "0 charge lines are marked limiting" in verdict.detail


def test_charge_is_consistent_blocks_a_table_with_two_limiting_reagents() -> None:
    """Two references make every equivalents figure in the table ambiguous."""
    design = _design(
        base={
            "charge": [
                ChargeLine(component="a", limiting=True).model_dump(),
                ChargeLine(component="b", limiting=True).model_dump(),
            ]
        }
    )
    verdict = charge_is_consistent(design)
    assert not verdict.passed
    assert "2 charge lines are marked limiting" in verdict.detail


def test_charge_is_consistent_blocks_a_limiting_reagent_that_is_not_one_equivalent() -> None:
    design = _design(
        base={
            "charge": [
                ChargeLine(component="aryl chloride", limiting=True, equivalents=1.5).model_dump()
            ]
        }
    )
    verdict = charge_is_consistent(design)
    assert not verdict.passed
    assert "by definition it is 1.0" in verdict.detail


def test_charge_is_consistent_blocks_equivalents_that_disagree_with_the_amounts() -> None:
    """Two statements of one fact; a table where they disagree gets weighed out wrong."""
    design = _design(
        base={
            "charge": [
                ChargeLine(
                    component="aryl chloride", limiting=True, equivalents=1.0, amount_mmol=1.0
                ).model_dump(),
                ChargeLine(component="boronic acid", equivalents=2.0, amount_mmol=1.5).model_dump(),
            ]
        }
    )
    verdict = charge_is_consistent(design)
    assert not verdict.passed
    assert f"disagree by more than {_AGREEMENT_FRACTION:.0%}" in verdict.detail
    assert "boronic acid" in verdict.detail


def test_charge_is_consistent_tolerates_a_rounded_amount_inside_two_percent() -> None:
    """The band exists because a real charge table is rounded to weighable numbers."""
    design = _design(
        base={
            "charge": [
                ChargeLine(
                    component="aryl chloride", limiting=True, equivalents=1.0, amount_mmol=1.0
                ).model_dump(),
                ChargeLine(
                    component="boronic acid", equivalents=2.0, amount_mmol=2.01
                ).model_dump(),
            ]
        }
    )
    assert charge_is_consistent(design).passed


def test_charge_is_consistent_warns_when_the_limiting_reagent_has_no_amount() -> None:
    """No amount means no equivalents can become a weight — a warning, not a refusal.

    A **failed** warning: this branch returns before the disagreement scan, so a table whose lines
    contradict each other is reported as checked-and-fine, and a passing verdict is one no
    rendering path shows.
    """
    verdict = charge_is_consistent(
        _design(base={"charge": [ChargeLine(component="a", limiting=True).model_dump()]})
    )
    assert not verdict.passed and verdict.severity == "warning"


def test_charge_is_consistent_is_a_warning_when_there_is_no_charge_table_at_all() -> None:
    verdict = charge_is_consistent(_design())
    assert verdict.passed and verdict.severity == "warning"


# --- atom_balance -------------------------------------------------------------------------------


def test_atom_balance_passes_when_every_product_element_is_supplied() -> None:
    design = _design(request=_request(reaction_smiles="CCO.CC(=O)O>>CCOC(C)=O"))
    verdict = atom_balance(design)
    assert verdict.passed and verdict.severity == "warning"


def test_atom_balance_fails_when_the_product_holds_an_element_nothing_charged_supplies() -> None:
    """Either a species is missing from the charge table or the product is wrong."""
    verdict = atom_balance(_design(request=_request(reaction_smiles="CCO>>CCBr")))
    assert not verdict.passed and verdict.severity == "warning"
    assert "Br" in verdict.detail


def test_atom_balance_reads_the_charge_table_as_a_supplier() -> None:
    """A reagent the reaction SMILES omits is still charged, so it still supplies its elements."""
    design = _design(
        request=_request(reaction_smiles="CCO>>CCBr"),
        base={"charge": [ChargeLine(component="HBr", smiles="Br").model_dump()]},
    )
    assert atom_balance(design).passed


def test_atom_balance_is_a_pass_when_there_is_no_reaction_to_balance() -> None:
    assert atom_balance(_design()).passed


# --- factor_levels_declared ---------------------------------------------------------------------


def _two_factors() -> list[Factor]:
    return [
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


def test_factor_levels_declared_passes_when_every_arm_sets_every_factor() -> None:
    design = _design(
        factors=_two_factors(),
        arms=[ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4"})],
    )
    verdict = factor_levels_declared(design)
    assert verdict.passed and verdict.severity == "blocker"


def test_factor_levels_declared_blocks_an_arm_setting_a_factor_nothing_declares() -> None:
    design = _design(
        factors=_two_factors(),
        arms=[
            ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4", "solvent": "THF"})
        ],
    )
    verdict = factor_levels_declared(design)
    assert not verdict.passed and verdict.severity == "blocker"
    assert "undeclared factor(s): solvent" in verdict.detail


def test_factor_levels_declared_blocks_an_arm_setting_a_level_the_factor_does_not_declare() -> None:
    design = _design(
        factors=_two_factors(),
        arms=[ProtocolArm(arm_id="A1", levels={"ligand": "RuPhos", "base": "K3PO4"})],
    )
    verdict = factor_levels_declared(design)
    assert not verdict.passed
    assert "not a declared level" in verdict.detail


def test_factor_levels_declared_blocks_an_arm_that_leaves_a_factor_unset() -> None:
    """An unset factor is an arm nobody can reproduce: the level was decided at the bench."""
    design = _design(
        factors=_two_factors(), arms=[ProtocolArm(arm_id="A1", levels={"ligand": "XPhos"})]
    )
    verdict = factor_levels_declared(design)
    assert not verdict.passed
    assert "does not set: base" in verdict.detail


def test_factor_levels_declared_exempts_a_control_arm() -> None:
    """A control is deliberately outside the factor space, which is why it is a control."""
    design = _design(
        factors=_two_factors(),
        arms=[
            ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4"}),
            ProtocolArm(arm_id="C1", control="negative"),
        ],
    )
    assert factor_levels_declared(design).passed


# --- arms_are_distinct --------------------------------------------------------------------------


def test_arms_are_distinct_passes_when_no_two_arms_share_conditions() -> None:
    design = _design(
        factors=_two_factors(),
        arms=[
            ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4"}),
            ProtocolArm(arm_id="A2", levels={"ligand": "SPhos", "base": "K3PO4"}),
        ],
    )
    verdict = arms_are_distinct(design)
    assert verdict.passed and verdict.severity == "warning"


def test_arms_are_distinct_warns_about_an_unmarked_duplicate() -> None:
    design = _design(
        factors=_two_factors(),
        arms=[
            ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4"}),
            ProtocolArm(arm_id="A2", levels={"ligand": "XPhos", "base": "K3PO4"}),
        ],
    )
    verdict = arms_are_distinct(design)
    assert not verdict.passed and verdict.severity == "warning"
    assert "A2 repeats A1" in verdict.detail


def test_arms_are_distinct_exempts_a_marked_replicate() -> None:
    """`replicate_of` is exactly how an intended repeat says it is one."""
    design = _design(
        factors=_two_factors(),
        arms=[
            ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4"}),
            ProtocolArm(
                arm_id="A2", levels={"ligand": "XPhos", "base": "K3PO4"}, replicate_of="A1"
            ),
        ],
    )
    assert arms_are_distinct(design).passed


def test_arms_are_distinct_exempts_controls() -> None:
    """Two blanks are two blanks, not a duplicated row."""
    design = _design(
        arms=[
            ProtocolArm(arm_id="C1", control="blank"),
            ProtocolArm(arm_id="C2", control="blank"),
        ]
    )
    assert arms_are_distinct(design).passed


# --- layout_fits --------------------------------------------------------------------------------


def _plated(wells: list[Well], *, arms: list[str], plate_format: int = 24) -> ExperimentDesign:
    """A screen whose layout is supplied by hand, so `layout_fits` sees what it is given."""
    return _design(
        request=_request(mode="screen"),
        arms=[ProtocolArm(arm_id=arm_id) for arm_id in arms],
        layout=PlateLayout(plate_format=plate_format, rows=4, columns=6, wells=wells).model_dump(),
    )


def test_layout_fits_passes_a_plate_that_holds_every_arm_once() -> None:
    design = _plated([_well("A1", 0, 0, "A1", 1), _well("A2", 0, 1, "A2", 2)], arms=["A1", "A2"])
    verdict = layout_fits(design)
    assert verdict.passed and verdict.severity == "blocker"


def test_layout_fits_blocks_an_unknown_plate_format() -> None:
    """A 60-well plate would be laid out as 6x10 or 10x6, and the two are different plates."""
    design = _plated([_well("A1", 0, 0, "A1", 1)], arms=["A1"], plate_format=60)
    verdict = layout_fits(design)
    assert not verdict.passed and verdict.severity == "blocker"
    assert "unknown plate format 60" in verdict.detail


def test_layout_fits_blocks_more_wells_than_the_plate_holds() -> None:
    arms = [f"A{index}" for index in range(25)]
    wells = [_well(f"W{index}", 0, index, arm, index + 1) for index, arm in enumerate(arms)]
    verdict = layout_fits(_plated(wells, arms=arms))
    assert not verdict.passed
    assert "25 wells on a 24-well plate" in verdict.detail


def test_layout_fits_blocks_two_arms_in_one_well() -> None:
    design = _plated([_well("A1", 0, 0, "A1", 1), _well("A1", 0, 0, "A2", 2)], arms=["A1", "A2"])
    verdict = layout_fits(design)
    assert not verdict.passed
    assert "same well" in verdict.detail


def test_layout_fits_blocks_an_arm_with_no_well() -> None:
    verdict = layout_fits(_plated([_well("A1", 0, 0, "A1", 1)], arms=["A1", "A2"]))
    assert not verdict.passed
    assert "arms with no well: A2" in verdict.detail


def test_layout_fits_blocks_a_well_naming_no_arm() -> None:
    design = _plated([_well("A1", 0, 0, "A1", 1), _well("A2", 0, 1, "ghost", 2)], arms=["A1"])
    verdict = layout_fits(design)
    assert not verdict.passed
    assert "wells naming no arm: ghost" in verdict.detail


def test_layout_fits_blocks_a_run_order_that_is_not_one_to_n() -> None:
    """The run order is what a drift analysis reads back; a gap in it is unusable."""
    design = _plated([_well("A1", 0, 0, "A1", 1), _well("A2", 0, 1, "A2", 3)], arms=["A1", "A2"])
    verdict = layout_fits(design)
    assert not verdict.passed
    assert "run order is not 1..n" in verdict.detail


def test_layout_fits_passes_a_single_experiment_with_no_layout() -> None:
    assert layout_fits(_design()).passed


# --- forbidden_absent ---------------------------------------------------------------------------


def test_forbidden_absent_passes_when_the_exclusion_is_honoured() -> None:
    design = _design(
        request=_request(forbidden=["DMF"]),
        base={"charge": [ChargeLine(component="toluene", smiles="Cc1ccccc1").model_dump()]},
    )
    verdict = forbidden_absent(design)
    assert verdict.passed and verdict.severity == "blocker"
    assert "1 exclusions honoured" in verdict.detail


def test_forbidden_absent_blocks_a_reagent_matched_by_name() -> None:
    design = _design(
        request=_request(forbidden=["DMF"]),
        base={"charge": [ChargeLine(component="dmf").model_dump()]},
    )
    verdict = forbidden_absent(design)
    assert not verdict.passed and verdict.severity == "blocker"
    assert "DMF" in verdict.detail


def test_forbidden_absent_blocks_a_reagent_matched_by_structure_written_differently() -> None:
    """The exclusion must not be defeatable by spelling.

    The chemist wrote one SMILES for DMF and the design wrote the other.
    """
    design = _design(
        request=_request(forbidden=[_DMF]),
        base={
            "charge": [ChargeLine(component="the amide solvent", smiles=_DMF_REVERSED).model_dump()]
        },
    )
    verdict = forbidden_absent(design)
    assert not verdict.passed
    assert _DMF in verdict.detail


def test_forbidden_absent_blocks_a_forbidden_name_hiding_on_a_factor_level() -> None:
    """A screen's solvents live on its levels, which is where an exclusion is most easily lost."""
    design = _design(
        request=_request(forbidden=["DMF"]),
        factors=[
            Factor(
                name="solvent",
                kind="categorical",
                levels=[FactorLevel(label="DMF"), FactorLevel(label="toluene")],
            )
        ],
    )
    assert not forbidden_absent(design).passed


def test_forbidden_absent_passes_when_nothing_is_forbidden() -> None:
    assert forbidden_absent(_design()).passed


# --- evidence_present ---------------------------------------------------------------------------


def test_evidence_present_blocks_a_design_citing_nothing() -> None:
    """The blocker that makes "use the record and the tools" a property of the code."""
    verdict = evidence_present(_design())
    assert not verdict.passed and verdict.severity == "blocker"
    assert "cites nothing" in verdict.detail


def test_evidence_present_warns_when_only_a_tool_is_cited() -> None:
    design = _design(evidence=[EvidenceRef(kind="tool", tool="predict_pka", summary="pKa 10.3")])
    verdict = evidence_present(design)
    assert not verdict.passed and verdict.severity == "warning"
    assert "no precedent cited" in verdict.detail


def test_evidence_present_warns_when_only_a_precedent_is_cited() -> None:
    design = _design(evidence=[EvidenceRef(kind="precedent", ref="reaction-1", summary="72%")])
    verdict = evidence_present(design)
    assert not verdict.passed and verdict.severity == "warning"
    assert "nothing was computed" in verdict.detail


def test_evidence_present_passes_with_both_a_grounding_citation_and_a_tool() -> None:
    verdict = evidence_present(_design(evidence=_cited()))
    assert verdict.passed and verdict.severity == "blocker"


@pytest.mark.parametrize("kind", ["precedent", "record", "note", "observation"])
def test_evidence_present_counts_every_grounding_kind_as_precedent(kind: str) -> None:
    """Four kinds ground a design; a fifth (`tool`) is the computation beside them."""
    design = _design(
        evidence=[
            EvidenceRef.model_validate(
                {"kind": kind, "ref": "reaction-9", "summary": "the record says so"}
            ),
            EvidenceRef(kind="tool", tool="predict_pka", summary="computed"),
        ]
    )
    assert evidence_present(design).passed


def test_a_citation_that_names_nothing_to_open_does_not_count() -> None:
    """Two sentences are not two citations, and the blocker is what proves it.

    The one that matters: a grounding kind with no `ref` and a `tool` kind with no `tool` name are
    both prose a model can write about work it did not do. Before this they cleared the blocker
    between them — measured — which made the ADR's central claim ("use the record and the tools is
    a property of the code") false on the only turn it has to hold, the one where the model has an
    answer it likes.
    """
    verdict = evidence_present(
        _design(
            evidence=[
                EvidenceRef(kind="precedent", summary="prior work supports 80 C"),
                EvidenceRef(kind="tool", summary="I computed the pKa"),
            ]
        )
    )
    assert not verdict.passed and verdict.severity == "blocker"
    # The message names *which* of the two failures this is: citations were supplied, and none of
    # them is followable. Saying "cites nothing" over two supplied references sent the model back to
    # re-run five search tools when the fix was one empty field.
    assert "none is followable" in verdict.detail
    assert "prior work supports 80 C" in verdict.detail


def test_an_unfollowable_citation_is_named_beside_the_ones_that_counted() -> None:
    """A dropped citation is reported rather than silently uncounted."""
    verdict = evidence_present(
        _design(evidence=[*_cited(), EvidenceRef(kind="tool", summary="a bare sentence")])
    )
    assert verdict.passed
    assert "nothing names what to open" in verdict.detail
    assert "a bare sentence" in verdict.detail


# --- hazard_screen_ran --------------------------------------------------------------------------


def test_hazard_screen_ran_fails_when_no_screen_is_cited() -> None:
    verdict = hazard_screen_ran(_design(evidence=_cited()))
    assert not verdict.passed and verdict.severity == "warning"
    assert "no hazard screen" in verdict.detail


@pytest.mark.parametrize(
    "tool", ["screen_hazards", "screen_genotoxic_alerts", "ich_impurity_limit"]
)
def test_hazard_screen_ran_passes_on_any_of_the_three_screens(tool: str) -> None:
    design = _design(evidence=[EvidenceRef(kind="tool", tool=tool, summary="screened")])
    verdict = hazard_screen_ran(design)
    assert verdict.passed
    assert tool in verdict.detail


def test_hazard_screen_ran_ignores_an_unrelated_tool() -> None:
    """The check names three screens; a `predict_pka` citation is not one of them."""
    design = _design(evidence=[EvidenceRef(kind="tool", tool="predict_pka", summary="pKa")])
    assert not hazard_screen_ran(design).passed


# --- controls_present ---------------------------------------------------------------------------


def test_controls_present_warns_about_a_plate_with_no_control() -> None:
    design = _design(request=_request(mode="screen"), arms=[ProtocolArm(arm_id="A1")])
    verdict = controls_present(design)
    assert not verdict.passed and verdict.severity == "warning"
    assert "no control on the plate" in verdict.detail


def test_controls_present_passes_a_plate_that_carries_one() -> None:
    design = _design(
        request=_request(mode="screen"),
        arms=[ProtocolArm(arm_id="A1"), ProtocolArm(arm_id="C1", control="positive")],
    )
    verdict = controls_present(design)
    assert verdict.passed
    assert "C1" in verdict.detail


def test_controls_present_does_not_ask_a_single_experiment_for_a_control() -> None:
    assert controls_present(_design()).passed


# --- objectives_are_measured --------------------------------------------------------------------


def test_objectives_are_measured_fails_when_nothing_reports_an_objective() -> None:
    design = _design(request=_request(objectives=["yield", "purity"]))
    verdict = objectives_are_measured(design)
    assert not verdict.passed and verdict.severity == "warning"
    assert "purity" in verdict.detail and "yield" in verdict.detail


def test_objectives_are_measured_passes_when_an_analytic_names_each_objective() -> None:
    """Matched case-insensitively, so `Yield` on the analytic answers `yield` on the request."""
    design = _design(
        request=_request(objectives=["yield"]),
        base={
            "analytics": [
                Analytic(name="HPLC", timing="on completion", measures=["Yield"]).model_dump()
            ]
        },
    )
    assert objectives_are_measured(design).passed


def test_objectives_are_measured_passes_when_no_objective_is_stated() -> None:
    assert objectives_are_measured(_design()).passed


# --- quantities_are_plausible -------------------------------------------------------------------


def test_quantities_are_plausible_flags_a_kelvin_value_in_the_celsius_field() -> None:
    """353.15 is 80 °C typed as Kelvin, and it looks exactly like a real high-temperature run."""
    design = _design(base={"setpoints": Setpoints(temperature_c=353.15).model_dump()})
    verdict = quantities_are_plausible(design)
    assert not verdict.passed and verdict.severity == "warning"
    assert "Kelvin" in verdict.detail


def test_quantities_are_plausible_accepts_a_real_setpoint() -> None:
    design = _design(base={"setpoints": Setpoints(temperature_c=80.0, time_h=16.0).model_dump()})
    assert quantities_are_plausible(design).passed


def test_quantities_are_plausible_reads_an_arms_own_setpoints() -> None:
    """An override is where a unit mistake survives a correct shared body."""
    design = _design(arms=[ProtocolArm(arm_id="A1", setpoints=Setpoints(temperature_c=373.15))])
    verdict = quantities_are_plausible(design)
    assert not verdict.passed
    assert "arm A1" in verdict.detail


def test_quantities_are_plausible_flags_minutes_typed_into_an_hours_field() -> None:
    design = _design(base={"setpoints": Setpoints(time_h=960.0).model_dump()})
    assert not quantities_are_plausible(design).passed


def test_quantities_are_plausible_flags_a_zero_equivalents_line() -> None:
    design = _design(
        base={"charge": [ChargeLine(component="ligand", equivalents=0.0).model_dump()]}
    )
    verdict = quantities_are_plausible(design)
    assert not verdict.passed
    assert "0 equivalents" in verdict.detail


# --- coverage_is_stated -------------------------------------------------------------------------


def test_coverage_is_stated_reports_a_full_grid() -> None:
    design = _design(
        request=_request(mode="screen"),
        factors=_two_factors(),
        arms=[
            ProtocolArm(arm_id=f"A{index}", levels={"ligand": ligand, "base": base})
            for index, (ligand, base) in enumerate(
                [("XPhos", "K3PO4"), ("XPhos", "Cs2CO3"), ("SPhos", "K3PO4"), ("SPhos", "Cs2CO3")]
            )
        ],
    )
    verdict = coverage_is_stated(design)
    assert verdict.severity == "note" and verdict.passed
    assert "full grid: 4 of 4" in verdict.detail


def test_coverage_is_stated_reports_a_reduced_design_and_asks_what_is_confounded() -> None:
    design = _design(
        request=_request(mode="screen"),
        factors=_two_factors(),
        arms=[ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4"})],
    )
    verdict = coverage_is_stated(design)
    # **Passing, and the sentence reaches the page anyway.** This check had no `_fail` in any
    # branch, and the first fix for that made it fail — which was the wrong half. A fractional
    # factorial is a deliberate, textbook design that `generate_screening_design` emits, and
    # nothing in `ExperimentDesign` records the confounding statement this asks for, so every
    # correct reduced plate would have carried a failed check it could not clear. The half that
    # was right is in `render_markdown`, which now lists every `note` rather than failed checks
    # only: the sentence reaches the reader, and the check stays a check a reader believes.
    assert verdict.severity == "note" and verdict.passed
    assert "reduced design: 1 of 4" in verdict.detail
    assert "confounded" in verdict.detail


def test_coverage_is_stated_never_refuses_and_a_campaign_is_outside_it() -> None:
    """A `campaign` may ship a first round that does not cover its grid.

    This check is a `note` in every branch — an assertion that fails whoever promotes it.
    """
    campaign = _design(
        request=_request(mode="campaign"),
        factors=_two_factors(),
        arms=[ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4"})],
    )
    assert coverage_is_stated(campaign).detail == "not a fixed screen"
    for design in (campaign, _design()):
        verdict = coverage_is_stated(design)
        assert verdict.passed and verdict.severity == "note"


def test_coverage_is_stated_counts_neither_controls_nor_replicates_as_coverage() -> None:
    design = _design(
        request=_request(mode="screen"),
        factors=_two_factors(),
        arms=[
            ProtocolArm(arm_id="A1", levels={"ligand": "XPhos", "base": "K3PO4"}),
            ProtocolArm(
                arm_id="A2", levels={"ligand": "XPhos", "base": "K3PO4"}, replicate_of="A1"
            ),
            ProtocolArm(arm_id="C1", control="blank"),
        ],
    )
    assert "1 of 4" in coverage_is_stated(design).detail


# --- the set as a whole -------------------------------------------------------------------------


def test_check_ids_matches_what_run_checks_actually_produces() -> None:
    """Both directions: no id is advertised that nothing produces, and none is produced unlisted.

    A UI legend and a stored verdict are read against `check_ids()`, so a check renamed on one side
    only would leave a row nothing explains — or a legend entry nothing ever fills. Asserted at both
    stages, because a stage that quietly dropped a check would show a shorter list to a chemist who
    has no way to tell that from a check that passed.
    """
    designs = (_design(), _protocol(), _design(evidence=_cited(), request=_request(mode="screen")))
    stages: tuple[CheckStage, ...] = ("request", "protocol")
    for design in designs:
        for stage in stages:
            produced = [check.check_id for check in run_checks(design, stage=stage)]
            assert len(produced) == len(set(produced))
            assert set(produced) == set(check_ids())
            assert produced == list(check_ids())


def test_run_checks_keeps_its_declared_reading_order() -> None:
    """Unreadable first, then arithmetically wrong, then missing, then merely worth knowing."""
    assert check_ids()[0] == "is_a_protocol"
    assert check_ids()[1] == "components_resolve"
    assert check_ids()[-1] == "coverage_is_stated"
    assert check_ids().index("charge_is_consistent") < check_ids().index("evidence_present")


def test_blockers_selects_exactly_the_failed_blocking_checks() -> None:
    """A passing blocker and a failing warning are both excluded, which is the whole distinction."""
    checks = [
        ProtocolCheck(check_id="a", severity="blocker", passed=False, detail="stop"),
        ProtocolCheck(check_id="b", severity="blocker", passed=True),
        ProtocolCheck(check_id="c", severity="warning", passed=False),
        ProtocolCheck(check_id="d", severity="note", passed=False),
    ]
    assert [check.check_id for check in blockers(checks)] == ["a"]


def test_a_protocol_citing_nothing_is_blocked_for_that_one_reason() -> None:
    """The floor `draft_experiment_protocol` enforces.

    A real procedure with no citations is refused for the reason that matters, rather than for an
    accident of the other twelve.
    """
    uncited = _design(arms=[ProtocolArm(arm_id="A1")])
    assert [check.check_id for check in blockers(run_checks(uncited))] == ["evidence_present"]


def test_a_cited_single_experiment_clears_every_blocker() -> None:
    """The passing direction of the whole set, so the refusal above is about evidence alone."""
    assert blockers(run_checks(_protocol())) == []


# --- is_a_protocol ------------------------------------------------------------------------------


def test_is_a_protocol_blocks_a_structured_ask_stored_as_a_draft() -> None:
    """A design with nothing to do is the intake, not a protocol.

    Storing it as one would put an ask and a procedure in the same table under the same status.
    """
    verdict = is_a_protocol(_design(evidence=_cited()))
    assert not verdict.passed and verdict.severity == "blocker"
    assert "structured ask rather than a protocol" in verdict.detail


@pytest.mark.parametrize(
    "part",
    [
        {"arms": [ProtocolArm(arm_id="A1")]},
        {"base": {"steps": [{"index": 1, "kind": "stir", "text": "stir at 80 C"}]}},
        {"base": {"charge": [ChargeLine(component="aryl chloride").model_dump()]}},
    ],
)
def test_is_a_protocol_passes_on_any_of_the_three_things_that_make_one(
    part: dict[str, object],
) -> None:
    """An arm, a step or a charge table — any one of them says what to do."""
    assert is_a_protocol(_design(**part)).passed


# --- the request stage --------------------------------------------------------------------------


def test_the_request_stage_evaluates_only_what_an_ask_can_answer() -> None:
    """A blocker that fires on the normal path is one whoever reads it learns to ignore.

    That is exactly the property the one real blocker depends on.
    """
    ask = _design(
        request=_request(
            components=[RequestedComponent(name_as_written="mystery", smiles="not-a-smiles")]
        )
    )
    verdicts = {check.check_id: check for check in run_checks(ask, stage="request")}

    # The two that are about the ask itself are really evaluated, and really fail.
    assert not verdicts["components_resolve"].passed
    assert verdicts["components_resolve"].severity == "blocker"

    # Everything else is reported rather than omitted, as a passing note naming what it waits for.
    assert verdicts["evidence_present"].passed
    assert verdicts["evidence_present"].severity == "note"
    assert "only the ask" in verdicts["evidence_present"].detail
    assert verdicts["is_a_protocol"].passed and verdicts["is_a_protocol"].severity == "note"


def test_an_ordinary_structured_ask_produces_no_blocker_at_the_request_stage() -> None:
    """The passing direction: the intake path is clean, so a blocker there means something."""
    assert blockers(run_checks(_design(), stage="request")) == []
    # The same design at the protocol stage is refused — which is what makes the stage a decision.
    assert blockers(run_checks(_design())) != []


def test_naming_the_solvent_you_want_replaced_is_not_a_contradiction() -> None:
    """The commonest process-chemistry ask there is, which used to be permanently unstorable.

    A chemist getting out of DMF names DMF as the incumbent and forbids it in one sentence. Reading
    the *ask's* own components as species the design uses made that a `blocker` — "the design uses
    reagents the request forbids: DMF" — over a design whose solvent is 2-MeTHF, and
    `draft_experiment_protocol` raises on any blocker, so there was no way to state both the
    incumbent and the exclusion.
    """
    ask = _design(
        request=_request(
            forbidden=["DMF"],
            components=[RequestedComponent(name_as_written="DMF", smiles=_DMF)],
        ),
        base={"setpoints": Setpoints(solvent="2-MeTHF", temperature_c=80, time_h=4).model_dump()},
    )
    verdicts = {check.check_id: check for check in run_checks(ask)}
    assert verdicts["forbidden_absent"].passed
    assert blockers(run_checks(ask, stage="request")) == []


def test_a_design_that_actually_runs_in_the_forbidden_solvent_is_still_refused() -> None:
    """The half that has to keep working: the exclusion binds what the design *does*."""
    design = _design(
        request=_request(
            forbidden=["DMF"],
            components=[RequestedComponent(name_as_written="DMF", smiles=_DMF)],
        ),
        base={"setpoints": Setpoints(solvent="DMF", temperature_c=80, time_h=4).model_dump()},
    )
    verdict = {check.check_id: check for check in run_checks(design)}["forbidden_absent"]
    assert not verdict.passed and verdict.severity == "blocker"
    assert "DMF" in verdict.detail


def test_a_rounded_catalyst_line_is_not_a_blocker() -> None:
    """The tolerance has to allow the rounding a chemist actually writes.

    250 mg of a 182 g/mol aryl halide is 1.37 mmol and 5 mol% Pd is 0.0685 mmol, which a chemist
    writes `0.07`. A flat 2% of the line's own figure made that a *blocker* — and a blocker refuses
    the draft outright. Swept across the usual scales and loadings, 4 of 18 correct tables were
    refused, every one a normal catalyst or ligand line at a non-round limiting scale.
    """
    design = _design(
        base={
            "setpoints": Setpoints(temperature_c=80, time_h=16, solvent="2-MeTHF").model_dump(),
            "charge": [
                ChargeLine(
                    component="aryl bromide",
                    role=SpeciesRole.STARTING_MATERIAL,
                    limiting=True,
                    equivalents=1.0,
                    amount_mmol=1.37,
                ).model_dump(),
                ChargeLine(
                    component="Pd(OAc)2",
                    role=SpeciesRole.CATALYST,
                    equivalents=0.05,
                    amount_mmol=0.07,
                ).model_dump(),
            ],
        }
    )
    verdict = charge_is_consistent(design)
    assert verdict.passed, verdict.detail


def test_a_charge_table_that_really_disagrees_is_still_a_blocker() -> None:
    """The half the tolerance must not swallow: a factor-of-ten slip is never close."""
    design = _design(
        base={
            "setpoints": Setpoints(temperature_c=80, time_h=16, solvent="2-MeTHF").model_dump(),
            "charge": [
                ChargeLine(
                    component="aryl bromide",
                    role=SpeciesRole.STARTING_MATERIAL,
                    limiting=True,
                    equivalents=1.0,
                    amount_mmol=1.37,
                ).model_dump(),
                ChargeLine(
                    component="boronate",
                    role=SpeciesRole.REAGENT,
                    equivalents=1.2,
                    amount_mmol=0.164,
                ).model_dump(),
            ],
        }
    )
    verdict = charge_is_consistent(design)
    assert not verdict.passed and verdict.severity == "blocker"


def test_a_screen_that_misses_half_its_grid_does_not_report_a_full_one() -> None:
    """Counting arms compared a number to a product of level counts.

    Four arms covering two of four declared combinations reported "full grid: 4 of 4 combinations",
    and `render_markdown` prints that sentence to the chemist. A declared level that is never run is
    exactly what this check exists to make somebody say out loud.
    """
    solvent = Factor(
        name="solvent",
        kind="categorical",
        levels=[FactorLevel(label="THF"), FactorLevel(label="toluene")],
    )
    ligand = Factor(
        name="ligand",
        kind="categorical",
        levels=[FactorLevel(label="XPhos"), FactorLevel(label="SPhos")],
    )
    design = _design(
        request=_request(mode="screen"),
        factors=[solvent, ligand],
        arms=[
            # `toluene` is declared and never run; two of the four combinations are duplicated.
            ProtocolArm(arm_id="A1", levels={"solvent": "THF", "ligand": "XPhos"}),
            ProtocolArm(arm_id="A2", levels={"solvent": "THF", "ligand": "SPhos"}),
            ProtocolArm(arm_id="A3", levels={"solvent": "THF", "ligand": "XPhos"}),
            ProtocolArm(arm_id="A4", levels={"solvent": "THF", "ligand": "SPhos"}),
        ],
    )
    verdict = coverage_is_stated(design)
    assert "2 of 4 combinations" in verdict.detail
    assert "full grid" not in verdict.detail


def test_the_plate_checks_read_the_design_not_the_mode_field_of_the_ask() -> None:
    """One mis-set enum on the intake used to switch off three checks on a real plate."""
    design = _design(
        request=_request(mode="single"),
        arms=[ProtocolArm(arm_id=f"A{index}") for index in range(1, 97)],
    )
    # `layout_fits` no longer excuses itself as "a single experiment"; with no layout at all it
    # degrades to the carry-forward warning, which is a separate deliberate behaviour.
    assert "single experiment" not in layout_fits(design).detail
    assert not controls_present(design).passed


def test_a_step_states_a_temperature_and_a_duration_that_are_checked() -> None:
    """A step at 5000 C for a million hours passed as "setpoints and amounts in range"."""
    design = _design(
        base={
            "setpoints": Setpoints(temperature_c=80, time_h=16, solvent="2-MeTHF").model_dump(),
            "steps": [
                ProtocolStep(
                    index=1,
                    kind=ProtocolStepKind.TEMPERATURE,
                    text="heat it",
                    temperature_c=5000.0,
                    duration_h=1_000_000.0,
                ).model_dump()
            ],
        }
    )
    verdict = quantities_are_plausible(design)
    assert not verdict.passed
    assert "5000" in verdict.detail and "1000000" in verdict.detail


def test_a_randomized_layout_without_a_seed_is_refused_by_the_model() -> None:
    """`place()` refuses this; a browser-posted layout never goes through `place()`."""
    with pytest.raises(ValidationError, match="needs a seed"):
        PlateLayout(plate_format=24, rows=4, columns=6, randomized=True, seed=None)
