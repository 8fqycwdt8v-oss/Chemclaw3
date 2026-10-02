"""What changed between two revisions of an artefact, and the files a chemist keeps.

The diff's paths are the frozen contract's (`"lines 12-14"`, `"rows[3].yield"`, `"columns"`,
`"items[2].smiles"`, `"series[0].y[4]"`, `"spec"`), and its shape is `protocols.diff.FieldChange`'s
so one UI component renders both. The CSV exports go through the protocol export's formula guard;
the injection case is driven on a cell, a header and a structure label.
"""

import csv
import io
from typing import Any

from chemclaw.exhibits.diff import capped, diff_specs
from chemclaw.exhibits.export import export_filename, render_export
from chemclaw.exhibits.models import Spec, parse_spec


def _spec(raw: dict[str, Any]) -> Spec:
    """A parsed spec, as every caller of the diff holds one."""
    return parse_spec(raw)


def _table(rows: list[dict[str, Any]], columns: list[dict[str, str]] | None = None) -> Spec:
    """A two-column solvent table."""
    return _spec(
        {
            "kind": "table",
            "columns": columns
            or [{"key": "solvent", "label": "Solvent"}, {"key": "yield", "label": "Yield"}],
            "rows": rows,
        }
    )


def test_a_document_diffs_by_line_hunks_numbered_in_the_new_text() -> None:
    """An edit to lines 2-3 is one hunk at those lines; an inserted line is an addition."""
    before = _spec(
        {"kind": "document", "markdown": "# Plan\nDegas 2 min.\nHeat to 80 C.\nWork up."}
    )
    after = _spec(
        {"kind": "document", "markdown": "# Plan\nDegas 20 min.\nHeat to 60 C.\nWork up.\nDry."}
    )
    diff = diff_specs(before, after, from_revision=1, to_revision=2)
    assert [(c.path, c.kind) for c in diff.changes] == [
        ("lines 2-3", "changed"),
        ("line 5", "added"),
    ]
    assert diff.changes[0].before == "Degas 2 min.\nHeat to 80 C."
    assert diff.changes[0].after == "Degas 20 min.\nHeat to 60 C."
    assert (diff.from_revision, diff.to_revision) == (1, 2)


def test_a_table_diffs_per_cell_and_reports_a_new_header_as_columns() -> None:
    """A chemist's one-cell correction is one change at `rows[i].key`."""
    before = _table([{"solvent": "THF", "yield": 76}, {"solvent": "DMF", "yield": 64}])
    after = _table([{"solvent": "THF", "yield": 76}, {"solvent": "DMF", "yield": 61}])
    diff = diff_specs(before, after, from_revision=1, to_revision=2)
    assert [(c.path, c.before, c.after) for c in diff.changes] == [("rows[1].yield", "64", "61")]

    relabelled = _table(
        [{"solvent": "THF", "yield": 76}],
        [{"key": "solvent", "label": "Solvent"}, {"key": "yield", "label": "Isolated yield"}],
    )
    paths = [c.path for c in diff_specs(before, relabelled, from_revision=1, to_revision=2).changes]
    assert paths == ["columns", "rows[1]"]


def test_structures_and_charts_diff_at_the_item_field_and_the_point() -> None:
    """`items[2].smiles` and `series[0].y[4]`, as the contract spells them."""
    items: list[dict[str, Any]] = [
        {"smiles": "C"},
        {"smiles": "CC"},
        {"smiles": "CCC", "props": {"pka": 4.8}},
    ]
    edited = [dict(item) for item in items]
    edited[2] = {"smiles": "CCO", "props": {"pka": 4.9}}
    diff = diff_specs(
        _spec({"kind": "structures", "items": items}),
        _spec({"kind": "structures", "items": edited}),
        from_revision=1,
        to_revision=2,
    )
    assert [c.path for c in diff.changes] == ["items[2].smiles", "items[2].props.pka"]

    def chart(y: list[float]) -> Spec:
        return _spec(
            {
                "kind": "chart",
                "chart": "line",
                "x_label": "t (h)",
                "y_label": "conversion (%)",
                "series": [{"name": "A", "x": [0, 1, 2, 3, 4], "y": y}],
            }
        )

    points = diff_specs(
        chart([0, 20, 40, 60, 80]), chart([0, 20, 40, 60, 85]), from_revision=3, to_revision=4
    )
    assert [(c.path, c.before, c.after) for c in points.changes] == [("series[0].y[4]", "80", "85")]


