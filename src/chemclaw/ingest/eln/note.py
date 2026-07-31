"""Map a canonical ORD reaction to an agent knowledge-graph note (plan step 4.5).

The pure mapping from an `OrdReaction` to a `reaction` note, proposed through the **same**
PR-gate as every other agent note (D-005) — no second write path. Kept separate from the
sync activity so it is tested directly. The note records the reaction SMILES, headline
conditions, and the full **step-by-step procedure** in prose so a detailed development recipe
survives ingestion intact (a human reviewer signs off on the recipe, not just a SMILES). It
carries no `[[wikilink]]` (a dangling link would fail `chemclaw.kg.validate` on the very PR this
opens); compound cross-links are a later step once compound notes exist.
"""

from chemclaw.ingest.eln.ord import Impurity, OrdReaction, OutcomeClass, ReactionStep
from chemclaw.kg.note import Note


def note_from_ord_reaction(reaction: OrdReaction) -> Note:
    """Map an `OrdReaction` to an agent-authored `reaction` note (idempotent id)."""
    body = (
        f"Reaction `{reaction.reaction_smiles()}` from ELN entry {reaction.reaction_id}.\n\n"
        f"{_hypothesis_block(reaction)}"
        f"{_conditions_block(reaction)}"
        f"{_impurity_block(reaction)}"
        f"{_procedure_block(reaction)}"
    )
    return Note(
        id=f"reaction-{reaction.reaction_id}",
        type="reaction",
        created_by="agent",
        source=reaction.provenance,
        # The experiment's own date is what makes the note time-scopable (gap KNW-1). F10-G2 added
        # `valid_from`/`valid_to` to answer "what did we know at time T", and for the largest note
        # class nothing populated them — a reaction became valid-since-forever. A run is evidence
        # from the day it was run; `valid_to` stays open (a result does not expire on its own, it
        # is superseded, which is a separate edit).
        valid_from=reaction.performed_at,
        body=body,
    )


def _hypothesis_block(reaction: OrdReaction) -> str:
    """Lead with what the run was testing, when the source recorded it (D-157).

    First in the body rather than buried among the conditions, because it is what makes the run
    legible: every condition below is an answer, and this is the question. Empty when unrecorded —
    the note never says "no hypothesis", which would assert something the record does not.
    """
    if not reaction.hypothesis:
        return ""
    return f"Tested: {' '.join(reaction.hypothesis.split())}\n\n"


def _conditions_block(reaction: OrdReaction) -> str:
    """Render the headline conditions (temperature/time/yield) as a bullet list."""
    conditions = []
    if reaction.temperature_c is not None:
        conditions.append(f"temperature: {reaction.temperature_c} °C")
    if reaction.time_h is not None:
        conditions.append(f"time: {reaction.time_h} h")
    if reaction.yield_percent is not None:
        conditions.append(f"yield: {reaction.yield_percent}%")
    if reaction.purity_percent is not None:
        conditions.append(f"purity: {reaction.purity_percent}%")
    if reaction.performed_at is not None:
        conditions.append(f"performed: {reaction.performed_at.isoformat()}")
    if reaction.outcome_class is not OutcomeClass.SUCCESS:
        # Stated first-class in the body, not implied by a missing yield: a reader (and retrieval)
        # must be able to tell "this did not work" from "nobody recorded the number" (gap KNW-3).
        outcome = f"outcome: {reaction.outcome_class.value}"
        if reaction.failure_reason:
            outcome += f" — {reaction.failure_reason}"
        conditions.append(outcome)
    return "".join(f"- {c}\n" for c in conditions)


def _impurity_block(reaction: OrdReaction) -> str:
    """Render the impurity profile, the half of the outcome yield alone never captures.

    Rendered into the note body (not only frontmatter) because retrieval reads bodies: an
    impurity-driven question — "what did we see besides product on that route?" — has to be able
    to match here.
    """
    if not reaction.impurities:
        return ""
    lines = "".join(f"- {_impurity_line(imp)}\n" for imp in reaction.impurities)
    return f"\n## Impurities\n\n{lines}"


def _impurity_line(impurity: Impurity) -> str:
    """One impurity: whatever the source actually recorded, never a fabricated identity."""
    label = impurity.name or impurity.smiles or "unidentified"
    detail = []
    if impurity.smiles and impurity.name:
        detail.append(f"`{impurity.smiles}`")
    if impurity.area_percent is not None:
        detail.append(f"{impurity.area_percent}% area")
    return f"{label} ({', '.join(detail)})" if detail else label


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
