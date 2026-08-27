"""DRFP reaction fingerprints — the reaction capability core (plan step 3.4).

Pure, GPU-free, model-free: a reaction SMILES (`reactants>>products`) becomes a DRFP
(Differential Reaction FingerPrint) folded to `settings.drfp_bits` and stored as a
fixed-width bitstring, so it maps onto a Postgres `bit(drfp_bits)` column exactly like the
molecule ECFP4. DRFP is the reaction-specific "reaction SMILES → bits" step; ranking and
the store are the shared, domain-neutral `chemclaw.science.fingerprints.store`.
"""

from drfp import DrfpEncoder

from chemclaw.core.chem import STANDARDIZATION_VERSION, standard_smiles
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintInputError


def _standardize_species(reaction_smiles: str) -> str:
    """Standardize every `.`-separated species in a `reactants>agents>products` string.

    `OrdReaction.transformation_smiles` builds every indexed row this way (`standard_smiles`
    per component), so a query built any other way scores against a form it never shares
    (REV-1). Applied here rather than left to each caller, because a caller with only a bare
    string has nothing else to standardize it with — this is the one function every query and
    every indexed row passes through.

    Splits on the single-`>` reaction-SMILES convention — `reactants>agents>products`, of
    which `reactants>>products` is the empty-agents case — and standardizes each field's
    tokens independently, agents included. Not just the outer two fields: `DrfpEncoder`
    folds the agent slot onto the reactants before it shingles anything
    (`sides[0] += "." + sides[1]`, see `reaction_definition`), so a 3-part string and its
    hand-folded 2-part equivalent must come out standardized identically, token for token, or
    the fold stops being a no-op and two spellings of one reaction diverge.

    A string that does not split into exactly three `>`-delimited fields is returned
    unchanged: it is not a reaction SMILES this module's callers write, and `DrfpEncoder` is
    what should raise on it, not a guess here about how to parse it.
    """
    fields = reaction_smiles.split(">")
    if len(fields) != 3:
        return reaction_smiles
    return ">".join(".".join(standard_smiles(s) for s in field.split(".")) for field in fields)


def drfp_bitstring(reaction_smiles: str) -> str:
    """Return the DRFP fingerprint of `reaction_smiles` as a `drfp_bits`-long bitstring.

    Standardizes each `.`-separated species before folding, so a query naming a salt, a
    charged species or another tautomer of an indexed compound scores against the same
    standardized form the index was built from (REV-1) — the index already goes through
    `standard_smiles` per component
    (`chemclaw.ingest.eln.ord.OrdReaction.transformation_smiles`), so an unstandardized
    query was comparing against a form it could never equal.

    Raises `FingerprintInputError` if the input is not a valid reaction SMILES (DRFP needs a
    `>>`-separated reaction) or if it yields an empty fingerprint (a degenerate reaction
    with no extracted features is not useful to index or search), so the caller never
    stores or queries a meaningless fingerprint (G4). The narrow type is what lets a caller
    treat an unfingerprintable *argument* as an empty answer without also absorbing a store
    that cannot be searched.
    """
    standardized = _standardize_species(reaction_smiles)
    try:
        folded = DrfpEncoder.encode(standardized, n_folded_length=settings.drfp_bits)[0]
    except Exception as exc:  # DRFP raises its own NoReactionError etc.; normalize it.
        raise FingerprintInputError(
            f"unparseable reaction SMILES: {reaction_smiles!r} ({exc})"
        ) from exc
    bits = "".join("1" if value else "0" for value in folded)
    if "1" not in bits:
        raise FingerprintInputError(f"reaction produced an empty fingerprint: {reaction_smiles!r}")
    return bits


def reaction_definition() -> str:
    """The current DRFP definition signature stored on each reaction row.

    Recorded per row so the store never ranks DRFP bits folded to a different width against
    each other — changing `drfp_bits` and re-indexing can't silently mix incomparable rows.

    `agents-excluded` marks rows built from `OrdReaction.transformation_smiles` — the solvent and
    the catalyst left *out* of the string rather than moved into the agent slot. A row carrying
    either earlier token encoded the solvent as part of the transformation, whichever slot it was
    written in: `DrfpEncoder.internal_encode` folds the agent slot back onto the reactants
    (`sides[0] += "." + sides[1]`), so the `agents` token named a change that produced
    byte-identical bits. Ranking across the two would compare a solvent-dominated fingerprint
    against a solvent-neutral one and report the difference as chemistry, so the token moves and
    the old rows fall out of search until a re-index rebuilds them.
    """
    return f"drfp:b{settings.drfp_bits}:agents-excluded:{STANDARDIZATION_VERSION}"
