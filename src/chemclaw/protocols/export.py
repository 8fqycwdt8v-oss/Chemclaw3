"""The run sheet as a CSV a chemist can open, paste into a plate reader, or hand to an instrument.

CSV rather than a spreadsheet format: instrument software, LIMS imports and workbooks all read
it, and `openpyxl` is deliberately kept out of the chat process's import graph. The row order
is the run order `render.run_sheet_rows` decides (randomised when there is a layout); a CSV
that re-sorted would undo it.
"""

import csv
import io
import re
from urllib.parse import quote

from chemclaw.protocols.models import ExperimentDesign
from chemclaw.protocols.render import ArmRow, run_sheet_rows

#: The columns every run sheet carries, in reading order. Factor levels are appended per design.
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


#: What a `design_id` may contribute to a downloaded filename; everything else becomes `_`. The
#: id reaches a `Content-Disposition` header from a path parameter, so it is sanitised here
#: whatever the minting function promises.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def run_sheet_filename(design_id: str, revision: int) -> str:
    """The name a saved run sheet carries: the design, the revision, and what it is.

    The revision is in the name because a printed sheet outlives the read, and two revisions of one
    plate must not share a filename.
    """
    return f"{_FILENAME_SAFE.sub('_', design_id)}-r{revision}-run-sheet.csv"


def run_sheet_path(design_id: str, revision: int) -> str:
    """Where this sheet is fetched from, derived here because the artefact is this module's.

    Both the agent (which quotes the path) and `api.routes.protocols` (which serves it) read it from
    here; `agent -> api` is not an allowed import, so this is the one place both can reach.
    """
    return f"/protocols/{quote(design_id, safe='')}/run-sheet.csv?revision={revision}"


#: The characters that make a spreadsheet treat a cell as a formula rather than text. Quoting does
#: not prevent it (quotes are stripped before the parse); tab and CR count as leading whitespace.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _is_number(text: str) -> bool:
    """True where the cell is a plain number, so a negative temperature is left exactly as it is."""
    try:
        float(text)
    except ValueError:
        return False
    return True


def csv_cell(value: object) -> str:
    """One value as a cell: empty for absent, and never in exponent form inside laboratory range.

    The one formula-injection guard for every CSV this system serves (`exhibits/export.py` uses
    it too). `None` becomes `""`, never `"None"`. A *text* cell opening with a formula trigger is
    prefixed with `'`, since design fields are untrusted free text. Numbers are never prefixed:
    `-40` and `+2.5` are ordinary values a LIMS import must receive intact.
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

    Written through `csv.writer` (`QUOTE_MINIMAL`, CRLF) because levels, solvents and notes may
    contain commas, quotes or newlines.

    Args:
        design: The design to export. A design with no arms yields the header alone.

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
                *(csv_cell(getattr(row, column)) for column in _FIXED),
                *(_level(row, name) for name in factors),
            ]
        )
    return buffer.getvalue()


def _factor_headers(factors: list[str]) -> list[str]:
    """Column names for the factors, disambiguated where one collides with a fixed column.

    A solvent screen's factor is called `solvent`, which is also a `_FIXED` column; duplicate
    headers make readers disagree about which is real. Colliding names are suffixed (not refused,
    not all renamed), so a non-colliding factor keeps the name the chemist wrote.
    """
    return [f"{name} (factor)" if name in _FIXED else name for name in factors]


def _level(row: ArmRow, factor: str) -> str:
    """This arm's level for one factor, or empty where the design does not set it."""
    return csv_cell(row.levels.get(factor, ""))
