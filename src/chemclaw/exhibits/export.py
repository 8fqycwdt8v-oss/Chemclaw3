"""An artefact as a file a chemist can keep: Markdown, CSV, a SMILES list or an XYZ geometry.

**Which formats a kind offers is decided in one function**, `render_export`, and the route asks it:
a kind/format pair it does not render is a 404 rather than a file that is the wrong shape.
SDF and SVG are produced on the client from what it already draws (RDKit WASM, the chart's own DOM),
so the server takes no new rendering path and an export is always plain text. A geometry that cites
a calculation artifact is the one export whose bytes are not in the spec, so `resolve_export` is the
async entry the route calls and `render_export` the pure half every other kind is.

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

from chemclaw.exhibits.models import (
    ChartSpec,
    DocumentSpec,
    GeometrySpec,
    HtmlSpec,
    Spec,
    StructuresSpec,
    TableSpec,
)
from chemclaw.exhibits.sources import geometry_xyz
from chemclaw.protocols.export import csv_cell

#: The media type each format is served as.
MEDIA_TYPES: dict[str, str] = {
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "smi": "chemical/x-daylight-smiles; charset=utf-8",
    "xyz": "chemical/x-xyz; charset=utf-8",
    # **Text, never `text/html`, and the extension does not change that.** A page the model wrote
    # is untrusted markup; served as HTML from this origin it would run with the front door's
    # cookies and its `connect-src`. The file a chemist saves opens in their browser from disk if
    # they choose — the one place it may run besides the UI's sandbox origin
    # (`D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves`).
    "html": "text/plain; charset=utf-8",
}

#: What a filename may carry of a title; everything else becomes `-`, because the name reaches a
#: `Content-Disposition` header and a header built from text somebody typed is a splitting site.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def render_export(spec: Spec, fmt: str) -> str | None:
    """The artefact as a file of format `fmt`, or `None` when its kind does not offer that format.

    Offered: a document as `md`; a table as `csv` or `md`; a structures panel as `smi` or `csv`; a
    chart as `csv`; an html page as `html` — served as plain text (`MEDIA_TYPES`). A spec arrives
    resolved: every binding is its value, and one that no longer resolves is an empty cell. A
    pinned result and a link have no file of their own — the pane opens what they point at — so
    every format answers `None` for them. A geometry is `xyz` when it carries its
    block inline; one citing a calculation artifact needs a read, which is `resolve_export`'s.
    """
    if isinstance(spec, GeometrySpec) and fmt == "xyz" and spec.xyz is not None:
        return _with_newline(spec.xyz)
    if isinstance(spec, HtmlSpec) and fmt == "html":
        return _with_newline(spec.html)
    if isinstance(spec, DocumentSpec) and fmt == "md":
        return _with_newline(spec.markdown)
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
            # `SMILES<tab>label`, the SMILES-file layout every reader expects. A structure whose
            # bound SMILES no longer resolves has no line to give.
            return "".join(
                f"{item.smiles}\t{_one_line(item.label)}\n"
                for item in spec.items
                if isinstance(item.smiles, str)
            )
        if fmt == "csv":
            names = list(dict.fromkeys(name for item in spec.items for name in item.props))
            rows = [
                [item.smiles, item.label, *(item.props.get(name) for name in names)]
                for item in spec.items
            ]
            return _csv(["smiles", "label", *names], rows)
    if isinstance(spec, ChartSpec) and fmt == "csv":
        # Long form, one row per point named by its series, so series of any length fit. A series
        # whose binding no longer resolves has no points to give, so it gives none.
        points = [
            [series.name, x, y]
            for series in spec.series
            if isinstance(series.x, list) and isinstance(series.y, list)
            for x, y in zip(series.x, series.y, strict=True)
        ]
        return _csv(["series", spec.x_label or "x", spec.y_label or "y"], points)
    return None


async def resolve_export(spec: Spec, fmt: str) -> str | None:
    """`render_export`, plus the one format whose text is read from the calc artifact store.

    `None` both when the kind does not offer `fmt` and when what a geometry cites is gone, or is
    an artifact now over the download cap — the route answers 404 to each, because none has a file
    to give. A `structure_id` the reader's view already resolved arrives here as inline `xyz`.
    """
    if isinstance(spec, GeometrySpec) and fmt == "xyz" and spec.xyz is None:
        text = await geometry_xyz(spec)
        return None if text is None else _with_newline(text)
    return render_export(spec, fmt)


def export_filename(title: str, exhibit_id: str, revision: int, fmt: str) -> str:
    """The saved file's name: the title, the revision, and the id that makes it unique."""
    return f"{safe_filename(title, 'artefact')}-{exhibit_id}-r{revision}.{fmt}"


def safe_filename(text: str, fallback: str) -> str:
    """`text` cut to what a `Content-Disposition` filename may carry, or `fallback` if nothing is.

    Every run of characters outside `[A-Za-z0-9._-]` becomes one `-`, and leading or trailing dots
    and dashes go, so neither a quote nor a line break nor a path separator reaches the header.
    """
    return _FILENAME_SAFE.sub("-", text).strip("-.")[:60] or fallback


def _csv(header: Sequence[object], rows: Iterable[Sequence[object]]) -> str:
    """A CSV with every cell guarded, CRLF line ends as a spreadsheet expects."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([csv_cell(value) for value in header])
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    return buffer.getvalue()


def _with_newline(text: str) -> str:
    """`text` ending in exactly the line break a text file is expected to end in."""
    return text if text.endswith("\n") else text + "\n"


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
