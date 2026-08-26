"""The *derived* species vocabulary: what a molecule was doing in a reaction.

`chemclaw.ingest.eln.ord.Role` is the **record** vocabulary — the five values an ELN column, an
ORD file or a warehouse binding is allowed to state, and the five a chemist typed. This is the
**derived** one: what a model concluded from the structures after the fact, at a resolution no
source records. The two are deliberately different, and widening `Role` instead of adding this was
the first design considered and rejected, for reasons that are all load-bearing:

* `Role` decides *arithmetic*. `OrdReaction._AGENT_ROLES` chooses which side of
  `transformation_smiles()` a species lands on, so a sixth member changes every DRFP bit in
  `reaction_fingerprints`, forcing a `reaction_definition()` bump and a full re-index.
* `Role` is *tenant-writable*. `ingest.eln.warehouse.binding.ComponentBinding` validates each
  site's YAML `value_map` against it, so a widened enum lets a data file move a species across the
  fingerprint boundary with no code change.
* `ord.py` argues explicitly that a base **stays on the reactant side**, because it participates
  stoichiometrically and is part of what the transformation *is*. A `BASE` member of `Role` would
  either contradict that or be a synonym of `REAGENT`.

So a refined role is a versioned, model-produced *claim about* a recorded role, and it lives beside
the label that produced it.

**Why this module names the recorded roles as strings rather than importing `Role`.** `science/`
may import `chemclaw.core` and nothing else (`tests/test_layering.py`), and `Role` lives in
`ingest/`. `Role` is a `StrEnum`, so its members *are* these strings and a caller passes one
straight in. That leaves one real hazard — a sixth `Role` member landing here unnoticed — and it is
closed where it can be: `tests/test_label_vocabulary.py` asserts `{r.value for r in Role}` equals
this map's keys, so adding a recorded role fails a test instead of silently mapping every species
of that role to `UNKNOWN`.
"""

from enum import StrEnum

# Bumped when this module's *meaning* changes — a new member, or a changed mapping — so that rows
# derived under the old meaning become stale. It is folded into the labeller version on the client
# side (never derived remotely), the same way `CALCULATION_EPOCH` rides in a calculation's
# `params_hash` rather than in the version the remote server reports.
VOCABULARY_VERSION = "roles1"


class SpeciesRole(StrEnum):
    """What one species was doing, at the resolution the precedent questions need.

    `STARTING_MATERIAL` rather than `reactant`, because that is the word the question uses ("has
    this substrate been used as starting material") and because it is not a synonym: a reagent is
    also a reactant in the mass-balance sense, and the distinction this vocabulary exists to draw
    is exactly the one `Role.REACTANT` blurs.

    `UNKNOWN` is a member and not `None` so that "the labeller looked and could not decide" stays
    distinguishable from "nothing has looked yet" — which is the column being NULL. Conflating the
    two would make an unlabelled corpus and an unclassifiable one report identical coverage.
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

    A *group*, not a column, because the fields inside one move together: whatever produced
    `named_reaction` produced `reaction_class`, `rxno_id`, `confidence` and `method` in the same
    breath, and a policy that could ask for one without the others would be a policy nothing could
    honour.
    """

    NAMED_REACTION = "named-reaction"
    ATOM_MAPPING = "atom-mapping"
    SPECIES_ROLES = "species-roles"
    SPECIES_FEATURES = "species-features"


# The total map from what a source recorded to what this vocabulary calls it, before any model has
# looked. Keys are `Role`'s values (see the module docstring). Deliberately conservative: a
# recorded `reagent` becomes `REAGENT` and not a guess at `BASE`, because the whole point of the
# refined vocabulary is that the guess needs the structures.
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

    An unmapped value is `UNKNOWN` rather than an exception: this runs on the ingest path for every
    species of every reaction, and a corpus whose role column holds one unexpected string must be
    indexed with that species marked unknown, not refused wholesale. The test above is what keeps
    that leniency from hiding a *new `Role` member*, which is the case where silence would be a bug.
    """
    return _FROM_RECORD.get(role, SpeciesRole.UNKNOWN)
