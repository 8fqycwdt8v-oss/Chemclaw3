"""The *derived* species vocabulary: what a molecule was doing in a reaction.

`chemclaw.ingest.eln.ord.Role` is the record vocabulary, what a source may state. This is what a
model concluded from the structures, at a finer resolution. `Role` is not widened instead because it
decides which side of the DRFP transformation a species lands on (a new member forces a re-index),
it is tenant-writable through warehouse `value_map`s, and a base stays on the reactant side by
design. A refined role is a versioned claim about a recorded role.

`science/` may not import `ingest/`, so recorded roles are named here as strings (`Role` is a
`StrEnum`); `tests/test_label_vocabulary.py` asserts the keys equal `Role`'s values so a new
recorded role cannot silently map to `UNKNOWN`.
"""

from enum import StrEnum

# Bumped when this module's meaning changes, so rows derived under the old meaning become stale.
# Folded into the labeller version on the client side, never derived remotely.
VOCABULARY_VERSION = "roles1"


class SpeciesRole(StrEnum):
    """What one species was doing, at the resolution the precedent questions need.

    `STARTING_MATERIAL` rather than `reactant`, because a reagent is also a reactant in the
    mass-balance sense and this vocabulary exists to separate them. `UNKNOWN` is a member, not
    `None`, so "looked and could not decide" differs from "not yet looked" (NULL) in coverage.
    """

    STARTING_MATERIAL = "starting-material"
    PRODUCT = "product"
    REAGENT = "reagent"
    SOLVENT = "solvent"
    CATALYST = "catalyst"
    LIGAND = "ligand"
    BASE = "base"
    ADDITIVE = "additive"
    UNKNOWN = "unknown"


class LabelGroup(StrEnum):
    """One derived label a source may already carry, or the enricher may have to derive.

    A group, not a column, because its fields are produced together (`named_reaction`,
    `reaction_class`, `rxno_id`, `confidence`, `method`), so a policy cannot ask for one without the
    others.
    """

    NAMED_REACTION = "named-reaction"
    ATOM_MAPPING = "atom-mapping"
    SPECIES_ROLES = "species-roles"
    SPECIES_FEATURES = "species-features"


# The total map from a recorded role to this vocabulary before any model has looked. Keys are
# `Role`'s values. Conservative: a recorded `reagent` stays `REAGENT`, since refining it needs the
# structures.
_FROM_RECORD: dict[str, SpeciesRole] = {
    "reactant": SpeciesRole.STARTING_MATERIAL,
    "product": SpeciesRole.PRODUCT,
    "reagent": SpeciesRole.REAGENT,
    "solvent": SpeciesRole.SOLVENT,
    "catalyst": SpeciesRole.CATALYST,
}


def recorded_roles() -> frozenset[str]:
    """The recorded-role strings this module maps — the set the layering test pins to `Role`."""
    return frozenset(_FROM_RECORD)


def species_role_from(role: str) -> SpeciesRole:
    """The coarse `SpeciesRole` a recorded role already implies, with no model involved.

    An unmapped value is `UNKNOWN`, not an exception: this runs on ingest for every species, and one
    unexpected string must not refuse a corpus. The vocabulary test catches a new `Role` member.
    """
    return _FROM_RECORD.get(role, SpeciesRole.UNKNOWN)
