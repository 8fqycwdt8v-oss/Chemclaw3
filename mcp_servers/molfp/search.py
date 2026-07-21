"""High-level molecule search over a fingerprint store (plan step 3.3).

The two molecule capability entry points the MCP server and agent call:
`find_similar_molecules` (Tanimoto neighbors) and `find_substructure_matches` (molecules
containing a query fragment). Both take the store as a seam so they are backend-agnostic
and testable with the in-memory store. Defaults (top_k, threshold) come from config — the
capability surfaces them; the `reaction-search` skill decides how to set them (G6).
"""

from rdkit import Chem

from mcp_servers.fpstore import (
    FingerprintError,
    FingerprintRecord,
    FingerprintStore,
    Match,
    find_matches,
)
from mcp_servers.molfp.fingerprint import ecfp_bitstring, molecule_definition


def record_for(record_id: str, smiles: str) -> FingerprintRecord:
    """Build a `FingerprintRecord` (id + SMILES label + ECFP4 + its definition signature)."""
    return FingerprintRecord(
        id=record_id, label=smiles, bits=ecfp_bitstring(smiles), definition=molecule_definition()
    )


async def find_similar_molecules(
    store: FingerprintStore,
    smiles: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[Match]:
    """Return molecules structurally similar to `smiles`, most similar first.

    `top_k` and `threshold` default to the configured values. Raises `FingerprintError`
    on an unparseable query so the caller never searches with a meaningless fingerprint.
    """
    return await find_matches(store, ecfp_bitstring(smiles), top_k, threshold)


async def find_substructure_matches(store: FingerprintStore, query: str) -> list[FingerprintRecord]:
    """Return stored molecules that contain the `query` fragment.

    The query is interpreted as SMARTS (the right language for a substructure pattern; a
    plain SMILES is also valid SMARTS), with a SMILES parse as a fallback for the rare
    string that fails as SMARTS. Exact RDKit matching over the corpus — a structural
    filter, not a similarity score. v1 scans all records; a pattern-fingerprint prefilter
    is a later optimization for large corpora (ECFP bits cannot screen substructures
    soundly).
    """
    pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
    if pattern is None:
        raise FingerprintError(f"unparseable substructure query: {query!r}")
    matches: list[FingerprintRecord] = []
    for record in await store.all_records():
        mol = Chem.MolFromSmiles(record.label)
        if mol is not None and mol.HasSubstructMatch(pattern):
            matches.append(record)
    return matches
