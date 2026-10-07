"""Group same-transformation runs into an optimization campaign and note.

Episodic memory for process development on one transformation: the same reaction run repeatedly with
varied conditions. Unlike `chemclaw.memory.chains` (product to reactant along a route), members are
the same chemistry grouped by DRFP similarity (`chemclaw.memory.similarity`). The note lays out
every run side by side in performed order, with what each changed relative to the previous
(`chemclaw.memory.progression`), citing each via `[[reaction-<id>]]`. The table is deterministic and
complete on its own; analysis is left to on-demand skills.
"""

from datetime import date

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.core.markdown import MISSING, render_table
from chemclaw.ingest.eln.ord import Impurity, OrdReaction
from chemclaw.kg.note import Note, strip_links
from chemclaw.memory.comparison import (
    cell,
    changes_cell,
    date_cell,
    drop_empty_columns,
    ordering_caveat,
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

    Clusters by DRFP Tanimoto >= `threshold` (default `optimization_similarity_threshold`, tight:
    the same reaction). Single-member clusters are dropped. Deterministic (sorted output).
    """
    floor = threshold if threshold is not None else settings.optimization_similarity_threshold
    fingerprints = reaction_fingerprints(reactions)
    return [
        OptimizationCampaign(reaction_ids=cluster)
        for cluster in cluster_by_similarity(fingerprints, floor)
        if len(cluster) >= 2
    ]


def optimization_campaign_note(
    note_id: str,
    campaign: OptimizationCampaign,
    reactions: dict[str, OrdReaction],
    *,
    minted_on: date | None = None,
) -> Note:
    """Build an agent `optimization-campaign` note: the runs in time order, with their deltas.

    One table row per run (cited reaction, date, setpoints, yield, recorded quality columns, and
    what changed vs the previous run), then a per-run block with its hypothesis and procedure
    excerpt. Chronological because a campaign is usually worked day by day; without dates the note
    says so. Output-neutral: it reports what was recorded and leaves what mattered to the reader.
    """
    # Rows are read off the ordered series and looked up by id, rather than zipping two separately
    # sorted lists that could mispair silently.
    series = progression([reactions[rid] for rid in campaign.reaction_ids])
    members = [reactions[step.reaction_id] for step in series.steps]
    pairs = list(zip(series.steps, members, strict=True))
    # Every column except the two always-answerable ones goes through `drop_empty_columns`,
    # including the setpoints, which a prose-only source may not record; the turn-time comparison
    # applies the same rule.
    columns = [("Run", [f"[[reaction-{step.reaction_id}]]" for step, _ in pairs])] + [
        *drop_empty_columns(
            [
                ("Performed", [date_cell(step.performed_at) for step, _ in pairs]),
                ("Temp (°C)", [cell(run.temperature_c) for _, run in pairs]),
                ("Time (h)", [cell(run.time_h) for _, run in pairs]),
                ("Yield (%)", [cell(run.yield_percent) for _, run in pairs]),
                *_quality_columns(members),
            ]
        ),
        # Never dropped: it always says something — a delta, "first run" or "unchanged (repeat)" —
        # and it is the column carrying the development argument.
        (
            "Changed vs previous",
            [changes_cell(step, first=index == 0) for index, (step, _) in enumerate(pairs)],
        ),
    ]
    headers = [name for name, _ in columns]
    rows = [[cells[index] for _, cells in columns] for index in range(len(pairs))]
    body = (
        f"Optimization campaign: {len(members)} runs of the same transformation "
        f"(DRFP-similar), representative `{members[0].reaction_smiles()}`.\n\n"
        f"{ordering_caveat(series)}\n\n"
        f"{render_table(headers, rows)}\n"
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
        # The anchor run's date (see `jobs.supported_from`); absent, the digest never reports the
        # note.
        valid_from=minted_on,
    )


def _run_detail(reaction: OrdReaction) -> str:
    """The per-run block: the hypothesis it tested, then its procedure excerpt (each if any).

    Both are ELN free text, so wikilinks are stripped to their targets: otherwise the text could
    forge graph edges the campaign never cited.
    """
    lines = []
    if reaction.hypothesis:
        lines.append(f"  - tested: {strip_links(' '.join(reaction.hypothesis.split()))}")
    if excerpt := _excerpt(reaction):
        lines.append(f"  - procedure: {excerpt}")
    if not lines:
        return ""
    return f"- [[reaction-{reaction.reaction_id}]]:\n" + "\n".join(lines)


def _quality_columns(members: list[OrdReaction]) -> list[tuple[str, list[str]]]:
    """The outcome columns beyond yield (purity and the impurity profile) as `(header, cells)`.

    A process campaign often optimizes the impurity the yield hides, so these belong in the table,
    placed between Yield and "Changed vs previous". Returns candidates only; the caller applies
    `comparison.drop_empty_columns` over all columns at once.
    """
    return [
        ("Purity (%)", [cell(run.purity_percent) for run in members]),
        ("Major impurity", [_impurity_cell(run.major_impurity()) for run in members]),
        (
            "Impurity area (%)",
            [cell(imp.area_percent if (imp := run.major_impurity()) else None) for run in members],
        ),
    ]


def _impurity_cell(impurity: Impurity | None) -> str:
    """Name an impurity in a table cell, by whatever identity the record carries."""
    if impurity is None:
        return MISSING
    return impurity.name or f"`{impurity.smiles}`"


def _excerpt(reaction: OrdReaction) -> str:
    """A short, single-line procedure excerpt for a run (empty when no procedure was recorded)."""
    if not reaction.procedure_text:
        return ""
    text = strip_links(" ".join(reaction.procedure_text.split()))
    return text[: settings.note_excerpt_chars]
