"""The comparative table of runs, shared by campaign notes and turn-time comparison.

A process chemist comparing runs of one transformation wants them side by side, a column per
condition and outcome, plus what each run changed relative to the one before. This module holds the
parts that know about a run: the change cells, the ordering caveat and `drop_empty_columns`. Each
caller keeps its own record-specific columns; table rendering, escaping and `MISSING` are
`core.markdown`'s.
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

    An unchanged run is a reproducibility check, not a gap, and is labelled as such.
    """
    if first:
        return "first run"
    if not step.changes:
        return "unchanged (repeat)"
    return "; ".join(change.describe() for change in step.changes)


def ordering_caveat(series: Progression) -> str:
    """State what the row order means, so nobody reads a trajectory into an id listing.

    Three cases: a full timeline, a timeline with undated runs at the end, and no time information
    (where "changed vs previous" compares arbitrary neighbours). The sentence also says when dates
    are entry-creation timestamps (`adapter.DatedIngest`) rather than experiment dates, since a
    batch transcribed at once is not ordered by those.
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

    A column of dashes wastes width and suggests the quantity was measured and absent. Emptiness is
    decided from the rendered cells, so one rule covers every column. In a surviving column a
    missing value shows `MISSING`.
    """
    return [(header, cells) for header, cells in candidates if any(c != MISSING for c in cells)]
