"""The artefact spec: what each kind accepts, what it refuses, and the caps a write is held to.

`exhibits.models` is the one validator both writers go through (the agent's `spec` and a browser's
JSON body). Driven on `parse_spec` and `require_writable`, the functions tools and routes call.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.exhibits.models import (
    EXHIBIT_ID,
    ChartSpec,
    DocumentSpec,
    ExhibitRef,
    InvalidExhibit,
    TableSpec,
    new_exhibit_id,
    parse_spec,
    require_writable,
    spec_bytes,
    spec_json,
)

_TABLE: dict[str, Any] = {
    "kind": "table",
    "columns": [
        {"key": "solvent", "label": "Solvent"},
        {"key": "yield", "label": "Yield", "unit": "%"},
    ],
    "rows": [{"solvent": "THF", "yield": 76}, {"solvent": "2-MeTHF", "yield": 81.5}],
}


def _writable(raw: dict[str, Any], title: str = "Screen") -> None:
    """Parse and write-check, as both writers do."""
    require_writable(parse_spec(raw), title=title, change_note="")


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "document", "markdown": "# Plan\n\n1. Degas."},
        _TABLE,
        {
            "kind": "structures",
            "items": [{"smiles": "c1ccccc1O", "label": "phenol", "props": {"pka": 9.95}}],
        },
        {
            "kind": "chart",
            "chart": "bar",
            "x_label": "solvent",
            "y_label": "yield (%)",
            "series": [{"name": "run 1", "x": ["THF", "DMF"], "y": [76, 64]}],
        },
        {"kind": "result", "result_ref": "a" * 64, "tool": "screen_hazards"},
        {"kind": "link", "target": "protocol", "id": "design-0123456789ab"},
        {"kind": "html", "html": "<p>76 %</p>", "height": 480},
    ],
)
def test_every_kind_round_trips_through_its_stored_json(raw: dict[str, Any]) -> None:
    """Each kind parses, passes the write check, and dumps back to what was sent."""
    spec = parse_spec(raw)
    require_writable(spec, title="t", change_note="")
    assert parse_spec(spec_json(spec)) == spec


@pytest.mark.parametrize(
    ("raw", "names"),
    [
        ({"kind": "poster", "markdown": "x"}, "kind"),
        ({"kind": "document", "markdown": "x", "html": "<b>"}, "html"),
        ({"markdown": "x"}, "kind"),
        ("a table", "spec"),
        ({**_TABLE, "rows": [{"solvent": "THF", "pressure": 2}]}, "pressure"),
        ({**_TABLE, "columns": [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}]}, "unique"),
        ({**_TABLE, "rows": [{"solvent": "THF", "yield": True}]}, "yield"),
        ({**_TABLE, "columns": []}, "columns"),
        ({"kind": "structures", "items": []}, "items"),
        ({"kind": "result", "result_ref": "not-a-hash"}, "result_ref"),
        ({"kind": "link", "target": "calculation", "id": "x"}, "target"),
        ({"kind": "html", "html": "<p>x</p>", "height": 0}, "height"),
        ({"kind": "html", "html": "<p>x</p>", "height": "480"}, "height"),
        ({"kind": "html", "markdown": "x"}, "html"),
    ],
)
def test_a_spec_that_does_not_fit_its_kind_is_refused_naming_where(raw: object, names: str) -> None:
    """Refused with a worded error that points at the field, so one retry can fix it."""
    with pytest.raises(InvalidExhibit, match=names):
        parse_spec(raw)


def test_a_boolean_is_not_a_number_and_nan_is_not_storable() -> None:
    """`true` would read as 1 under lax coercion, and `jsonb` cannot hold NaN."""
    with pytest.raises(InvalidExhibit):
        parse_spec({**_TABLE, "rows": [{"yield": True}]})
    with pytest.raises(InvalidExhibit):
        parse_spec({**_TABLE, "rows": [{"yield": float("nan")}]})
    # An integer stays an integer, so a stored table reads back exactly as it was written.
    assert spec_json(parse_spec(_TABLE))["rows"][0]["yield"] == 76


def test_a_chart_pairs_its_points_and_takes_text_x_only_on_bars() -> None:
    """Unequal x and y, or a category axis on a line chart, are refused; a bar chart takes text."""
    series = {"name": "s", "x": [1, 2], "y": [3]}
    with pytest.raises(InvalidExhibit, match="2 x values and 1 y values"):
        parse_spec(
            {"kind": "chart", "chart": "line", "x_label": "t", "y_label": "T", "series": [series]}
        )
    text_x = {"name": "s", "x": ["THF"], "y": [1]}
    with pytest.raises(InvalidExhibit, match="only a 'bar' chart"):
        parse_spec(
            {
                "kind": "chart",
                "chart": "scatter",
                "x_label": "a",
                "y_label": "b",
                "series": [text_x],
            }
        )
    bar = parse_spec(
        {"kind": "chart", "chart": "bar", "x_label": "a", "y_label": "b", "series": [text_x]}
    )
    assert isinstance(bar, ChartSpec)


def test_a_smiles_rdkit_cannot_read_whole_is_refused_on_write() -> None:
    """The shape check passes any string; the write check is where RDKit reads every structure."""
    raw = {"kind": "structures", "items": [{"smiles": "CCO"}, {"smiles": "C1CC"}]}
    spec = parse_spec(raw)
    with pytest.raises(InvalidExhibit, match=r"items\[1\]\.smiles"):
        require_writable(spec, title="t", change_note="")


def test_the_caps_refuse_a_write_and_name_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows, structures, points and bytes each refuse past their configured cap."""
    monkeypatch.setattr(settings, "exhibit_max_rows", 1)
    with pytest.raises(InvalidExhibit, match="1-row cap"):
        _writable(_TABLE)
    monkeypatch.setattr(settings, "exhibit_max_structures", 1)
    with pytest.raises(InvalidExhibit, match="1-structure cap"):
        _writable({"kind": "structures", "items": [{"smiles": "C"}, {"smiles": "CC"}]})
    monkeypatch.setattr(settings, "exhibit_max_points", 2)
    with pytest.raises(InvalidExhibit, match="2-point cap"):
        _writable(
            {
                "kind": "chart",
                "chart": "line",
                "x_label": "t",
                "y_label": "y",
                "series": [{"name": "s", "x": [1, 2, 3], "y": [1, 2, 3]}],
            }
        )
    document = parse_spec({"kind": "document", "markdown": "x" * 100})
    monkeypatch.setattr(settings, "exhibit_max_spec_bytes", spec_bytes(document) - 1)
    with pytest.raises(InvalidExhibit, match="byte cap"):
        require_writable(document, title="t", change_note="")


