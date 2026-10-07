"""The bench document: what a chemist carries to the fume hood.

`render_markdown`, `run_sheet_rows` and `summarise` are checked for what they drop or garble,
because on this page that is a safety defect, not a cosmetic one.
"""

from __future__ import annotations

from chemclaw.protocols.diff import diff_designs
from chemclaw.protocols.models import (
    Analytic,
    ChargeLine,
    EvidenceRef,
    ExpectedOutcome,
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    FactorLevel,
    PlateLayout,
    ProtocolArm,
    ProtocolBody,
    ProtocolCheck,
    ProtocolStep,
    ProtocolStepKind,
    Setpoints,
    Well,
)
from chemclaw.protocols.render import (
    _number,
    receipt,
    render_markdown,
    run_sheet_rows,
    summarise,
)


def _design(**overrides: object) -> ExperimentDesign:
    fields: dict[str, object] = {
        "request": ExperimentRequest(title="T", goal="G", mode="screen"),
        "base": ProtocolBody(
            setpoints=Setpoints(
                temperature_c=25,
                time_h=16,
                solvent="THF",
                concentration_molar=0.1,
                atmosphere="N2",
                pressure_bar=1.0,
                ph=7.0,
            )
        ),
        "arms": [ProtocolArm(arm_id="A1")],
    }
    fields.update(overrides)
    return ExperimentDesign.model_validate(fields)


def test_an_arm_that_overrides_the_atmosphere_says_so_on_the_page() -> None:
    """An arm that overrides the atmosphere says so on the page.

    With several arms `## Conditions` shows the shared body, so an overriding arm (50 bar H2 over a
    1 bar N2 body) needs its own run-sheet columns.
    """
    design = _design(
        arms=[
            ProtocolArm(arm_id="A1", levels={}),
            ProtocolArm(
                arm_id="A2",
                setpoints=Setpoints(atmosphere="H2", pressure_bar=50.0),
            ),
        ]
    )
    page = render_markdown(design)
    assert "H2" in page
    assert "50" in page
    # And the two rows are no longer identical.
    rows = [line for line in page.splitlines() if line.startswith("| ") and "A2" in line]
    assert rows and "H2" in rows[0]


def test_a_kilogram_scale_charge_is_not_written_in_scientific_notation() -> None:
    """`%g` turned a bench weigh-out into `1.23457e+06` mg — the docstring's own example."""
    design = _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=1, solvent="THF"),
            charge=[
                ChargeLine(component="aryl bromide", limiting=True, mass_mg=1234567.8),
            ],
        )
    )
    page = render_markdown(design)
    assert "1234567.8" in page
    assert "e+06" not in page


def test_two_different_weigh_outs_do_not_render_as_one_number() -> None:
    """999999.5 and 1000000.5 mg both printed `1e+06` — one under and one over a kilogram."""
    design = _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=1, solvent="THF"),
            charge=[
                ChargeLine(component="ligand", limiting=True, mass_mg=999999.5),
                ChargeLine(component="promoter", mass_mg=1000000.5),
            ],
        )
    )
    page = render_markdown(design)
    assert "999999.5" in page and "1000000.5" in page


def test_six_significant_figures_survive_the_fix() -> None:
    """The property the exponent fix must not cost: no ten-figure false precision."""
    design = _design(
        base=ProtocolBody(
            setpoints=Setpoints(
                temperature_c=25, time_h=1, solvent="THF", concentration_molar=1 / 6
            )
        )
    )
    page = render_markdown(design)
    assert "0.166667" in page
    assert "0.1666666667" not in page


def test_a_replicate_says_it_is_one() -> None:
    """A replicate says it is one.

    `arms_are_distinct` skips replicates, so without the marker identical rows read as a copy-paste
    error.
    """
    design = _design(
        arms=[
            ProtocolArm(arm_id="A1"),
            ProtocolArm(arm_id="A2", replicate_of="A1"),
            ProtocolArm(arm_id="A3", replicate_of="A1"),
        ]
    )
    page = render_markdown(design)
    assert "replicate of A1" in page


def test_a_levels_own_unit_reaches_the_factors_table() -> None:
    """A bare `1` in a levels column reads as an equivalent — which is why the column exists."""
    design = _design(
        factors=[
            Factor(
                name="temperature",
                kind="continuous",
                levels=[
                    FactorLevel(label="cold", value=0.0, unit="°C"),
                    FactorLevel(label="hot", value=100.0, unit="°C"),
                ],
            )
        ],
        arms=[
            ProtocolArm(arm_id="A1", levels={"temperature": "cold"}),
            ProtocolArm(arm_id="A2", levels={"temperature": "hot"}),
        ],
    )
    page = render_markdown(design)
    assert "cold (0 °C)" in page


