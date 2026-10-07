r"""The one Markdown table this system renders, and the rules it renders by.

- Escaping is correctness: a `|` in a cell renders as more table and a newline ends the row, which
  shifts values under the wrong heading or forges whole rows. `render_table` places caller values
  strictly as cells. The backslash is escaped before the pipe, or GFM eats it (`x\|y` -> `x|y`).
- An empty cell is spelled `MISSING`, once; `""` is converted, so `drop_empty_columns` can detect
  empty columns and absence never reads as a measured zero.
- No width padding: readers are a renderer and a model, and padding would make re-synthesised
  notes diff on whitespace.
- No zero-row special case: a header with no rows means "asked, nothing came back", and callers
  that mean "no table" decide that themselves.
"""

from __future__ import annotations

from collections.abc import Sequence

#: What a table cell shows when the record is silent. One spelling, for `drop_empty_columns`.
MISSING = "—"

#: The delimiter-row spelling per alignment character. Right alignment is for the columns a reader
#: compares down rather than reads across — counts, durations, deltas — and **ten** of the twenty
#: tables want it, which is why this is the one option `render_table` takes.
_ALIGNMENT_RULES = {"l": "---", "r": "---:"}


def placeable(text: str) -> str:
    r"""One cell's text, unable to add structure to the grid it is placed in.

    A `|` or newline in free text this system does not write (ELN fields, tool results, exception
    messages) would add cells or rows, forging evidence in an artifact read comparatively. The text
    is
    preserved: backslash escaped first so escaping the pipe cannot consume one, and whitespace runs
    collapsed because a cell is one line.
    """
    return " ".join(text.split()).replace("\\", "\\\\").replace("|", r"\|")


def render_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], *, align: str | None = None
) -> str:
    """Render a Markdown table as one block, with no trailing newline.

    `align` is one character per column — `l` or `r` — or `None` for all-left. A row whose length
    does not match the headers raises rather than silently shifting columns.
    """
    width = len(headers)
    if align is None:
        align = "l" * width
    if len(align) != width or any(character not in _ALIGNMENT_RULES for character in align):
        raise ValueError(f"align {align!r} must be {width} character(s) of 'l' or 'r'")
    for index, row in enumerate(rows):
        if len(row) != width:
            raise ValueError(f"row {index} has {len(row)} cell(s), headers declare {width}")
    lines = [
        f"| {' | '.join(placeable(header) for header in headers)} |",
        f"| {' | '.join(_ALIGNMENT_RULES[character] for character in align)} |",
        *(f"| {' | '.join(placeable(cell) or MISSING for cell in row)} |" for row in rows),
    ]
    return "\n".join(lines)
