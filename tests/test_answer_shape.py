"""`chemclaw.evals.answer_shape` counts what it says it counts, on answers small enough to read.

The measurement backs a merged decision and one of its `Revisit when:` lines, so a detector that
silently stopped finding tables would turn the trigger off without anyone deciding to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chemclaw.evals.answer_shape import (
    Thresholds,
    main,
    measure,
    outcomes_in,
    report,
    structures,
    tables,
)

_TABLE = """Here is the screen.

| solvent | yield (%) |
|---|---:|
| THF | 71.5 |
| MeTHF | 64.0 |
| toluene | 12 |
| DMF | 88.25 |

Done."""


def test_a_table_needs_its_separator_row() -> None:
    """A pipe-led line is not a table until a separator follows the header."""
    assert [len(t) for t in tables(_TABLE)] == [6]
    assert tables("| not | a table |\n| just | pipes |") == []


def test_two_tables_in_one_answer_are_two() -> None:
    """Prose between tables ends the first; a table at the very end is still found."""
    answer = "| a |\n|---|\n| 1 |\n\ntext\n\n| b |\n|---|\n| 2 |\n| 3 |"
    assert [len(t) for t in tables(answer)] == [3, 4]


def test_structures_are_counted_once_by_canonical_form_and_need_heavy_atoms() -> None:
    """Two spellings of one molecule are one; `CO` and a code word are not structures."""
    answer = "`OCC` and `CCO` and `c1ccccc1` and `CO` and `predict_pka`"
    assert structures(answer, min_atoms=3) == {"CCO", "c1ccccc1"}


def test_table_figures_are_checked_only_where_the_run_recorded_verification() -> None:
    """A run without `verified_numbers` contributes figures but no verdict about them."""
    checked = {"answer": _TABLE, "verified_numbers": ["71.5", "12"], "tools_called": []}
    unchecked = {"answer": _TABLE, "tools_called": ["render_structure", "find_notes"]}
    c = measure([checked, unchecked])
    assert c["answers"] == 2
    assert c["with_table"] == 2
    assert c["with_big_table"] == 2
    assert c["table_figures"] == 8
    assert c["table_figures_checked"] == 4
    assert c["table_figures_verified"] == 2
    assert c["render_structure_calls"] == 1
    assert c["tool_calls"] == 2


def test_a_document_is_long_and_headed() -> None:
    """Length alone or headings alone is a reply, not a document."""
    body = "word " * 4000
    headed = f"## Plan\n{body}\n## Risks\n{body}"
    limits = Thresholds(document_tokens=600)
    assert measure([{"answer": headed}], limits)["document_shaped"] == 1
    assert measure([{"answer": body}], limits)["document_shaped"] == 0
    assert measure([{"answer": "## A\nshort\n## B\nshort"}], limits)["document_shaped"] == 0


def test_an_empty_answer_is_not_an_answer() -> None:
    """A turn that never answered is excluded rather than counted as a table-free answer."""
    assert measure([{"answer": ""}, {"answer": None}])["answers"] == 0


def _write(directory: Path, name: str, payload: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload))


def test_the_report_has_one_row_per_directory_of_outcomes_and_a_total(tmp_path: Path) -> None:
    """JSON that is not an outcome is skipped, and each outcome directory is one row."""
    _write(tmp_path / "run-a", "p1.json", {"probe": {}, "outcome": {"answer": _TABLE}})
    _write(tmp_path / "run-a", "grades.json", [1, 2, 3])
    _write(tmp_path / "run-b" / "nested", "p2.json", {"probe": {}, "outcome": {"answer": "hi"}})
    assert len(outcomes_in(tmp_path / "run-a")) == 1
    text = report([tmp_path])
    rows = [line for line in text.splitlines() if line.startswith("| ") and "set" not in line]
    assert len(rows) == 3
    assert rows[-1].startswith("| **all** | 2 | 1 (50.0%)")


def test_the_cli_prints_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The entry the decision's trigger names actually runs."""
    _write(tmp_path, "p.json", {"probe": {}, "outcome": {"answer": _TABLE}})
    assert main([str(tmp_path), "--min-rows", "5"]) == 0
    out = capsys.readouterr().out
    assert "| **all** | 1 | 1 (100.0%) | 0 (0.0%)" in out
