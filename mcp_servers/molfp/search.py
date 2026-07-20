"""High-level molecule search over a fingerprint store (plan step 3.3).

The two capability entry points the MCP server and agent call: `find_similar_molecules`
(Tanimoto neighbors) and `find_substructure_matches` (molecules containing a query
fragment). Both take the store as a seam so they are backend-agnostic and testable with
the in-memory store. Defaults (top_k, threshold) come from config — the capability
surfaces them; the `reaction-search` skill decides how to set them per task (G6).
"""

from rdkit import Chem

from chemclaw.config import settings
from mcp_servers.molfp.fingerprint import FingerprintError, ecfp_bitstring
from mcp_servers.molfp.store import FingerprintStore, Match, MoleculeRecord


def record_for(record_id: str, smiles: str) -> MoleculeRecord:
    """Build a `MoleculeRecord` (id + SMILES + freshly computed ECFP4) for insertion."""
    return MoleculeRecord(id=record_id, smiles=smiles, bits=ecfp_bitstring(smiles))


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
    query_bits = ecfp_bitstring(smiles)
    return await store.find_similar(
        query_bits,
        top_k if top_k is not None else settings.fingerprint_top_k,
        threshold if threshold is not None else settings.fingerprint_similarity_threshold,
    )


async def find_substructure_matches(store: FingerprintStore, query: str) -> list[MoleculeRecord]:
    """Return stored molecules that contain the `query` fragment (SMARTS or SMILES).

    Exact RDKit substructure matching over the corpus — a structural filter, not a
    similarity score. v1 scans all records; a pattern-fingerprint prefilter is a later
    optimization for large corpora (ECFP bits cannot screen substructures soundly).
    """
    pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
    if pattern is None:
        raise FingerprintError(f"unparseable substructure query: {query!r}")
    matches: list[MoleculeRecord] = []
    for record in await store.all_records():
        mol = Chem.MolFromSmiles(record.smiles)
        if mol is not None and mol.HasSubstructMatch(pattern):
            matches.append(record)
    return matches
