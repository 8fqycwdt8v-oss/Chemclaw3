"""Fold a labeller's answer into a stored row, filling what is missing and keeping what is not.

This module is the whole of "the database will not have all these labels in the beginning, so the
agent should be able to identify all". A source's `labels:` policy says what it *carries*; it never
says what to skip. Pistachio ships NameRxn names for part of its corpus and not for the rest, an
ELN ships none, and both are the same case here: a group is derived for any row where the value is
absent, whatever the policy claims about the source's intent.

`override` is the one thing that reaches past a present value, and it exists for a specific shape:
an ELN's roles are a free-text column somebody typed, so `species-roles` from such a source is a
five-value guess the refined vocabulary must not inherit as though a model had produced it.
"""

from chemclaw.ingest.labels.labeller import ReactionNaming, ReactionRepresentation
from chemclaw.science.labels.policy import LabelPolicy
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.vocabulary import LabelGroup, SpeciesRole, species_role_from


def merge(
    stored: ReactionLabel,
    policy: LabelPolicy,
    representation: ReactionRepresentation | None,
    naming: ReactionNaming | None,
) -> ReactionLabel:
    """The row as it should be stored after this pass: derived where derivable, kept where present.

    A `None` half means the server did not answer for this reaction — it could not parse it, or the
    batch came back short. That is not an error and not a reason to drop the other half: a reaction
    the atom mapper choked on may still be named, and vice versa. Both absent still returns a row,
    which the caller stamps; the alternative is a reaction the drain re-reads on every pass forever
    because nothing can ever be derived from it.
    """
    return stored.model_copy(
        update={
            **_named(stored, policy, naming),
            **_mapping(stored, policy, representation),
            "species": _species(stored, policy, representation),
        }
    )


def _named(
    stored: ReactionLabel, policy: LabelPolicy, naming: ReactionNaming | None
) -> dict[str, object]:
    """The five naming fields, as a group: whatever produced one produced all of them.

    A source-supplied name keeps `method='source'` — which is worth recording, because "Pistachio
    said Buchwald-Hartwig" and "our SMIRKS matched Buchwald-Hartwig" are different evidence and a
    chemist reading a frequency table is entitled to know which they are looking at.
    """
    has_value = stored.named_reaction is not None or stored.rxno_id is not None
    if not policy.derives(LabelGroup.NAMED_REACTION, has_value):
        return {"method": stored.method or _SOURCE}
    if naming is None:
        return {}
    return {
        "named_reaction": naming.named_reaction,
        "reaction_class": naming.reaction_class,
        "rxno_id": naming.rxno_id,
        "confidence": naming.confidence,
        "method": naming.method,
    }


def _mapping(
    stored: ReactionLabel, policy: LabelPolicy, representation: ReactionRepresentation | None
) -> dict[str, object]:
    """The atom map, kept when the source shipped one and we are not overriding it."""
    if not policy.derives(LabelGroup.ATOM_MAPPING, stored.mapped_smiles is not None):
        return {}
    if representation is None or representation.mapped_smiles is None:
        return {}
    return {"mapped_smiles": representation.mapped_smiles}


def _species(
    stored: ReactionLabel, policy: LabelPolicy, representation: ReactionRepresentation | None
) -> list[SpeciesLabel]:
    """Per-species roles and features, positionally against the list that was sent.

    The positional contract is the labeller client's, and it holds because the client sends the
    species explicitly rather than letting the server parse them out of the reaction SMILES: a
    stored species' `ordinal` comes from `OrdReaction.compounds()` and the record SMILES groups the
    agents together, so the two orders differ on every reaction with a solvent.

    A short or absent answer falls back to `species_role_from` — the coarse map from what the
    source recorded. That is a floor rather than a guess: it never invents a ligand, and it keeps
    `UNKNOWN` meaning "a labeller looked and could not decide" rather than "nothing has looked".
    """
    roles = policy.derives(
        LabelGroup.SPECIES_ROLES, any(s.derived_role is not None for s in stored.species)
    )
    features = policy.derives(
        LabelGroup.SPECIES_FEATURES, any(s.scaffold or s.functional_groups for s in stored.species)
    )
    answered = representation.species if representation is not None else []
    merged: list[SpeciesLabel] = []
    for index, species in enumerate(stored.species):
        update: dict[str, object] = {}
        derived = answered[index] if index < len(answered) else None
        if roles:
            update["derived_role"] = (
                _role(derived.role) if derived is not None else species_role_from(species.role)
            )
        if features and derived is not None:
            update["scaffold"] = derived.scaffold
            update["functional_groups"] = list(derived.functional_groups)
        merged.append(species.model_copy(update=update) if update else species)
    return merged


def _role(value: str) -> SpeciesRole:
    """The server's role string as a member, or `UNKNOWN` for one this vocabulary does not have.

    Lenient rather than strict because the server is versioned separately: a labeller that learns a
    new role before this repository does must degrade to "could not decide" for those species, not
    fail the whole batch. The version string is what makes that visible — the rows carry a labeller
    version this build does not fully understand, and re-running after an upgrade re-derives them.
    """
    try:
        return SpeciesRole(value)
    except ValueError:
        return SpeciesRole.UNKNOWN


# What `method` says when the label came with the corpus rather than from a model here.
_SOURCE = "source"
