"""High-level reaction search over a fingerprint store (plan step 3.4).

The reaction capability entry point: `find_similar_reactions` (Tanimoto neighbors over
DRFP). Takes the store as a seam so it is backend-agnostic and testable with the in-memory
store. Reactions have no substructure search — DRFP is a whole-reaction difference
fingerprint, not a substructure screen — so this module exposes similarity only.
"""

from mcp_servers.fpstore import FingerprintRecord, FingerprintStore, Match, find_matches
from mcp_servers.rxnfp.fingerprint import drfp_bitstring


def record_for_reaction(record_id: str, reaction_smiles: str) -> FingerprintRecord:
    """Build a `FingerprintRecord` (id + reaction-SMILES label + freshly computed DRFP)."""
    return FingerprintRecord(
        id=record_id, label=reaction_smiles, bits=drfp_bitstring(reaction_smiles)
    )


async def find_similar_reactions(
    store: FingerprintStore,
    reaction_smiles: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[Match]:
    """Return reactions similar to `reaction_smiles`, most similar first.

    `top_k` and `threshold` default to the configured values. Raises `FingerprintError`
    on an invalid reaction so the caller never searches with a meaningless fingerprint.
    """
    return await find_matches(store, drfp_bitstring(reaction_smiles), top_k, threshold)
