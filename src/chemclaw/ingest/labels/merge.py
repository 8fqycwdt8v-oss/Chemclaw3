"""Fold a labeller's answer into a stored row, filling what is missing and keeping what is not.

A source's `labels:` policy says what it carries, never what to skip: a group is derived for any row
where the value is absent. `override` is the one thing that replaces a present value, for sources
whose values are untrustworthy (e.g. an ELN's free-text species roles).
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

    A `None` half means the server did not answer for it; the other half is still merged. With both
    absent a row is still returned for the caller to stamp, so it is not re-read forever.
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

    A source-supplied name keeps `method='source'`, so a source's classification is distinguishable
    from our SMIRKS match.
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

    The client sends species in `OrdReaction.compounds()` order, so positions match stored ordinals.
    A short or absent answer falls back to `species_role_from`, the coarse map of what the source
    recorded: it never invents a ligand, and keeps `UNKNOWN` meaning "a labeller could not decide".
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

    Lenient because the server is versioned separately; the row's labeller version records it, and
    re-running after an upgrade re-derives it.
    """
    try:
        return SpeciesRole(value)
    except ValueError:
        return SpeciesRole.UNKNOWN


# What `method` says when the label came with the corpus rather than from a model here.
_SOURCE = "source"
