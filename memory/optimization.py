"""Group same-transformation runs into an optimization campaign + note (plan Phase 5, episodic).

The episodic-memory artifact for **process development on one transformation**: a screen where
the same reaction is run repeatedly with varied conditions/reagents to move an output (yield,
purity, robustness). Distinct from `memory.chains` (which links product→reactant across a
synthetic route) — here the members are the *same* chemistry, grouped by DRFP similarity
(`memory.similarity`). The note lays out every run's conditions and outcomes side by side and
cites each via `[[reaction-<id>]]`, so a chemist — or the agent — can read what was tried and
what moved the result. The comparative skeleton is deterministic; the analysis (which change was
the lever) is the `optimization-campaign-synthesis` skill's judgment, layered on top.
"""

from pydantic import BaseModel

from chemclaw.config import settings
from eln.ord import OrdReaction
from kg.note import Note
from memory.similarity import cluster_by_similarity, reaction_fingerprints


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


def optimization_campaign_note(
    note_id: str, campaign: OptimizationCampaign, reactions: dict[str, OrdReaction]
) -> Note:
    """Build an agent `optimization-campaign` note: a comparative table over the runs.

    Each run is one table row — its reaction note (cited), headline temperature/time, and
    yield — followed by a short procedure excerpt per run so process/observation detail (which
    lives in the recipe prose) is visible, not just the numbers. The note is output-neutral by
    design: it surfaces the recorded conditions and outcomes and leaves *what mattered* to the
    skill's analysis and the human reviewer (D-005).
    """
    members = [reactions[rid] for rid in campaign.reaction_ids]
    representative = members[0].reaction_smiles()
    rows = "\n".join(
        f"| [[reaction-{r.reaction_id}]] | {_cell(r.temperature_c)} | {_cell(r.time_h)} "
        f"| {_cell(r.yield_percent)} |"
        for r in members
    )
    excerpts = "\n".join(
        f"- [[reaction-{r.reaction_id}]]: {_excerpt(r)}" for r in members if _excerpt(r)
    )
    body = (
        f"Optimization campaign: {len(members)} runs of the same transformation "
        f"(DRFP-similar), representative `{representative}`.\n\n"
        "| Run | Temp (°C) | Time (h) | Yield (%) |\n"
        "|-----|-----------|----------|-----------|\n"
        f"{rows}\n"
    )
    if excerpts:
        body += f"\nProcedures:\n{excerpts}\n"
    return Note(
        id=note_id,
        type="optimization-campaign",
        created_by="agent",
        source="memory:optimization-grouping",
        body=body,
    )


def _cell(value: float | None) -> str:
    """Render an optional numeric condition/outcome for a table cell (blank when unknown)."""
    return "—" if value is None else f"{value:g}"


def _excerpt(reaction: OrdReaction) -> str:
    """A short, single-line procedure excerpt for a run (empty when no procedure was recorded)."""
    if not reaction.procedure_text:
        return ""
    text = " ".join(reaction.procedure_text.split())
    return text[: settings.note_excerpt_chars]