def test_a_result_or_link_diffs_as_the_whole_spec_and_an_unchanged_one_as_nothing() -> None:
    """No finer path exists for a pin, so one `spec` row; identical revisions diff to nothing."""
    first = _spec({"kind": "link", "target": "note", "id": "rxn-a"})
    second = _spec({"kind": "link", "target": "note", "id": "rxn-b"})
    assert [c.path for c in diff_specs(first, second, from_revision=1, to_revision=2).changes] == [
        "spec"
    ]
    assert diff_specs(first, first, from_revision=1, to_revision=1).changes == []


def test_a_capped_diff_says_how_much_it_left_out_and_marks_a_cut_value() -> None:
    """What the model is shown is bounded; the count of what is not shown comes back with it."""
    rows = [{"solvent": f"S{i}", "yield": i} for i in range(10)]
    edited = [{"solvent": f"S{i}", "yield": i + 1} for i in range(10)]
    diff = diff_specs(_table(rows), _table(edited), from_revision=1, to_revision=2)
    kept, left_out = capped(diff, max_changes=3, max_chars=300)
    assert len(kept.changes) == 3 and left_out == 7
    long = diff_specs(
        _spec({"kind": "document", "markdown": "a"}),
        _spec({"kind": "document", "markdown": "b" * 500}),
        from_revision=1,
        to_revision=2,
    )
    cut, _ = capped(long, max_changes=5, max_chars=50)
    assert len(cut.changes[0].after) <= 50 and cut.changes[0].after.endswith("[cut]")


def test_a_csv_export_neutralises_a_formula_and_leaves_a_number_alone() -> None:
    """`=HYPERLINK(...)` arrives as text; `-40` arrives as the number it is."""
    spec = _table(
        [
            {"solvent": '=HYPERLINK("http://x","click")', "yield": -40},
            {"solvent": "@SUM(1+9)", "yield": 2.5},
        ],
        [{"key": "solvent", "label": "+Solvent"}, {"key": "yield", "label": "Yield"}],
    )
    body = render_export(spec, "csv")
    assert body is not None
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["'+Solvent", "Yield"]
    assert rows[1] == ['\'=HYPERLINK("http://x","click")', "-40"]
    assert rows[2] == ["'@SUM(1+9)", "2.5"]

    structures = _spec(
        {"kind": "structures", "items": [{"smiles": "CCO", "label": "=cmd|' /C calc'!A0"}]}
    )
    structure_rows = list(csv.reader(io.StringIO(render_export(structures, "csv") or "")))
    assert structure_rows[1][1].startswith("'=")


def test_each_kind_offers_exactly_its_formats() -> None:
    """A pair the kind does not offer is `None` (the route's 404), never a wrong-shaped file."""
    document = _spec({"kind": "document", "markdown": "# Plan"})
    assert render_export(document, "md") == "# Plan\n"
    assert render_export(document, "csv") is None
    table = _table([{"solvent": "THF|DMF", "yield": 1}])
    assert render_export(table, "md") == "| Solvent | Yield |\n| --- | --- |\n| THF\\|DMF | 1 |\n"
    structures = _spec(
        {"kind": "structures", "items": [{"smiles": "CCO", "label": "ethanol\nabs"}]}
    )
    assert render_export(structures, "smi") == "CCO\tethanol abs\n"
    chart = _spec(
        {
            "kind": "chart",
            "chart": "scatter",
            "x_label": "T (C)",
            "y_label": "yield (%)",
            "series": [{"name": "A", "x": [60, 80], "y": [70, 76]}],
        }
    )
    assert list(csv.reader(io.StringIO(render_export(chart, "csv") or ""))) == [
        ["series", "T (C)", "yield (%)"],
        ["A", "60", "70"],
        ["A", "80", "76"],
    ]
    pinned = _spec({"kind": "result", "result_ref": "b" * 64})
    assert all(render_export(pinned, fmt) is None for fmt in ("md", "csv", "smi"))


def test_an_export_filename_cannot_carry_a_header_break() -> None:
    """The title is typed by a person and reaches `Content-Disposition`; only safe bytes survive."""
    name = export_filename('Plan "v2"\r\nX-Evil: 1', "xb-0123456789abcdef", 3, "md")
    assert name == "Plan-v2-X-Evil-1-xb-0123456789abcdef-r3.md"
