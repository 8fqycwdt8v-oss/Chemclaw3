r"""The one Markdown table renderer (`core.markdown`), and the two properties it guarantees.

* A cell can fill a cell and never add one: a `|` from an ELN field, a quoted tool result or an
  exception message must not shift later values under the wrong heading.
* A cell says what the record said: the backslash is escaped before the pipe, or a GFM reader
  consumes a backslash the source wrote.

Cells are split on unescaped pipes only, as in `tests/test_optimization.py`.
"""

from __future__ import annotations

import re

import pytest

from chemclaw.cli.live_data import Check as DataCheck
from chemclaw.cli.live_data import DataRun, Reach
from chemclaw.cli.live_data import report as data_report
from chemclaw.cli.live_jobs import Check as JobCheck
from chemclaw.cli.live_jobs import SmokeRun
from chemclaw.cli.live_jobs import report as jobs_report
from chemclaw.core.markdown import MISSING, placeable, render_table
from chemclaw.evals.harness import EvalReport, ScoredResult, render_report


def cells(line: str) -> list[str]:
    """One rendered row, split into the cells a Markdown reader would see."""
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]


def table_rows(text: str) -> list[list[str]]:
    """Every table row in a rendered document, as cells — the delimiter rows dropped."""
    return [
        cells(line)
        for line in text.splitlines()
        if line.startswith("|") and not set(line) <= set("| -:")
    ]


# --- the grid ---------------------------------------------------------------------------


def test_the_delimiter_row_carries_the_declared_alignment() -> None:
    """`align` is the one option this renderer takes, because ~10 report tables want `---:`."""
    assert render_table(["a", "b"], [], align="lr").splitlines()[1] == "| --- | ---: |"
    assert render_table(["a", "b"], []).splitlines()[1] == "| --- | --- |"


def test_a_header_with_no_rows_still_renders_its_header() -> None:
    """A header with no rows still renders its header, saying the question was asked.

    Whether "nothing came back" needs saying is the caller's decision, not the renderer's.
    """
    assert render_table(["a"], []) == "| a |\n| --- |"


@pytest.mark.parametrize(
    "align", ["l", "lll", "lx", "LR"], ids=["too-short", "too-long", "unknown", "wrong-case"]
)
def test_an_alignment_that_does_not_match_the_headers_raises(align: str) -> None:
    """A misdeclared alignment is a silent column shift in the delimiter row — raise instead."""
    with pytest.raises(ValueError, match="align"):
        render_table(["a", "b"], [["1", "2"]], align=align)


def test_a_row_that_does_not_match_the_headers_raises() -> None:
    """A row that does not match its headers raises rather than rendering.

    A short or long row shifts every cell after it, which is the defect this module exists to stop —
    arriving from the caller's own code rather than from its data.
    """
    with pytest.raises(ValueError, match="row 1 has 1 cell"):
        render_table(["a", "b"], [["1", "2"], ["3"]])


def test_an_empty_cell_is_the_one_spelling_of_absence() -> None:
    """An empty cell is the one spelling of absence.

    `memory.comparison.drop_empty_columns` compares rendered cells against it, so a second spelling
    would let a column of blanks survive.
    """
    assert cells(render_table(["a", "b"], [["", "  "]]).splitlines()[2]) == [MISSING, MISSING]


# --- escaping ---------------------------------------------------------------------------


def test_a_pipe_in_a_cell_cannot_add_a_cell() -> None:
    """The whole point. Three cells declared, three cells rendered, content intact."""
    rendered = render_table(["a", "b"], [["x | y | z", "9"]])
    assert cells(rendered.splitlines()[2]) == [r"x \| y \| z", "9"]
    assert len(cells(rendered.splitlines()[2])) == len(cells(rendered.splitlines()[0]))


def test_a_newline_in_a_cell_cannot_add_a_row() -> None:
    """A newline ends a row in Markdown, so a value carrying one renders as more table.

    The forged-row case `memory.comparison` measured: a value that looks like the rest of a row.
    """
    forged = "routine |\n| rxn-FORGED | 99 | 99 | best result on file"
    rendered = render_table(["run", "note"], [["rxn-1", forged]])
    assert len(rendered.splitlines()) == 3
    assert "rxn-FORGED" in cells(rendered.splitlines()[2])[1]


def test_a_backslash_survives_the_pipe_escaping() -> None:
    r"""`x\|y` comes back as `x\|y`, not `x|y`: the backslash survives the pipe escaping.

    Pipe-only escaping leaves the source backslash adjacent to the added escape, and a GFM reader
    spends it; the text, not the column count, is the casualty.
    """
    assert placeable(r"x\|y") == r"x\\\|y"
    assert placeable("\\") == "\\\\"


def test_whitespace_in_a_cell_collapses_to_one_line() -> None:
    """A cell is one line by construction.

    The alternative spelling of a newline inside one is HTML in a payload a model reads.
    """
    assert placeable(" a \n\t b  ") == "a b"


# --- the report writers that had no rule at all --------------------------------------------


def test_a_tool_result_quoted_into_a_job_report_cannot_shift_its_columns() -> None:
    """A tool result quoted into a job report cannot shift its columns.

    `cli/live_storm.py` puts a tool's own output into `observed`, which `cli/live_jobs.py` renders
    in a table; a `|` in it must stay inside its cell.
    """
    run = SmokeRun(workflow_id="wf-1", seconds=1.0)
    run.checks = [JobCheck(name="tool result", passed=True, observed="result[0]='a | b'")]
    header, row = table_rows(jobs_report(run))
    assert len(row) == len(header) == 3
    assert row[2] == r"result[0]='a \| b'"


def test_a_corpus_check_cannot_shift_the_columns_of_the_fidelity_report() -> None:
    """The same rule over `cli/live_data.py`, whose `observed` quotes what a dataset row held."""
    run = DataRun(seconds=1.0)
    run.reach = [Reach(dataset="d1", published=1, seeded=1, mapped=1, refused=0)]
    run.checks = [DataCheck(name="a | b", passed=False, observed="expected 1 | got 2")]
    rows = table_rows(data_report(run))
    assert all(len(row) == len(rows[0]) for row in rows[:2])
    assert rows[-1] == [r"a \| b", "**FAIL**", r"expected 1 \| got 2"]


def test_provenance_pipes_stay_inside_their_cell_in_the_eval_report() -> None:
    """Provenance pipes stay inside their cell in the eval report.

    `precision`'s provenance carries set-cardinality bars. A metric with no unit renders `MISSING`,
    since a blank would be a second spelling of absence.
    """
    report = EvalReport(
        case_set_version="v1",
        results=[
            ScoredResult(
                case_id="c1",
                result_metric="precision",
                value=0.5,
                unit=None,
                passed=None,
                provenance="|TP| / |TP ∪ FP|",
            )
        ],
    )
    header, row = table_rows(render_report(report))
    assert len(row) == len(header) == 6
    assert row[3] == MISSING
    assert row[5] == r"\|TP\| / \|TP ∪ FP\|"