def test_a_cap_lowered_later_does_not_make_a_stored_spec_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caps are a write check: what a deployment already holds still parses after a cut."""
    stored = spec_json(parse_spec(_TABLE))
    monkeypatch.setattr(settings, "exhibit_max_rows", 1)
    assert isinstance(parse_spec(stored), TableSpec)


def test_text_no_column_can_store_is_refused_rather_than_stripped() -> None:
    """A NUL in a cell, a key or the title is a 422 on both backends, never a driver error."""
    with pytest.raises(InvalidExhibit, match="NUL"):
        _writable({"kind": "document", "markdown": "a\x00b"})
    with pytest.raises(InvalidExhibit, match="NUL"):
        _writable({**_TABLE, "rows": [{"solvent": "TH\x00F"}]})
    with pytest.raises(InvalidExhibit, match="title"):
        _writable({"kind": "document", "markdown": "ok"}, title="  ")
    # Line breaks and tabs are ordinary document text.
    _writable({"kind": "document", "markdown": "a\n\tb\r\n"})


def test_a_minted_id_has_the_contract_shape() -> None:
    """`xb-` and sixteen hex digits, random per call."""
    first, second = new_exhibit_id(), new_exhibit_id()
    assert EXHIBIT_ID.match(first) and EXHIBIT_ID.match(second)
    assert first != second


def test_a_reference_names_an_id_of_the_minted_shape() -> None:
    """`ExhibitRef` holds a chemist's message to the `xb-` shape before anything looks it up."""
    assert ExhibitRef(exhibit_id=new_exhibit_id()).revision == 0
    for bad in ("xb-1", "XB-0000000000000000", "../etc/passwd", "xb-0000000000000000 "):
        with pytest.raises(ValidationError):
            ExhibitRef(exhibit_id=bad)


def test_a_document_is_the_markdown_and_nothing_else() -> None:
    """The kind most artefacts will be (the ADR's 15.3% of answers) carries one field."""
    assert parse_spec({"kind": "document", "markdown": ""}) == DocumentSpec(
        kind="document", markdown=""
    )
