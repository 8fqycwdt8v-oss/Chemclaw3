"""The run sheet as a CSV a chemist can open, paste into a plate reader, or hand to an instrument.

**Three places claimed this existed and nothing produced it.** `protocols/README.md` lists "the
CSV export" among the five things written once because there is one design shape,
`protocols/models.py` repeats it in the same breath, and `ProtocolArm.arm_id`'s own comment calls
itself "the CSV row key". Measured before this file: `grep -rn "csv" src/chemclaw/protocols/`
returned three prose hits and no executable line. (That sentence went on "and the only `import
csv` in `src/` is in ingest readers", which was wrong when written — there were three, and
`cli/live_data.py` is a CLI. `D-2026-09-15` corrects it in the ADR that made the claim; this is the
docstring that made it too.) That is the shape
`D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` names
one seam over — a declared artefact with no producer — and the fix is the same: build it, or stop
claiming it.

**It is `csv` and not a spreadsheet, and that is a decision rather than a first step.** A plate map
goes into instrument software, a LIMS import, or a column in someone's own workbook, and every one
of those reads CSV. `openpyxl` is in this tree already but arrives *transitively* through `drfp`
with no version pin and no declaration in `[project.dependencies]`, and four modules plus
`tests/test_datasource_isolation.py` exist to keep it out of the chat process's import graph. A
deliverable format is not a reason to undo that.

**The row order is the run order**, which `render.run_sheet_rows` already decides — randomised
against session drift when there is a layout, arm order otherwise. A CSV that re-sorted would be a
second opinion about the thing the layout exists to fix.
"""

import csv
import io
import re
from urllib.parse import quote

from chemclaw.protocols.models import ExperimentDesign
from chemclaw.protocols.render import ArmRow, run_sheet_rows

#: The columns every run sheet carries, in reading order: what identifies the row, where it sits,
#: then the conditions. Factor levels are appended per design, because they are the one part of the
#: shape a design decides rather than the model.
_FIXED: tuple[str, ...] = (
    "arm_id",
    "well",
    "run_order",
    "temperature_c",
    "time_h",
    "solvent",
    "atmosphere",
    "pressure_bar",
    "concentration_molar",
    "ph",
    "control",
    "replicate_of",
    "note",
)


#: What a `design_id` may contribute to a downloaded filename. Everything else becomes `_`.
#: `design_id_for` mints `design-<12 hex>`, so this removes nothing a real id carries — it is here
#: because a `design_id` reaches a `Content-Disposition` header, and a header built out of a path
#: parameter is a response-splitting site whatever the minting function promises. A store that
#: 404s an unknown id is not that guarantee either: it bounds which ids resolve, not which
#: characters an id that does resolve may hold, because a revision is filed under whatever the
#: path said.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def run_sheet_filename(design_id: str, revision: int) -> str:
    """The name a saved run sheet carries: the design, the revision, and what it is.

    **The revision is in the name because the sheet outlives the read.** It is printed and carried
    to a bench, where the design has already moved on; a filename naming only the design puts two
    different plates under one name on the same laptop, and the case where that matters most is
    exactly the one where the arm counts differ.
    """
    return f"{_FILENAME_SAFE.sub('_', design_id)}-r{revision}-run-sheet.csv"


def run_sheet_path(design_id: str, revision: int) -> str:
    """Where this sheet is fetched from — derived here because the artefact is this module's.

    The agent names this path when it hands a chemist a protocol, and `api.routes.protocols` serves
    it; neither may own the spelling, because two copies of a URL drift and the one the model
    quotes is the one nobody tests. `api -> protocols` is an allowed import direction and
    `agent -> api` is not, so this is the only place both can read.
    """
    return f"/protocols/{quote(design_id, safe='')}/run-sheet.csv?revision={revision}"


#: The characters that make a spreadsheet treat a cell as a formula rather than as text.
#:
#: Excel, LibreOffice and Google Sheets all evaluate a cell opening with one of these, and quoting
#: does not prevent it — the quotes are stripped at import and the text is then parsed. Tab and CR
#: are here because both are treated as leading whitespace before the trigger.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _is_number(text: str) -> bool:
    """True where the cell is a plain number, so a negative temperature is left exactly as it is."""
    try:
        float(text)
    except ValueError:
        return False
    return True


