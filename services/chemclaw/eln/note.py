"""Map a canonical ORD reaction to an agent knowledge-graph note (plan step 4.5).

The pure mapping from an `OrdReaction` to a `reaction` note, proposed through the **same**
PR-gate as every other agent note (D-005) — no second write path. Kept separate from the
sync activity so it is tested directly. The note records the reaction SMILES, headline
conditions, and the full **step-by-step procedure** in prose so a detailed development recipe
survives ingestion intact (a human reviewer signs off on the recipe, not just a SMILES). It
carries no `[[wikilink]]` (a dangling link would fail `kg.validate` on the very PR this
opens); compound cross-links are a later step once compound notes exist.
"""

from eln.ord import OrdReaction, ReactionStep
from kg.note import Note


def note_from_ord_reaction(reaction: OrdReaction) -> Note:
    """Map an `OrdReaction` to an agent-authored `reaction` note (idempotent id)."""
    body = (
        f"Reaction `{reaction.reaction_smiles()}` from ELN entry {reaction.reaction_id}.\n\n"
        f"{_conditions_block(reaction)}"
        f"{_procedure_block(reaction)}"
    )
    return Note(
        id=f"reaction-{reaction.reaction_id}",
        type="reaction",
        created_by="agent",
        source=reaction.provenance,
        body=body,
    )


def _conditions_block(reaction: OrdReaction) -> str:
    """Render the headline conditions (temperature/time/yield) as a bullet list."""
    conditions = []
    if reaction.temperature_c is not None:
        conditions.append(f"temperature: {reaction.temperature_c} °C")
    if reaction.time_h is not None:
        conditions.append(f"time: {reaction.time_h} h")
    if reaction.yield_percent is not None:
        conditions.append(f"yield: {reaction.yield_percent}%")
    return "".join(f"- {c}\n" for c in conditions)


def _procedure_block(reaction: OrdReaction) -> str:
    """Render the ordered procedure as a numbered list (empty when there are no steps)."""
    if not reaction.steps:
        return ""
    lines = "".join(f"{step.index}. {_step_line(step)}\n" for step in reaction.steps)
    return f"\n## Procedure\n\n{lines}"


def _step_line(step: ReactionStep) -> str:
    """One procedure line: the instruction, tagged with its kind and any parsed conditions."""
    detail = [f"_{step.kind.value}_"]
    if step.temperature_c is not None:
        detail.append(f"{step.temperature_c} °C")
    if step.duration_h is not None:
        detail.append(f"{step.duration_h} h")
    return f"{step.text} ({', '.join(detail)})"
