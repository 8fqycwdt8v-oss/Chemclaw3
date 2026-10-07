"""An artefact as a file a chemist can keep: Markdown, CSV, a SMILES list or an XYZ geometry.

`render_export` alone decides which formats a kind offers; an unsupported pair is a 404. SDF and SVG
are produced client-side, so every server export is plain text. `resolve_export` is the async entry
for a geometry citing a calculation artifact; `render_export` is the pure half.

Every CSV cell goes through `protocols.export.csv_cell`, the one formula-injection guard: a cell
opening with `=`, `+`, `-`, `@`, tab or CR is prefixed so a spreadsheet reads it as text.
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
    # Text, never `text/html`: model-written markup served from this origin would run with the front
    # door's cookies. It runs only in the UI's sandbox origin, or from disk if a chemist opens the
    # saved file.
    "html": "text/plain; charset=utf-8",
}

#: What a filename may carry of a title; everything else becomes `-`, because the name reaches a
#: `Content-Disposition` header and a header built from text somebody typed is a splitting site.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def render_export(spec: Spec, fmt: str) -> str | None:
    """The artefact as a file of format `fmt`, or `None` when its kind does not offer that format.

    Offered: document as `md`; table as `csv` or `md`; structures as `smi` or `csv`; chart as `csv`;
    html as `html` (served as plain text); geometry as `xyz` when inline. A pinned result and a link
    have no file. The spec arrives resolved; a binding that no longer resolves is an empty cell.
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

    `None` when the kind does not offer `fmt`, or the cited geometry is gone or over the download
    cap; the route answers 404.
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

    Runs outside `[A-Za-z0-9._-]` become one `-` and edge dots and dashes are stripped, so no quote,
    line break or path separator reaches the header.
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
