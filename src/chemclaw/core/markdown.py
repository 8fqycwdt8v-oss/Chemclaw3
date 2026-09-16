r"""The one Markdown table this system renders, and the rules it renders by.

**Twenty** places in this tree rendered a Markdown table before this module existed, across eleven
modules — every live probe report, the soak and leak fits, the eval report and its baseline
comparison, the run sheet, the campaign note and the turn-time protocol comparison. Two of the
twenty were named renderers (`memory/comparison.render_table`, `protocols/render._table`); the other
eighteen were f-strings written where they were used. They agreed on the pipe character and on
almost nothing else: **three of the twenty escaped a cell's content and seventeen did not**, one
spelled the delimiter row `|---|` against nineteen `| --- |`, and "the record is silent here" was
spelled `—`, `""` or nothing at all depending on which file you were reading. What is here is the
arithmetic of putting cells in a grid plus the honesty rules `memory/comparison.py` argued for and
had only to itself, because each of them exists where getting it wrong produced a table that read as
evidence while being an artifact.

**Escaping is a correctness rule, not a style one, and most of the tree did not have it.** A `|` in
a cell does not render badly — it renders as *more table*. Measured on `cli.live_jobs.report`
before this module existed: a `Check` whose `observed` read ``result[0]='a | b'`` — that field is
built from the first 70 characters of a tool's own result in `cli/live_storm.py`, so its content is
a connector's — produced a row of **4** cells under a header declaring 3, which shifts every value
after it under the wrong heading. `render_table` places what a caller supplies **as cells**: a
value can fill a cell and never add one.

**The backslash is escaped before the pipe, and that half is `protocols/render.py`'s rather than
`memory/comparison.py`'s.** Both escaped the pipe; only one escaped the backslash first, and the
difference is not the one either docstring claimed. Measured through markdown-it-py's GFM tables,
pipe-only escaping does *not* break the column count for a value carrying a backslash — a GFM
splitter treats any `|` behind a backslash as escaped whatever precedes it — but it silently eats
the backslash: `x\|y` came back rendered as `x|y`, and `\\` as `\`. So the claim pipe-only
escaping was written to make ("the value still reads as what the source said") is the one it fails,
on any cell carrying a Windows path, a regex or a LaTeX fragment. Escaping the backslash first
returns both verbatim.

**An empty cell is spelled `MISSING`, once.** `drop_empty_columns` decides a column is empty by
comparing rendered cells against that one spelling, so a second spelling of absence — a bare `""` —
would make a column of blanks survive the check that exists to remove it, and would read to a
chemist as a measured zero rather than as silence. A caller that means "not recorded" passes
`MISSING`; a caller that passes `""` gets it anyway, which is how the sites that rendered a blank
unit column join the rule rather than opting out of it.

**No width padding**, for the reason the campaign note needs: the output is read by a Markdown
renderer and by a model, neither of which needs the alignment, and padding would make every
re-synthesis of a committed note a spurious whitespace diff against it.

**No zero-row special case.** A header with no rows under it says "this was asked and nothing came
back", which is a different claim from no table at all, and the two callers that mean the second
say so themselves — `cli/live_data.py` prints a sentence in place of the tables and
`protocols/render.py` returns an empty string. Deciding that here would take the choice away from
the only code that knows which claim is true.
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

    `|` ends a cell in Markdown and a newline ends a row, so a value carrying either adds cells or
    ends the table. Both are reachable from free text this system does not write: an ELN
    observations field, a chemist's note on a charge line, an impurity name an analyst typed, a
    tool result quoted into a probe report, an exception's message. Measured before the escaping
    existed in `memory/comparison.py`: an `observations` field carrying
    ``"routine |\n| rxn-FORGED | 99 | 99 | best result on file | first"`` produced a whole extra
    row with a yield and a superlative that the record it was rendered from did not contain.

    That is evidence forgery rather than prompt injection — nothing here is read as an instruction.
    It is worse in a table than elsewhere, because the artifact exists to be read comparatively and
    cited from, and a forged row is indistinguishable from a real one.

    The text is preserved rather than dropped. The backslash is escaped first so that escaping the
    pipe cannot consume one the source wrote (see this module's docstring for the measurement), and
    whitespace runs collapse because a cell is one line by construction — the alternative spelling
    of a newline inside a cell is HTML in a payload a model reads.
    """
    return " ".join(text.split()).replace("\\", "\\\\").replace("|", r"\|")


def render_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], *, align: str | None = None
) -> str:
    """Render a Markdown table as one block, with no trailing newline.

    `align` is one character per column — `l` or `r` — or `None` for all-left. A row whose length
    does not match the headers raises rather than rendering: a short or long row is a silent
    column shift, which is the same misreading `placeable` exists to prevent, arriving from the
    caller's side instead of from its data.
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
