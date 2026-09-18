"""The comparative table, rendered once and read at two altitudes.

A process chemist comparing runs of one transformation wants one thing on the page: the runs side
by side, a column per condition and outcome, and — the column that carries the actual development
argument — *what each run changed relative to the one before it*. `chemclaw.memory.optimization`
has built exactly that since Phase 5, for a DRFP-similar campaign, offline, into a campaign note.

This module is what is left of that table once the campaign is taken out of it, because a second
caller needs the same table over a *retrieved* set of protocols at turn time — the same artifact at
a different altitude. Extracting it is what stops the two from drifting into two tables that
disagree about what `—` means or about what an undated series licenses. (The grid itself has since
gone one level further down; the paragraph after next says where and why.)

**What is here is the reduce, and only the reduce.** Everything that knows what a *record* is stays
with its caller: `optimization` keeps the `OrdReaction` columns (purity, the major impurity, the
procedure excerpt) because it holds `OrdReaction`s, and the turn-time caller keeps its own, because
it holds prose. What both share is how an *absent* value is rendered and what an undated series
licenses — the honesty rules below — since each of them exists because getting it wrong produced a
table that read as evidence while being an artifact.

**The grid arithmetic left, and so did one of those rules.** Nineteen other places in this tree
rendered a Markdown table, seventeen of them escaping nothing, so this module's own escaping
guarantee was a property of two artifacts rather than of this system's tables. It is
`core.markdown` now — the renderer, the escaping and the one spelling of `MISSING`, which the cells
below compare against because the code that *renders* an empty cell is the code that has to name it.
Both callers import `render_table` and `MISSING` from there directly rather than through here, so
there is one place to read either from. What stays is what knows about a *run*: the cells, the
ordering caveat, and `drop_empty_columns`, which is the one honesty rule whose subject is a column
of records rather than a cell of text.
"""

from datetime import date

from chemclaw.core.markdown import MISSING
from chemclaw.memory.progression import Progression, ProgressionStep


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

    **A fourth thing cuts across the first two: where a date came from.** A source with no
    experiment-date column is dated from the entry's creation timestamp by the ingest seam
    (`adapter.DatedIngest`), which is when the record was *written*. Ordering by that is the best
    available and is usually right, but a batch transcribed in one sitting orders by nothing — so
    the sentence has to say which kind of timeline it is rather than claim the stronger one.
    """
    undated = series.undated()
    if series.is_timeline():
        if entry := series.entry_dated():
            return (
                f"Runs in the order they were recorded. {_runs(len(entry))} carry no experiment "
                "date, so the order is the order the entries were written, not proof of the order "
                "they were run — a batch transcribed in one sitting carries no sequence at all."
            )
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


def _runs(count: int) -> str:
    """Render a run count for a sentence: `1 run`, or `n runs`, without a stray plural."""
    return "1 run" if count == 1 else f"{count} runs"


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
