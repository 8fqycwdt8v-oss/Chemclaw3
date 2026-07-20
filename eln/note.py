"""Map a canonical ORD reaction to an agent knowledge-graph note (plan step 4.5).

The pure mapping from an `OrdReaction` to a `reaction` note, proposed through the **same**
PR-gate as every other agent note (D-005) — no second write path. Kept separate from the
sync activity so it is tested directly. The note records the reaction SMILES and headline
conditions in prose and carries no `[[wikilink]]` (a dangling link would fail `kg.validate`
on the very PR this opens); compound cross-links are a later step once compound notes exist.
"""

from eln.ord import OrdReaction
from kg.note import Note


def note_from_ord_reaction(reaction: OrdReaction) -> Note:
    """Map an `OrdReaction` to an agent-authored `reaction` note (idempotent id)."""
    conditions = []
    if reaction.temperature_c is not None:
        conditions.append(f"temperature: {reaction.temperature_c} °C")
    if reaction.time_h is not None:
        conditions.append(f"time: {reaction.time_h} h")
    if reaction.yield_percent is not None:
        conditions.append(f"yield: {reaction.yield_percent}%")
    condition_lines = "".join(f"- {c}\n" for c in conditions)
    body = (
        f"Reaction `{reaction.reaction_smiles()}` from ELN entry {reaction.reaction_id}.\n\n"
        f"{condition_lines}"
    )
    return Note(
        id=f"reaction-{reaction.reaction_id}",
        type="reaction",
        created_by="agent",
        source=reaction.provenance,
        body=body,
    )