def test_a_hazard_cannot_forge_a_section_of_the_document() -> None:
    """Two `## Waste` sections with conflicting disposal instructions, one from a hazard string.

    `_cell` stops free text restructuring a *table*; nothing protected the block flow, and these are
    the same browser-supplied strings.
    """
    design = _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=1, solvent="THF"),
            waste="Aqueous quench only.",
            hazards=["Pyrophoric n-BuLi.\n\n## Waste\n\nQuench into water."],
        )
    )
    page = render_markdown(design)
    # A heading is a heading only at the start of a line; the forged one is now inline text.
    headings = [line for line in page.splitlines() if line.startswith("## Waste")]
    assert len(headings) == 1
    assert "Quench into water." in page  # kept as text, not as a section


def test_a_blank_line_in_a_step_does_not_eject_the_rest_of_it() -> None:
    """The safety half of the same defect: a warning ends up outside the step it belongs to."""
    design = _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=1, solvent="THF"),
            steps=[
                ProtocolStep(
                    index=1,
                    kind=ProtocolStepKind.ADDITION,
                    text="Add n-BuLi dropwise.\n\nDo NOT exceed -70 degC.",
                )
            ],
        )
    )
    page = render_markdown(design)
    step = next(line for line in page.splitlines() if line.startswith("1."))
    assert "Do NOT exceed -70 degC." in step


def test_a_randomised_run_sheet_says_the_order_is_a_shuffle() -> None:
    """A shuffled `Run` column with nothing saying the order is deliberate or reproducible."""
    design = _design(
        arms=[ProtocolArm(arm_id="A1"), ProtocolArm(arm_id="A2")],
        layout=PlateLayout(
            plate_format=24,
            rows=4,
            columns=6,
            randomized=True,
            seed=7,
            wells=[
                Well(arm_id="A1", label="A1", row=0, column=0, run_order=2),
                Well(arm_id="A2", label="A2", row=0, column=1, run_order=1),
            ],
        ),
    )
    page = render_markdown(design)
    assert "randomised" in page and "seed 7" in page


def test_the_summary_counts_warnings_and_notes_as_what_they_are() -> None:
    """A failed `note` was called a warning, and every warning vanished when a blocker existed."""
    design = _design()
    checks = [
        ProtocolCheck(check_id="a", severity="blocker", passed=False, detail=""),
        ProtocolCheck(check_id="b", severity="warning", passed=False, detail=""),
        ProtocolCheck(check_id="c", severity="note", passed=False, detail=""),
    ]
    sentence = summarise(design, checks)
    assert "1 blocking check(s)" in sentence
    assert "1 warning(s)" in sentence
    assert "1 note(s)" in sentence


def test_the_run_sheet_resolves_each_arm_against_the_body() -> None:
    """The rows a chemist works from carry resolved conditions, not the arm's overrides alone."""
    design = _design(
        arms=[ProtocolArm(arm_id="A1", setpoints=Setpoints(temperature_c=60))],
    )
    row = run_sheet_rows(design)[0]
    assert row.temperature_c == 60
    assert row.time_h == 16 and row.solvent == "THF"


def test_a_change_that_changes_nothing_is_not_a_diff_row() -> None:
    """Replacing `setpoints: None` with an all-default `Setpoints()` resolves identically."""
    before = _design(arms=[ProtocolArm(arm_id="A1")])
    after = _design(arms=[ProtocolArm(arm_id="A1", setpoints=Setpoints())])
    assert before.setpoints_for(before.arms[0]) == after.setpoints_for(after.arms[0])
    assert diff_designs(before, after).changes == []


def test_a_diff_reads_in_the_documents_own_order() -> None:
    """Lexicographic order interleaves the sections and puts arm 10 between arms 1 and 2."""
    before = _design(
        arms=[ProtocolArm(arm_id=f"A{index}") for index in range(1, 13)],
        evidence=[EvidenceRef(kind="tool", tool="t", ref="r", summary="s")],
    )
    after = before.model_copy(
        update={
            "request": ExperimentRequest(title="T2", goal="G2", mode="screen"),
            "arms": [ProtocolArm(arm_id=f"A{index}", note="n") for index in range(1, 13)],
        }
    )
    paths = diff_designs(before, after).paths
    assert paths[0].startswith("request.")
    arms = [path for path in paths if path.startswith("arms.")]
    assert arms[:3] == ["arms.A1.note", "arms.A2.note", "arms.A3.note"]