def _cell(value: object) -> str:
    """One value as a cell: empty for absent, and never in exponent form inside laboratory range.

    `None` becomes `""` rather than `"None"` — a spreadsheet reading the literal string `None` in a
    numeric column is the kind of thing that survives all the way to somebody weighing it out.

    **A text cell opening with a formula trigger is prefixed with `'`, and that case was neither
    handled nor argued when this shipped.** `run_sheet_csv`'s docstring enumerated the hazards as
    "a comma, a quote or a newline" and `tests/test_protocol_export.py`'s as "a comma, a quote, a
    newline, an absent number, and a column order" — the one case that *executes* was in neither
    list. Driven at the time, a `solvent` of `@SUM(1+9)*cmd|'/C calc'!A0` and a `note` of
    an `=HYPERLINK(...)` naming an attacker's host both reached a spreadsheet as live formulas, and
    `QUOTE_MINIMAL` does nothing about it because quoting is stripped before the parse. Those
    fields are free text on a design drafted from tool results and then edited over
    `POST /protocols/{id}/revisions`, which is exactly the text this repository treats as untrusted
    everywhere else.

    **Numbers are never prefixed, which is the whole reason the check is not on the trigger alone.**
    `-40` is an ordinary temperature and `+2.5` an ordinary equivalents figure; prefixing either
    would corrupt the value for the LIMS import this export exists to feed, trading a real hazard
    for a certain one. So the prefix lands only where the cell is text *and* opens with a trigger,
    which leaves every numeric column byte-identical and every formula inert. The cost is stated
    rather than hidden: such a cell arrives in a spreadsheet showing a leading apostrophe, and a
    strict machine parser sees one character it did not write — for a value that was going to be
    executable code otherwise.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    text = str(value)
    if text.startswith(_FORMULA_TRIGGERS) and not _is_number(text):
        return f"'{text}"
    return text


def run_sheet_csv(design: ExperimentDesign) -> str:
    """The design's arms as CSV, one row per arm, in run order.

    Written through `csv.writer` rather than by joining commas, because a factor level, a solvent
    name or a note legitimately contains a comma, a quote or a newline — and a run sheet that
    silently shifts every column after a reagent called "toluene, anhydrous" is worse than no run
    sheet. `QUOTE_MINIMAL` with CRLF line endings is what a spreadsheet and an instrument both
    expect.

    Args:
        design: The design to export. A design with no arms yields the header alone, which is a
            true statement about it rather than an error: an ask that has not been drafted into a
            protocol has no runs to sheet.

    Returns:
        The CSV text, header first.
    """
    rows = run_sheet_rows(design)
    factors = [factor.name for factor in design.factors]
    headers = _factor_headers(factors)
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([*_FIXED, *headers])
    for row in rows:
        writer.writerow(
            [
                *(_cell(getattr(row, column)) for column in _FIXED),
                *(_level(row, name) for name in factors),
            ]
        )
    return buffer.getvalue()


def _factor_headers(factors: list[str]) -> list[str]:
    """Column names for the factors, disambiguated where one collides with a fixed column.

    A solvent screen is the canonical HTE design and its factor is called `solvent` — which is also
    a `_FIXED` column, so the header shipped with two columns of that name. Driven: the first (from
    `Setpoints.solvent`) is empty because the value varies, and readers disagree about which is
    real — `header.index("solvent")` and `pandas.read_csv` both take the empty one, while
    `dict(zip(header, row))` takes the factor. A LIMS import keyed by column name got blanks for
    the factor that is the point of the plate. The same held for a factor named `temperature_c`,
    `ph` or `note`.

    Suffixed rather than refused, because refusing would reject the commonest design this export
    exists for, and suffixed rather than renamed wholesale so a factor whose name collides with
    nothing keeps the name a chemist wrote.
    """
    return [f"{name} (factor)" if name in _FIXED else name for name in factors]


def _level(row: ArmRow, factor: str) -> str:
    """This arm's level for one factor, or empty where the design does not set it."""
    return _cell(row.levels.get(factor, ""))
