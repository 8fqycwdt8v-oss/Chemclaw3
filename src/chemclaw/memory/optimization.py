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

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.core.reagents import resolve_compound_name
from chemclaw.ingest.eln.ord import Impurity, OrdReaction
from chemclaw.kg.note import Note
from chemclaw.memory.comparison import (
    MISSING,
    cell,
    changes_cell,
    date_cell,
    drop_empty_columns,
    ordering_caveat,
    render_table,
)
from chemclaw.memory.progression import progression
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
    vocabulary here cannot drift from the one every other in-process caller uses. That guarantee is
    now bounded by the process: after `D-2026-08-16-the-physics-leaves-the-cache-stays` the
    calculators and the hazard screen answer from `Chemclaw3-mcp`, each carrying its own reagent
    table, and a shared import no longer holds them together.

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
    temperature/time, yield, the outcome-quality columns this campaign actually recorded
    (`_quality_columns`), and **what it changed relative to the run before it** — followed by a
    per-run block carrying the hypothesis it was testing and a short procedure excerpt, so the
    intent and the process detail are visible, not just the numbers.

    The ordering is chronological (D-162), because a campaign is usually not a screen run in one
    afternoon: it is a technician working one step for weeks, each day's experiment chosen in
    response to yesterday's result. Grouped by similarity alone, that reads as an unordered set,
    and the trajectory — the variable being walked, what the last three runs ruled out — is
    unreadable. When the runs carry no dates the note says so rather than implying a sequence.

    The note stays output-neutral: it surfaces the recorded conditions, outcomes and changes and
    leaves *what mattered* to the skill's analysis and the human reviewer (D-005).
    """
    # The series *is* the ordering: every row is read off a step, and the run behind it is looked
    # up by id. Zipping two independently-sorted lists would have paired them positionally, which
    # is right only for as long as two functions agree on a sort — and being the same length, a
    # disagreement would have mispaired every row silently rather than raising.
    series = progression([reactions[rid] for rid in campaign.reaction_ids])
    members = [reactions[step.reaction_id] for step in series.steps]
    quality = _quality_columns(members)
    headers = [
        "Run",
        "Performed",
        "Temp (°C)",
        "Time (h)",
        "Yield (%)",
        *(name for name, _ in quality),
        "Changed vs previous",
    ]
    rows = [
        [
            f"[[reaction-{step.reaction_id}]]",
            date_cell(step.performed_at),
            cell(run.temperature_c),
            cell(run.time_h),
            cell(run.yield_percent),
            *(cells[index] for _, cells in quality),
            changes_cell(step, first=index == 0),
        ]
        for index, (step, run) in enumerate(zip(series.steps, members, strict=True))
    ]
    body = (
        f"Optimization campaign: {len(members)} runs of the same transformation "
        f"(DRFP-similar), representative `{members[0].reaction_smiles()}`.\n\n"
        f"{ordering_caveat(series)}\n\n"
        f"{render_table(headers, rows)}"
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


def _quality_columns(members: list[OrdReaction]) -> list[tuple[str, list[str]]]:
    """The outcome columns beyond yield — purity and the impurity profile — as `(header, cells)`.

    A process campaign is rarely optimizing yield: it is optimizing the impurity that yield hides,
    and this is the one artifact built for side-by-side reading, so the three numbers a chemist
    actually compares belong in its rows. They sit between Yield and "Changed vs previous" —
    outcomes grouped together, with the widest free-text column left last.

    What is decided *here* is which three candidates an `OrdReaction` can offer. Whether a
    candidate survives is `comparison.drop_empty_columns`' rule — a column appears only if some run
    recorded it — which lives there because the turn-time digest needs the same rule over its own
    columns, and a second copy is how the two come to disagree about what `—` means.
    """
    candidates = [
        ("Purity (%)", [cell(run.purity_percent) for run in members]),
        ("Major impurity", [_impurity_cell(_major_impurity(run)) for run in members]),
        (
            "Impurity area (%)",
            [cell(imp.area_percent if (imp := _major_impurity(run)) else None) for run in members],
        ),
    ]
    return drop_empty_columns(candidates)


def _major_impurity(reaction: OrdReaction) -> Impurity | None:
    """The impurity a chemist would call the major one, or `None` when the record cannot say.

    Ranked by recorded `area_percent`, the number process development actually chases. When no
    impurity carries an area% the list is unranked, and naming one anyway would be the same
    fabrication `eln.note._principal_product` refuses for products — the cell would look like
    evidence about which impurity dominated while being an artifact of the export's ordering.
    A single recorded impurity is the exception that needs no ranking: it is the only one the
    record names, so calling it the major one adds no claim.
    """
    ranked = [imp for imp in reaction.impurities if imp.area_percent is not None]
    if ranked:
        return max(ranked, key=lambda imp: imp.area_percent or 0.0)
    return reaction.impurities[0] if len(reaction.impurities) == 1 else None


def _impurity_cell(impurity: Impurity | None) -> str:
    """Name an impurity in a table cell, by whatever identity the record carries."""
    if impurity is None:
        return MISSING
    return impurity.name or f"`{impurity.smiles}`"


def _excerpt(reaction: OrdReaction) -> str:
    """A short, single-line procedure excerpt for a run (empty when no procedure was recorded)."""
    if not reaction.procedure_text:
        return ""
    text = " ".join(reaction.procedure_text.split())
    return text[: settings.note_excerpt_chars]