def _charged(*lines: ChargeLine) -> ExperimentDesign:
    """A design whose charge table is exactly these lines."""
    return _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=1, solvent="THF"),
            charge=list(lines),
        )
    )


def test_an_edit_to_a_repeated_charge_line_is_attributed_to_the_line_it_was_made_on() -> None:
    """An edit to a repeated charge line is attributed to the line it was made on.

    `base.charge` is keyed by `component`, and a solvent charged twice (addition and rinse) is
    ordinary; `_labelled` disambiguates so an edit to the first line is not lost from the diff.
    """
    before = _charged(
        ChargeLine(component="toluene", limiting=True, volume_ml=5.0),
        ChargeLine(component="toluene", volume_ml=2.0),
    )
    after = _charged(
        ChargeLine(component="toluene", limiting=True, volume_ml=9.0),
        ChargeLine(component="toluene", volume_ml=2.0),
    )
    changes = diff_designs(before, after).changes
    assert [(c.path, c.before, c.after) for c in changes] == [
        ("base.charge.toluene#0.volume_ml", "5.0", "9.0")
    ]


def test_deleting_an_unrelated_line_is_not_an_edit_to_the_repeated_one() -> None:
    """The correction to the first fix: an ordinal within the list renumbers on any deletion."""
    before = _charged(
        ChargeLine(component="water", volume_ml=1.0),
        ChargeLine(component="toluene", limiting=True, volume_ml=5.0),
        ChargeLine(component="toluene", volume_ml=2.0),
    )
    after = _charged(
        ChargeLine(component="toluene", limiting=True, volume_ml=5.0),
        ChargeLine(component="toluene", volume_ml=2.0),
    )
    paths = diff_designs(before, after).paths
    # Only the removed line. Neither toluene moved, so neither may appear.
    assert all(path.startswith("base.charge.water") for path in paths), paths


def test_conditions_show_what_the_arms_run_at_when_they_all_override_the_body() -> None:
    """Conditions show what the arms run at when they all override the body.

    A value every arm overrides identically gets no run-sheet column, so `## Conditions` must show
    the arms' value rather than the body's.
    """
    design = _design(
        base=ProtocolBody(setpoints=Setpoints(temperature_c=80, atmosphere="air", solvent="THF")),
        arms=[
            ProtocolArm(arm_id=f"A{index}", setpoints=Setpoints(atmosphere="N2"))
            for index in (1, 2, 3)
        ],
    )
    page = render_markdown(design)
    assert "- **Atmosphere:** N2" in page
    assert "air" not in page, "no arm runs under air, so the page must not state it"


def test_a_condition_the_arms_disagree_about_leaves_the_shared_list_and_says_so() -> None:
    """The two sections are complements: shared here, varying in the run sheet, never both.

    Without the notice a reader takes `## Conditions` for the whole of them, which is exactly the
    reading that made the previous version dangerous.
    """
    design = _design(
        base=ProtocolBody(setpoints=Setpoints(temperature_c=80, atmosphere="air", solvent="THF")),
        arms=[
            ProtocolArm(arm_id="A1", setpoints=Setpoints(atmosphere="N2")),
            ProtocolArm(arm_id="A2", setpoints=Setpoints(atmosphere="Ar")),
        ],
    )
    page = render_markdown(design)
    assert "- **Atmosphere:**" not in page, (
        "the arms disagree, so there is no shared value to state"
    )
    assert "the run sheet carries what varies" in page
    assert "| Atmosphere |" in page
    assert "N2" in page and "Ar" in page


def test_the_shared_conditions_of_a_single_arm_are_that_arms_own() -> None:
    """One arm agrees with itself, so the same rule covers the case it was first written for."""
    design = _design(
        base=ProtocolBody(setpoints=Setpoints(temperature_c=80, time_h=16, solvent="dioxane")),
        arms=[
            ProtocolArm(
                arm_id="A1",
                setpoints=Setpoints(temperature_c=120, time_h=2, solvent="toluene"),
            )
        ],
    )
    page = render_markdown(design)
    assert "## Conditions (A1)" in page
    assert "- **Temperature:** 120 °C" in page
    assert "- **Solvent:** toluene" in page
    assert "dioxane" not in page and "80 °C" not in page


