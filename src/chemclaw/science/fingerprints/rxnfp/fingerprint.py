"""DRFP reaction fingerprints.

Pure and model-free: a reaction SMILES becomes a DRFP folded to `settings.drfp_bits` and stored as a
fixed-width bitstring, matching a Postgres `bit(drfp_bits)` column like the molecule ECFP4.
"""

from drfp import DrfpEncoder

from chemclaw.core.chem import STANDARDIZATION_VERSION, standard_smiles
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintInputError


def _standardize_species(reaction_smiles: str) -> str:
    """Standardize every `.`-separated species in a `reactants>agents>products` string.

    Every indexed row is built with `standard_smiles` per component, so every query passes through
    here too, or it would score against a form it never shares. All three fields are standardized,
    agents included, because `DrfpEncoder` folds the agent slot onto the reactants; a 3-part string
    and its folded 2-part form must standardize identically.

    A string that does not split into exactly three fields is returned unchanged for `DrfpEncoder`
    to reject.

    Atom maps are cleared by `standard_smiles`, which is load-bearing: DRFP shingles atom
    environments as SMILES text, so a mapped and an unmapped spelling of one reaction would
    otherwise share no bits.
    """
    fields = reaction_smiles.split(">")
    if len(fields) != 3:
        return reaction_smiles
    return ">".join(".".join(standard_smiles(s) for s in field.split(".")) for field in fields)


def drfp_bitstring(reaction_smiles: str) -> str:
    """Return the DRFP fingerprint of `reaction_smiles` as a `drfp_bits`-long bitstring.

    Standardizes each species first so a query matches the form the index was built from.

    Raises `FingerprintInputError` if the input is not a valid reaction SMILES or yields an empty
    fingerprint, so nothing meaningless is stored or queried. The narrow type lets a caller treat a
    bad argument as an empty answer without also absorbing a store failure.
    """
    # Bound each species before DRFP parses it: `DrfpEncoder.encode` runs its own RDKit parse, which
    # the `core/chem` size gate does not reach, and an oversized species can hang or crash it.
    for token in reaction_smiles.replace(">", ".").split("."):
        if len(token) > settings.molecule_max_smiles_length:
            raise FingerprintInputError(
                f"reaction SMILES contains a {len(token)}-character species, over the "
                f"{settings.molecule_max_smiles_length} limit: {reaction_smiles[:80]!r}…"
            )
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

    Recorded per row so the store never ranks bits of different definitions against each other.
    `agents-excluded` marks rows built with solvent and catalyst left out of the string; earlier
    rows encoded them as part of the transformation, so they fall out of search until re-indexed.
    """
    return f"drfp:b{settings.drfp_bits}:agents-excluded:{STANDARDIZATION_VERSION}"
