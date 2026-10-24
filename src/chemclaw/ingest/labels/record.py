"""Build the record phase of a label row from a canonical reaction.

The one definition of "what was in the flask" for the label index, shared by the ELN path and corpus
drains:

* `reaction_smiles()`, never `transformation_smiles()`: the index is asked about solvents, ligands
  and bases, which the fingerprint form drops, and they cannot be recovered later.
* Every species in `compounds()` order (the ordinal is the row key), with the source's role copied
  verbatim; interpreting it is the labeller's job.

A citation-only record (the source named a species without its structure) gets a row too, of the
record phase only: the species the source drew, its conditions, yield and workup, no reaction
string, and each species' coarse role mapped from the source's own word. No labeller ever reads it,
so nothing is derived from a partial reaction, and a species the source only named has no row.
"""

from chemclaw.core.chem import standard_smiles
from chemclaw.ingest.eln.ord import OrdReaction, RecordTier, StepKind
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.science.labels.records import LabelTier, ReactionLabel, SpeciesLabel
from chemclaw.science.labels.vocabulary import species_role_from


def record_phase(reaction: OrdReaction, source: str) -> ReactionLabel:
    """The record phase of `reaction` as it arrived from `source`, with nothing derived.

    For a structured reaction `derived_role` stays `None`: NULL means "nothing has looked yet",
    which makes the row stale and is what coverage counts, and the coarse source map is applied by
    the enricher only as a floor. A citation-only record is never looked at, so its species carry
    that floor from the start: the source's own word mapped to the vocabulary, never refined.
    """
    cited = reaction.tier is RecordTier.CITATION_ONLY
    return ReactionLabel(
        source=source,
        reaction_id=reaction.reaction_id,
        tier=LabelTier.CITATION_ONLY if cited else LabelTier.STRUCTURED,
        record_smiles=None if cited else reaction.reaction_smiles(),
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
                derived_role=species_role_from(component.role.value) if cited else None,
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