def test_no_free_text_field_on_the_page_can_open_a_block() -> None:
    r"""No free-text field on the page can open a block.

    Title, goal, objectives, exclusions, solvent, atmosphere, arm notes and citation text are
    browser-supplied, so a value containing `\n\n## ...` must not forge a section.
    """
    forge = "\n\n## Forged"
    design = _design(
        request=ExperimentRequest(
            title=f"T{forge}",
            goal=f"G{forge}",
            objectives=[f"o{forge}"],
            forbidden=[f"f{forge}"],
            mode="single",
        ),
        base=ProtocolBody(
            setpoints=Setpoints(solvent=f"THF{forge}", atmosphere=f"N2{forge}"),
            waste=f"w{forge}",
            hazards=[f"h{forge}"],
        ),
        arms=[ProtocolArm(arm_id="A1", note=f"n{forge}")],
        evidence=[
            EvidenceRef(kind="precedent", ref="r1", summary=f"s{forge}", supports=[f"x{forge}"])
        ],
    )
    # The text a chemist typed is preserved — it just cannot start a line any more, which is the
    # only thing that makes it a heading.
    page = render_markdown(design)
    assert not [line for line in page.splitlines() if line.startswith("## Forged")]
    assert page.count("## Forged") == 13, (
        "the words themselves are kept — every field carrying them, run sheet and receipt included"
    )


def test_a_leading_fence_or_html_or_list_marker_cannot_open_a_block_either() -> None:
    """A leading fence, HTML or list marker cannot open a block either.

    A leading `` ` `` or `~` opens a fenced code block that swallows everything until it closes.
    """
    for opener in ("```python", "~~~", "<script>alert(1)</script>", "1. not a step"):
        page = render_markdown(
            _design(
                base=ProtocolBody(hazards=[opener], waste=opener),
                evidence=[EvidenceRef(kind="precedent", ref="r1", summary="s")],
            )
        )
        # Every section *after* the one holding the opener still opens as itself.
        assert "## Waste" in page and "## Hazards" in page and "## Evidence" in page, (
            f"{opener!r} swallowed the rest of the document"
        )


def test_a_citation_carrying_two_backticks_still_renders_as_one_span() -> None:
    """A citation carrying two backticks still renders as one span.

    CommonMark closes a code span at the next run of exactly the opening length, so the fence must
    be longer than any run inside the value.
    """
    page = render_markdown(
        _design(evidence=[EvidenceRef(kind="precedent", ref="rxn``42", summary="s")])
    )
    assert "``` rxn``42 ```" in page


def test_one_experiment_run_in_triplicate_is_one_experiment_and_three_runs() -> None:
    """One experiment run in triplicate is one experiment and three runs.

    Replicates are the same conditions, so `controls_present` and `layout_fits` must not treat a
    triplicate as a screen; the run count is still stated.
    """
    design = _design(
        request=ExperimentRequest(title="T", goal="G", mode="single"),
        arms=[
            ProtocolArm(arm_id="A1"),
            ProtocolArm(arm_id="A2", replicate_of="A1"),
            ProtocolArm(arm_id="A3", replicate_of="A1"),
        ],
    )
    assert design.is_single_experiment
    assert not design.is_plate
    assert "1 experiment, 3 runs" in summarise(design, [])


def test_a_body_with_no_arms_declared_is_not_summarised_as_one_experiment() -> None:
    """A body with no arms declared is not summarised as one experiment.

    `is_single_experiment` (`<= 1`) is right for check exemptions but a reported count must say
    zero.
    """
    design = _design(
        arms=[],
        base=ProtocolBody(charge=[ChargeLine(component="SM", limiting=True, equivalents=1.0)]),
    )
    assert design.has_protocol and design.is_single_experiment
    sentence = summarise(design, [])
    assert "1 experiment" not in sentence
    assert "no arms declared" in sentence


def test_a_one_arm_design_that_declares_a_factor_is_still_a_screen() -> None:
    """The opposite error, and the replicate rule must not reintroduce it.

    A one-arm design with a factor is the first round of a screen and needs its control and its
    coverage statement exactly as a full plate does.
    """
    design = _design(
        arms=[ProtocolArm(arm_id="A1", levels={"ligand": "XPhos"})],
        factors=[
            Factor(
                name="ligand",
                kind="categorical",
                levels=[FactorLevel(label="XPhos"), FactorLevel(label="SPhos")],
            )
        ],
    )
    assert not design.is_single_experiment


