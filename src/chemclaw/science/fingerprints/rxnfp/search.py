"""High-level reaction search over a fingerprint store (plan step 3.4).

The reaction capability entry point: `find_similar_reactions` (Tanimoto neighbors over
DRFP). Takes the store as a seam so it is backend-agnostic and testable with the in-memory
store. Reactions have no substructure search — DRFP is a whole-reaction difference
fingerprint, not a substructure screen — so this module exposes similarity only.
"""

from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring, reaction_definition
from chemclaw.science.fingerprints.store import (
    FingerprintRecord,
    FingerprintSearch,
    FingerprintStore,
    Match,
    find_matches,
    index_is_empty,
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

    `top_k` and `threshold` default to the configured values. Raises `FingerprintError`
    on an invalid reaction so the caller never searches with a meaningless fingerprint.

    Returns a `FingerprintSearch`, not a bare list: this is the tool a chemist asks "have we ever
    run something like this?", and an unindexed corpus must not answer it with "no" (see that
    model's docstring for the live run that did exactly that). `hits_truncated` answers the other
    half of the same question — a page of `top_k` out of more that qualified is a floor, not the
    number of precedents on file. `approximate` answers the third: a deployment may search the
    index approximately (`fingerprint_search_exactness`), and then even a complete-looking page is
    the best the index proposed rather than the best on file.
    """
    matches, truncated = await find_matches(
        store, drfp_bitstring(reaction_smiles), top_k, threshold
    )
    return FingerprintSearch[Match](
        subject="reaction",
        hits=matches,
        index_empty=await index_is_empty(store, matches),
        hits_truncated=truncated,
        approximate=store.approximate,
    )
