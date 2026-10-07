"""High-level reaction search over a fingerprint store.

`find_similar_reactions` ranks Tanimoto neighbours over DRFP, with the store as a seam. Similarity
only: DRFP is a whole-reaction difference fingerprint, not a substructure screen.
"""

from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring, reaction_definition
from chemclaw.science.fingerprints.store import (
    FingerprintRecord,
    FingerprintSearch,
    FingerprintStore,
    Match,
    find_matches,
    index_is_empty,
    index_is_partial,
)


def record_for_reaction(record_id: str, reaction_smiles: str) -> FingerprintRecord:
    """Build a `FingerprintRecord` (id + reaction-SMILES label + DRFP + its definition)."""
    return FingerprintRecord(
        id=record_id,
        label=reaction_smiles,
        bits=drfp_bitstring(reaction_smiles),
        definition=reaction_definition(),
    )


async def find_similar_reactions(
    store: FingerprintStore,
    reaction_smiles: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> FingerprintSearch[Match]:
    """Return reactions similar to `reaction_smiles`, most similar first.

    `top_k` and `threshold` default to the configured values. Raises `FingerprintError` on an
    invalid reaction.

    Returns a `FingerprintSearch` rather than a list, so an empty answer is not read as "no
    precedent": `index_empty`, `index_partial` (mid-rebuild after a definition change),
    `hits_truncated` (a full page is a floor) and `approximate` (approximate index search) qualify
    it.
    """
    matches, truncated = await find_matches(
        store, drfp_bitstring(reaction_smiles), top_k, threshold
    )
    return FingerprintSearch[Match](
        subject="reaction",
        hits=matches,
        index_empty=await index_is_empty(store, matches),
        index_partial=await index_is_partial(store),
        hits_truncated=truncated,
        approximate=store.approximate,
    )
