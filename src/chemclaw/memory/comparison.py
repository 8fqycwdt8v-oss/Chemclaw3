"""The comparative table, rendered once and read at two altitudes.

A process chemist comparing runs of one transformation wants one thing on the page: the runs side
by side, a column per condition and outcome, and — the column that carries the actual development
argument — *what each run changed relative to the one before it*. `chemclaw.memory.optimization`
has built exactly that since Phase 5, for a DRFP-similar campaign, offline, into a PR-gated note.

This module is that renderer with the campaign taken out of it, because a second caller now needs
the same table over a *retrieved* set of protocols at turn time — the same artifact at a different
altitude. Extracting it is what stops the two from drifting into two tables that disagree about
what `—` means or about what an undated series licenses.

**What is here is the reduce, and only the reduce.** Everything that knows what a *record* is stays
with its caller: `optimization` keeps the `OrdReaction` columns (purity, the major impurity, the
procedure excerpt) because it holds `OrdReaction`s, and the turn-time caller keeps its own, because
it holds prose. What both share is the arithmetic of putting cells in a grid and the three honesty
rules below — and those rules are the part worth having in one place, since each of them exists
because getting it wrong produced a table that read as evidence while being an artifact.
"""

from datetime import date

from chemclaw.memory.progression import Progression, ProgressionStep

# What a table cell shows when the record is silent. One spelling, because `drop_empty_columns`
# decides a column is empty by comparing against it — two spellings would make a column of dashes
# survive the check that exists to remove it.
MISSING = "—"


def cell(value: float | None) -> str:
    """Render an optional numeric condition/outcome for a table cell (blank when unknown)."""
    return MISSING if value is None else f"{value:g}"


def date_cell(value: date | None) -> str:
    """Render the date a run was performed (blank when the source did not record one)."""
    return MISSING if value is None else value.isoformat()


def changes_cell(step: ProgressionStep, *, first: bool) -> str:
    """What this run changed: the deltas, "first run", or an explicit repeat.

    A run whose conditions match its predecessor exactly is not a gap in the record — it is a
    reproducibility check, and saying "unchanged" is what lets a reader tell the two apart.
    """
    if first:
        return "first run"
    if not step.changes:
        return "unchanged (repeat)"
    return "; ".join(change.describe() for change in step.changes)


def ordering_caveat(series: Progression) -> str:
    """State what the row order means, so nobody reads a trajectory into an id listing.

    Three cases, because they license three different readings: a full timeline, a timeline with
    undated runs parked at the end, and no time information at all — where the "changed vs
    previous" column compares neighbours in an arbitrary listing and must not be read as "what
    was tried next".
    """
    undated = series.undated()
    if series.is_timeline():
        return "Runs in the order they were performed."
    if len(undated) < len(series.steps):
        return (
            "Runs in the order they were performed, except "
            f"{len(undated)} with no recorded date, listed last: "
            + ", ".join(f"[[reaction-{rid}]]" for rid in undated)
            + "."
        )
    return (
        "**No run carries a date**, so this is a stable id listing, not a timeline — the changes "
        "column compares neighbouring rows, which is not evidence of what was tried next."
    )


def drop_empty_columns(candidates: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    """Keep only the columns some row actually recorded, as `(header, cells)`.

    **A column appears only if some row recorded it.** A column of dashes is worse than no column:
    it costs width in every row, invites the reader to conclude the quantity was measured and found
    absent, and pushes the columns that do carry data off the side of a narrow view. Emptiness is
    decided from the *rendered cells* rather than from a per-field predicate, so one rule covers
    every column and "recorded" cannot come to mean something different per column. Within a column
    that survives, a row missing the value keeps `MISSING`, which reads as "not measured here"
    against neighbours that were.
    """
    return [(header, cells) for header, cells in candidates if any(c != MISSING for c in cells)]


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a Markdown table, one list of cells per row, ending in a newline.

    Cell contents are the caller's — this only places them. No width padding: the table is read by
    a Markdown renderer and by a model, neither of which needs it, and padding would make every
    re-synthesis of a campaign a spurious whitespace diff against the merged note.
    """
    header_row = f"| {' | '.join(headers)} |"
    rule = f"|{'|'.join('---' for _ in headers)}|"
    body = "\n".join(f"| {' | '.join(cells)} |" for cells in rows)
    return f"{header_row}\n{rule}\n{body}\n"
