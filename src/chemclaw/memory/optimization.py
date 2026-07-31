"""Group same-transformation runs into an optimization campaign + note (plan Phase 5, episodic).

The episodic-memory artifact for **process development on one transformation**: a screen where
the same reaction is run repeatedly with varied conditions/reagents to move an output (yield,
purity, robustness). Distinct from `chemclaw.memory.chains` (which links product→reactant across a
synthetic route) — here the members are the *same* chemistry, grouped by DRFP similarity
(`chemclaw.memory.similarity`). The note lays out every run's conditions and outcomes side by side
in the order they were performed, each row naming what it changed relative to the run before it
(`chemclaw.memory.progression`), and cites each via `[[reaction-<id>]]`, so a chemist — or the
agent — can read what was tried, in what order, and what moved the result. The comparative
skeleton is deterministic; the analysis (which change was the lever, what to try next) is the
`optimization-campaign-synthesis` and `experiment-progression` skills' judgment, on top.
"""

from datetime import date

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.core.reagents import resolve_compound_name
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.kg.note import Note
from chemclaw.memory.progression import (
    Progression,
    ProgressionStep,
    order_chronologically,
    progression,
)
from chemclaw.memory.similarity import cluster_by_similarity, reaction_fingerprints


class OptimizationCampaign(BaseModel):
    """A set of >=2 structurally-similar runs of one transformation (an optimization series)."""

    reaction_ids: list[str]


def find_optimization_campaigns(
    reactions: list[OrdReaction], threshold: float | None = None
) -> list[OptimizationCampaign]:
    """Group reactions of the same transformation (DRFP similarity) into optimization series.

    Clusters by DRFP Tanimoto >= `threshold` (default `optimization_similarity_threshold`,
    tight — same reaction, not merely related). A cluster with a single member is not a
    campaign (nothing was optimized) and is dropped. Deterministic (sorted output).
    """
    floor = threshold if threshold is not None else settings.optimization_similarity_threshold
    fingerprints = reaction_fingerprints(reactions)
    return [
        OptimizationCampaign(reaction_ids=cluster)
        for cluster in cluster_by_similarity(fingerprints, floor)
        if len(cluster) >= 2
    ]


def canonical_condition(species: str) -> str:
    """Fold a condition species to one canonical token (gap KNW-4).

    `DMF`, `N,N-dimethylformamide` and `CN(C)C=O` are the same solvent and were three unrelated
    tokens to every lexical and grouping path, so an optimization campaign could be split in two by
    spelling alone. Resolution reuses the one identity table (`chemclaw.core.reagents`), so the
    vocabulary here cannot drift from the one the hazard screen and the calculators use.

    An unrecognised species folds to its own trimmed, lowercased form rather than being dropped:
    an unknown reagent is still a real condition, and losing it would silently merge campaigns that
    genuinely differ.
    """
    match = resolve_compound_name(species)
    return match.smiles if match is not None else species.strip().lower()


def optimization_campaign_note(
    note_id: str, campaign: OptimizationCampaign, reactions: dict[str, OrdReaction]
) -> Note:
    """Build an agent `optimization-campaign` note: the runs in time order, with their deltas.

    Each run is one table row — its reaction note (cited), the date it was performed, headline
    temperature/time, yield, and **what it changed relative to the run before it** — followed by
    a per-run block carrying the hypothesis it was testing and a short procedure excerpt, so the
    intent and the process detail are visible, not just the numbers.

    The ordering is chronological (D-162), because a campaign is usually not a screen run in one
    afternoon: it is a technician working one step for weeks, each day's experiment chosen in
    response to yesterday's result. Grouped by similarity alone, that reads as an unordered set,
    and the trajectory — the variable being walked, what the last three runs ruled out — is
    unreadable. When the runs carry no dates the note says so rather than implying a sequence.

    The note stays output-neutral: it surfaces the recorded conditions, outcomes and changes and
    leaves *what mattered* to the skill's analysis and the human reviewer (D-005).
    """
    members = order_chronologically([reactions[rid] for rid in campaign.reaction_ids])
    series = progression(members)
    representative = members[0].reaction_smiles()
    rows = "\n".join(
        f"| [[reaction-{r.reaction_id}]] | {_date_cell(r.performed_at)} "
        f"| {_cell(r.temperature_c)} | {_cell(r.time_h)} | {_cell(r.yield_percent)} "
        f"| {_changes_cell(step, first=index == 0)} |"
        for index, (r, step) in enumerate(zip(members, series.steps, strict=True))
    )
    body = (
        f"Optimization campaign: {len(members)} runs of the same transformation "
        f"(DRFP-similar), representative `{representative}`.\n\n"
        f"{_ordering_caveat(series)}\n\n"
        "| Run | Performed | Temp (°C) | Time (h) | Yield (%) | Changed vs previous |\n"
        "|-----|-----------|-----------|----------|-----------|---------------------|\n"
        f"{rows}\n"
    )
    detail = "\n".join(block for r in members if (block := _run_detail(r)))
    if detail:
        body += f"\nPer run:\n{detail}\n"
    return Note(
        id=note_id,
        type="optimization-campaign",
        created_by="agent",
        source="memory:optimization-grouping",
        body=body,
    )


def _ordering_caveat(series: Progression) -> str:
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


def _changes_cell(step: ProgressionStep, first: bool) -> str:
    """What this run changed: the deltas, "first run", or an explicit repeat.

    A run whose conditions match its predecessor exactly is not a gap in the record — it is a
    reproducibility check, and saying "unchanged" is what lets a reader tell the two apart.
    """
    if first:
        return "first run"
    if not step.changes:
        return "unchanged (repeat)"
    return "; ".join(change.describe() for change in step.changes)


def _run_detail(reaction: OrdReaction) -> str:
    """The per-run block: the hypothesis it tested, then its procedure excerpt (each if any)."""
    lines = []
    if reaction.hypothesis:
        lines.append(f"  - tested: {' '.join(reaction.hypothesis.split())}")
    if excerpt := _excerpt(reaction):
        lines.append(f"  - procedure: {excerpt}")
    if not lines:
        return ""
    return f"- [[reaction-{reaction.reaction_id}]]:\n" + "\n".join(lines)


def _cell(value: float | None) -> str:
    """Render an optional numeric condition/outcome for a table cell (blank when unknown)."""
    return "—" if value is None else f"{value:g}"


def _date_cell(value: date | None) -> str:
    """Render the date a run was performed (blank when the source did not record one)."""
    return "—" if value is None else value.isoformat()


def _excerpt(reaction: OrdReaction) -> str:
    """A short, single-line procedure excerpt for a run (empty when no procedure was recorded)."""
    if not reaction.procedure_text:
        return ""
    text = " ".join(reaction.procedure_text.split())
    return text[: settings.note_excerpt_chars]
