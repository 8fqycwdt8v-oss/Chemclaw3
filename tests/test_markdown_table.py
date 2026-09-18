r"""The one Markdown table renderer, and the two things it is for.

`core.markdown` exists because ~21 places in this tree built a table out of f-strings and agreed
only on the pipe character. Most of what it consolidated was style — a delimiter row spelled
`|---|` against `| --- |`, a header cased one way here and another there — and none of that is
worth a test. Two things are, because they are the reason the consolidation was not a library:

* **A cell can fill a cell and never add one.** Three of the twenty-one sites escaped their cells
  and the rest did not, so a `|` arriving from an ELN field, a tool result quoted into a probe
  report or an exception's message rendered as *more table* — silently shifting every value after
  it under the wrong heading. `tests/test_optimization.py` holds that for the campaign note, where
  the rule was first written; this file holds it for the renderer and for the report writers that
  had no rule at all.
* **A cell says the same thing the record said.** Escaping the pipe without escaping the backslash
  first does not break the column count — a GFM splitter treats any `|` behind a backslash as
  escaped whatever precedes it — but it eats a backslash the source wrote. That is the half the
  campaign note's own escaping got wrong and the run sheet's got right, so it is asserted rather
  than left to whichever call site is read next.

Cells are split the way `tests/test_optimization.py` splits them: on *unescaped* pipes only, since
counting an escaped one as a boundary is the misreading the escaping exists to prevent.
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
    """A header with no rows says the question was asked.

    "Asked and nothing came back" is a different claim from "not asked", and only the caller knows
    which is true — so the renderer does not decide it, and `protocols.render._table` and
    `cli.live_data.report` each say their own version in their own words.
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
    """A blank cell reads as a measured nothing; `MISSING` reads as a record that is silent.

    One spelling, because `memory.comparison.drop_empty_columns` decides a column is empty by
    comparing rendered cells against it — a second spelling would make a column of blanks survive
    the check that exists to remove it.
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
    r"""`x\|y` must come back as `x\|y`, not as `x|y`.

    Escaping the pipe alone leaves the source's own backslash adjacent to the escape this adds, and
    a GFM reader spends it on the escape: measured through markdown-it-py's GFM tables, pipe-only
    escaping rendered `x\|y` as `x|y` and `\\` as `\`. The column count was never the casualty —
    the text was, which is the property the escaping was written to protect.
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
    """A connector's own output reaches a report cell, and it is not this system's text.

    `cli/live_storm.py` puts the first 70 characters of a tool's own result into an `observed`
    field, and `cli/live_jobs.py` renders that field in a three-column table. Measured before this
    consolidation: ``result[0]='a | b'`` produced a row of **4** cells under a header declaring 3.
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
    """The one report that always escaped: `precision`'s provenance carries set-cardinality bars.

    Kept as a test of the shared renderer rather than of the deleted private `_cell`, and extended
    with the change this consolidation made deliberately — a metric with no unit renders `MISSING`
    rather than a blank, because a blank is a second spelling of absence.
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
