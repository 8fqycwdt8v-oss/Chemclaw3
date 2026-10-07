"""Build the record phase of a label row from a canonical reaction.

The one definition of "what was in the flask" for the label index, shared by the ELN path and corpus
drains:

* `reaction_smiles()`, never `transformation_smiles()`: the index is asked about solvents, ligands
  and bases, which the fingerprint form drops, and they cannot be recovered later.
* Every species in `compounds()` order (the ordinal is the row key), with the source's role copied
  verbatim; interpreting it is the labeller's job.
"""

from chemclaw.core.chem import standard_smiles
from chemclaw.ingest.eln.ord import OrdReaction, StepKind
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel


def record_phase(reaction: OrdReaction, source: str) -> ReactionLabel:
    """The record phase of `reaction` as it arrived from `source`, with nothing derived.

    `derived_role` stays `None`: NULL means "nothing has looked yet", which makes the row stale and
    is what coverage counts. The coarse source map is applied by the enricher only as a floor.
    """
    return ReactionLabel(
        source=source,
        reaction_id=reaction.reaction_id,
        record_smiles=reaction.reaction_smiles(),
        # Qualified by source, so a citation follows back to one site's record even when ids
        # collide.
        citation=note_id_for_reaction(reaction.reaction_id, source),
        performed_on=reaction.performed_at,
        temperature_c=reaction.temperature_c,
        time_h=reaction.time_h,
        yield_percent=reaction.yield_percent,
        workup_text=workup_text(reaction),
        species=[
            SpeciesLabel(
                ordinal=ordinal,
                smiles=standard_smiles(component.smiles),
                role=component.role.value,
            )
            for ordinal, component in enumerate(reaction.compounds())
        ],
    )


def workup_text(reaction: OrdReaction) -> str | None:
    """The reaction's workup instructions, verbatim, or `None` when it recorded none.

    Only the text of `StepKind.WORKUP` steps, not the full procedure. A reaction with a procedure
    but no structured steps has no workup here; splitting prose heuristically is not this index's
    job.
    """
    steps = [step.text.strip() for step in reaction.steps if step.kind is StepKind.WORKUP]
    joined = "\n\n".join(text for text in steps if text)
    return joined or None