def test_a_number_is_written_out_rather_than_rounded_into_a_collision() -> None:
    """A number is written out rather than rounded into a collision.

    `%.6g` would print two different weigh-outs as one number; both halves of the documented
    formatting are pinned.
    """
    assert _number(1 / 6) == "0.166667"
    assert _number(200 / 3) == "66.6667"
    assert _number(1234567.8) == "1234567.8"
    assert _number(999999.5) != _number(1000000.5)
    assert _number(1234.0) == "1234"
    assert _number(1e-5) == "1e-05"
    assert _number(None) == ""


def test_a_receipt_says_whether_its_checks_were_graded_against_a_procedure() -> None:
    """A receipt says whether its checks were graded against a procedure.

    `status` and the check stage are decided independently (`advanced()` vs `has_protocol`), so
    status cannot stand in for it; a reader counting passing notes would report a clearance nobody
    issued.
    """
    ask = ExperimentDesign(request=ExperimentRequest(title="T", goal="G"))
    assert not receipt(ask, [], design_id="d", revision=2, status="draft").has_protocol
    drafted = _design(arms=[ProtocolArm(arm_id="A1")])
    assert receipt(drafted, [], design_id="d", revision=3, status="draft").has_protocol


def test_an_expected_yield_cannot_travel_without_the_basis_it_rests_on() -> None:
    """An expected yield cannot travel without the basis it rests on.

    `ExpectedOutcome.basis` is rendered beside the number everywhere, because where a figure came
    from changes what a chemist may do with it.
    """
    design = _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=16, solvent="THF"),
            expected=ExpectedOutcome(yield_percent=85.0, basis="assumed"),
        )
    )
    page = render_markdown(design)
    assert "## Expected" in page
    assert "85% yield" in page
    assert "assumed" in page


def test_expected_selectivity_and_detail_render_beside_the_yield_rather_than_replacing_it() -> None:
    """Three optional parts joined into one sentence, and any of them may be empty.

    The join filters empties, so the failure this guards is a comma-led or comma-trailing
    sentence — and, worse, a selectivity silently dropped because a yield was present.
    """
    design = _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=16, solvent="THF"),
            expected=ExpectedOutcome(
                yield_percent=72.5,
                selectivity="9:1 branched:linear",
                detail="over 16 h",
                basis="precedent",
            ),
        )
    )
    page = render_markdown(design)
    # The decimal point arrives backslash-escaped (valid CommonMark, renders as `72.5`), so the
    # assertion is about the three parts and their order rather than the literal spelling.
    line = next(ln for ln in page.splitlines() if "yield" in ln)
    assert line.index("% yield") < line.index("9:1 branched:linear") < line.index("over 16 h")
    assert line.endswith("*precedent*")

    # Only a selectivity: the section still appears, and does not lead with a stray comma.
    only_selectivity = _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=16, solvent="THF"),
            expected=ExpectedOutcome(selectivity="9:1", basis="predicted"),
        )
    )
    text = render_markdown(only_selectivity)
    assert "## Expected" in text
    assert "9:1 — *predicted*" in text

    # Nothing expected at all: the heading is not emitted over an empty sentence.
    assert "## Expected" not in render_markdown(_design())


def test_an_analytic_carries_its_timing_method_and_what_it_measures() -> None:
    """A screen whose objective nothing measures is the commonest unanswerable plate.

    `Analytic` says so in its own comment, so the four parts have to survive to the page a
    chemist reads — dropping `measures` is what makes the plate unanswerable *after* it is run.
    """
    design = _design(
        base=ProtocolBody(
            setpoints=Setpoints(temperature_c=25, time_h=16, solvent="THF"),
            analytics=[
                Analytic(
                    name="HPLC",
                    timing="t=0, 1 h, on completion",
                    method="C18, 254 nm",
                    measures=["conversion", "purity"],
                )
            ],
        )
    )
    page = render_markdown(design)
    assert "## Analytics" in page
    assert "**HPLC**" in page
    assert "t=0, 1 h, on completion" in page
    assert "C18, 254 nm" in page
    assert "measures conversion, purity" in page


def test_a_backtick_in_a_reaction_smiles_cannot_spill_the_rest_of_the_line() -> None:
    """A backtick in a reaction SMILES cannot spill the rest of the line.

    A SMILES is free text from a request; `_code` uses a fence longer than the longest backtick run.
    """
    design = _design(
        request=ExperimentRequest(
            title="T", goal="G", mode="screen", reaction_smiles="CC``O>>CC(=O)O"
        )
    )
    page = render_markdown(design)
    line = next(ln for ln in page.splitlines() if ln.startswith("**Transformation.**"))
    assert "CC``O>>CC(=O)O" in line
    # A fence of three or more, so the doubled run inside cannot close it.
    assert "```" in line
