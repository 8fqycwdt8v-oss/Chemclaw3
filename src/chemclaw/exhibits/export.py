"""An artefact as a file a chemist can keep: Markdown, CSV or a SMILES list.

**Which formats a kind offers is decided in one function**, `render_export`, and the route asks it:
a kind/format pair it does not render is a 404 rather than a file that is the wrong shape.
SDF and SVG are produced on the client from what it already draws (RDKit WASM, the chart's own DOM),
so the server takes no new rendering path and an export is always plain text.

**Every CSV cell goes through `protocols.export.csv_cell`**, the one formula-injection guard this
system has: a cell that opens with `=`, `+`, `-`, `@`, a tab or a CR is prefixed so a spreadsheet
reads it as text, and a number is left exactly as it is. An artefact's cells are text the model or a
chemist wrote, often from tool output, which is the text this repository treats as untrusted.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence

from chemclaw.exhibits.models import ChartSpec, DocumentSpec, Spec, StructuresSpec, TableSpec
from chemclaw.protocols.export import csv_cell

#: The media type each format is served as.
MEDIA_TYPES: dict[str, str] = {
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "smi": "chemical/x-daylight-smiles; charset=utf-8",
}

#: What a filename may carry of a title; everything else becomes `-`, because the name reaches a
#: `Content-Disposition` header and a header built from text somebody typed is a splitting site.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def render_export(spec: Spec, fmt: str) -> str | None:
    """The artefact as a file of format `fmt`, or `None` when its kind does not offer that format.

    Offered: a document as `md`; a table as `csv` or `md`; a structures panel as `smi` or `csv`; a
    chart as `csv`. A pinned result and a link have no file of their own — the pane opens what they
    point at — so every format answers `None` for them.
    """
    if isinstance(spec, DocumentSpec) and fmt == "md":
        return spec.markdown if spec.markdown.endswith("\n") else spec.markdown + "\n"
    if isinstance(spec, TableSpec):
        header = [_with_unit(column.label or column.key, column.unit) for column in spec.columns]
        cells = [[row.get(column.key) for column in spec.columns] for row in spec.rows]
        if fmt == "csv":
            return _csv(header, cells)
        if fmt == "md":
            lines = [_pipe([_md_cell(label) for label in header]), _pipe(["---"] * len(header))]
            lines += [_pipe([_md_cell(value) for value in row]) for row in cells]
            return "\n".join(lines) + "\n"
    if isinstance(spec, StructuresSpec):
        if fmt == "smi":
            # `SMILES<tab>label`, the SMILES-file layout every reader expects.
            return "".join(f"{item.smiles}\t{_one_line(item.label)}\n" for item in spec.items)
        if fmt == "csv":
            names = list(dict.fromkeys(name for item in spec.items for name in item.props))
            rows = [
                [item.smiles, item.label, *(item.props.get(name) for name in names)]
                for item in spec.items
            ]
            return _csv(["smiles", "label", *names], rows)
    if isinstance(spec, ChartSpec) and fmt == "csv":
        # Long form, one row per point named by its series, so series of any length fit.
        points = [
            [series.name, x, y]
            for series in spec.series
            for x, y in zip(series.x, series.y, strict=True)
        ]
        return _csv(["series", spec.x_label or "x", spec.y_label or "y"], points)
    return None


def export_filename(title: str, exhibit_id: str, revision: int, fmt: str) -> str:
    """The saved file's name: the title, the revision, and the id that makes it unique."""
    stem = _FILENAME_SAFE.sub("-", title).strip("-.")[:60] or "artefact"
    return f"{stem}-{exhibit_id}-r{revision}.{fmt}"


def _csv(header: Sequence[object], rows: Iterable[Sequence[object]]) -> str:
    """A CSV with every cell guarded, CRLF line ends as a spreadsheet expects."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([csv_cell(value) for value in header])
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    return buffer.getvalue()


def _with_unit(label: str, unit: str) -> str:
    """A column header carrying its unit, the way a chemist writes one."""
    return f"{label} ({unit})" if unit else label


def _pipe(cells: Sequence[str]) -> str:
    """One Markdown table row."""
    return "| " + " | ".join(cells) + " |"


def _md_cell(value: object) -> str:
    """A value as a pipe-table cell: a pipe escaped, a line break flattened, absent as empty."""
    if value is None:
        return ""
    return _one_line(str(value)).replace("|", "\\|")


def _one_line(text: str) -> str:
    """`text` with its line breaks and tabs collapsed, so it cannot start a new record."""
    return " ".join(text.split())
