"""DRFP reaction fingerprints — the reaction capability core (plan step 3.4).

Pure, GPU-free, model-free: a reaction SMILES (`reactants>>products`) becomes a DRFP
(Differential Reaction FingerPrint) folded to `settings.drfp_bits` and stored as a
fixed-width bitstring, so it maps onto a Postgres `bit(drfp_bits)` column exactly like the
molecule ECFP4. DRFP is the reaction-specific "reaction SMILES → bits" step; ranking and
the store are the shared, domain-neutral `chemclaw.science.fingerprints.store`.
"""

from drfp import DrfpEncoder

from chemclaw.core.chem import STANDARDIZATION_VERSION
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintError


def drfp_bitstring(reaction_smiles: str) -> str:
    """Return the DRFP fingerprint of `reaction_smiles` as a `drfp_bits`-long bitstring.

    Raises `FingerprintError` if the input is not a valid reaction SMILES (DRFP needs a
    `>>`-separated reaction) or if it yields an empty fingerprint (a degenerate reaction
    with no extracted features is not useful to index or search), so the caller never
    stores or queries a meaningless fingerprint (G4).
    """
    try:
        folded = DrfpEncoder.encode(reaction_smiles, n_folded_length=settings.drfp_bits)[0]
    except Exception as exc:  # DRFP raises its own NoReactionError etc.; normalize it.
        raise FingerprintError(f"unparseable reaction SMILES: {reaction_smiles!r} ({exc})") from exc
    bits = "".join("1" if value else "0" for value in folded)
    if "1" not in bits:
        raise FingerprintError(f"reaction produced an empty fingerprint: {reaction_smiles!r}")
    return bits


def reaction_definition() -> str:
    """The current DRFP definition signature stored on each reaction row.

    Recorded per row so the store never ranks DRFP bits folded to a different width against
    each other — changing `drfp_bits` and re-indexing can't silently mix incomparable rows.

    `agents` marks reaction SMILES built in the three-part form, with solvent and catalyst in the
    agent slot rather than among the reactants (`OrdReaction.reaction_smiles`). A row indexed under
    the old two-part form encoded the solvent as part of the transformation, so its bits are not
    comparable with a row that does not — ranking across them would compare a solvent-dominated
    fingerprint against a solvent-neutral one and report the difference as chemistry.
    """
    return f"drfp:b{settings.drfp_bits}:agents:{STANDARDIZATION_VERSION}"
