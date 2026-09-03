"""The bench document, which nothing asserted.

`render_markdown`, `run_sheet_rows` and `summarise` were imported by no test in the suite: the whole
assertion surface over 472 lines was two lines elsewhere checking that the page starts with a title
and contains the string `## Evidence`. This is the form a chemist actually carries to a fume hood,
so what it drops or garbles is not a cosmetic defect — every property below was false, and the first
one put a chemist at a hydrogenation they had not read about.
"""

from __future__ import annotations

from chemclaw.protocols.diff import diff_designs
from chemclaw.protocols.models import (
    ChargeLine,
    EvidenceRef,
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
from chemclaw.protocols.render import render_markdown, run_sheet_rows, summarise


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
    """The one that matters most: a page that told a chemist 1 bar N2 for a 50 bar H2 arm.

    `## Conditions` renders the shared body whenever there is more than one arm, and the run sheet
    carried only temperature, time and solvent — so the overriding arm rendered byte for byte like
    the one that did not, with `H2` and `50` appearing nowhere and no check firing.
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
    """Three identical rows with nothing saying they are a deliberate triplicate.

    `arms_are_distinct` *skips* arms carrying `replicate_of`, so no check flags them either — the
    one field that makes those rows legitimate was the one field the document dropped, and a chemist
    reads a copy-paste error.
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
    """`_labelled`'s disambiguation, which nothing exercised with a duplicate key.

    `base.charge` is keyed by `component`, and a solvent charged in two portions — an addition and
    a rinse — is entirely ordinary. Without the disambiguation the second line overwrites the first,
    so a chemist editing the *first* toluene charge from 5 mL to 9 mL diffs to nothing at all: the
    edit vanishes from the one table this system keeps in order to learn from those edits.
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
