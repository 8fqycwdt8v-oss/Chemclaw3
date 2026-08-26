"""Build the record phase of a label row from a canonical reaction.

The one place that decides what "what was in the flask" means for the label index, so the ELN path
and any corpus drain cannot disagree about it. Two choices here are load-bearing and neither is
obvious:

* **`reaction_smiles()`, never `transformation_smiles()`.** The fingerprint form drops solvent and
  catalyst on purpose, because leaving them in let a solvent swap dominate DRFP similarity. The
  label index is asked *which solvent, which ligand, which base*, so it needs the form that keeps
  them — and there is no later opportunity to recover them, because `ElnAdapter` can fetch entries
  since a timestamp and cannot read one back by id.
* **Every species, in `compounds()` order, with the role the source recorded verbatim.** The
  ordinal is the row key, so it must be derived from the record and not from a set iteration; the
  role is copied rather than interpreted, because interpreting it is the labeller's job and doing
  it twice in two vocabularies is how the two end up disagreeing.
"""

from chemclaw.core.chem import standard_smiles
from chemclaw.ingest.eln.ord import OrdReaction, StepKind
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.vocabulary import species_role_from


def record_phase(reaction: OrdReaction, source: str) -> ReactionLabel:
    """The record phase of `reaction` as it arrived from `source`, with nothing derived.

    `derived_role` is left `None` on every species — deliberately, even though
    `species_role_from` could fill a coarse value here. NULL means "nothing has looked yet", which
    is what makes the row stale and what the coverage report counts; pre-filling it would make an
    unlabelled corpus indistinguishable from a labelled one at a glance. The coarse map is applied
    by the enricher as the *floor* under a model's answer, not as a stand-in for it.
    """
    return ReactionLabel(
        source=source,
        reaction_id=reaction.reaction_id,
        record_smiles=reaction.reaction_smiles(),
        citation=note_id_for_reaction(reaction.reaction_id),
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

    Only `StepKind.WORKUP` steps, and only their own text — not the whole procedure. "How do we
    best work up a reaction with this reagent" is answered by showing a chemist what other people
    actually did, and a full procedure buries that in charging and purification. A reaction whose
    export carries a procedure but no structured steps has no workup here rather than a guess at
    one; a heuristic that split prose on "the mixture was quenched" would be a extraction model
    hiding in an index.
    """
    steps = [step.text.strip() for step in reaction.steps if step.kind is StepKind.WORKUP]
    joined = "\n\n".join(text for text in steps if text)
    return joined or None


def coarse_species_roles(label: ReactionLabel) -> ReactionLabel:
    """Fill each species' `derived_role` from its recorded role alone — the floor, not the answer.

    Used by the enricher for a source whose `species-roles` group it is not deriving, and as the
    fallback when the labeller cannot classify a species. Keeps `unknown` meaning "a labeller
    looked and could not decide" rather than "nothing has looked".
    """
    return label.model_copy(
        update={
            "species": [
                species.model_copy(update={"derived_role": species_role_from(species.role)})
                for species in label.species
            ]
        }
    )
