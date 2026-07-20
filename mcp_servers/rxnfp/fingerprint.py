"""DRFP reaction fingerprints — the reaction capability core (plan step 3.4).

Pure, GPU-free, model-free: a reaction SMILES (`reactants>>products`) becomes a DRFP
(Differential Reaction FingerPrint) folded to `settings.drfp_bits` and stored as a
fixed-width bitstring, so it maps onto a Postgres `bit(drfp_bits)` column exactly like the
molecule ECFP4. DRFP is the reaction-specific "reaction SMILES → bits" step; ranking and
the store are the shared, domain-neutral `mcp_servers.fpstore`.
"""

from drfp import DrfpEncoder

from chemclaw.config import settings
from mcp_servers.fpstore import FingerprintError


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
