"""High-level molecule search over a fingerprint store (plan step 3.3).

The two molecule capability entry points the MCP server and agent call:
`find_similar_molecules` (Tanimoto neighbors) and `find_substructure_matches` (molecules
containing a query fragment). Both take the store as a seam so they are backend-agnostic
and testable with the in-memory store. Defaults (top_k, threshold) come from config — the
capability surfaces them; the `reaction-search` skill decides how to set them (G6).
"""

import logging

from pydantic import BaseModel
from rdkit import Chem

from chemclaw.config import settings
from mcp_servers.fpstore import (
    FingerprintError,
    FingerprintRecord,
    FingerprintStore,
    Match,
    find_matches,
)
from mcp_servers.molfp.fingerprint import ecfp_bitstring, molecule_definition

log = logging.getLogger(__name__)


class SubstructureHit(BaseModel):
    """A substructure hit: the stored record's id and SMILES label.

    Deliberately lean — no bits, no definition. The fingerprint is an internal storage
    detail no search consumer uses (the agent wrapper strips to SMILES immediately), and
    returning it would ship ~2KB of '0'/'1' noise per hit into the model context over MCP.
    """

    id: str
    label: str


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


async def find_substructure_matches(store: FingerprintStore, query: str) -> list[SubstructureHit]:
    """Return stored molecules that contain the `query` fragment.

    The query is interpreted as SMARTS (the right language for a substructure pattern; a
    plain SMILES is also valid SMARTS), with a SMILES parse as a fallback for the rare
    string that fails as SMARTS. Exact RDKit matching over the corpus — a structural
    filter, not a similarity score. Guards on the model-supplied query (G4/SEC-4): its
    length is bounded by `substructure_query_max_length` (SMARTS matching is subgraph
    isomorphism, worst-case exponential, run in-process with no statement_timeout analog)
    and an empty/zero-atom pattern is rejected rather than silently matching nothing
    (RDKit parses "" to a 0-atom pattern, which would read as "no precedent exists").
    The scan is bounded to `substructure_scan_max_records` (a full-table load into the
    worker heap is the failure mode) and the result to `fingerprint_max_top_k` (a broad
    fragment like "C" matches essentially every organic molecule — an unbounded hit list
    would flood the model context); hitting either cap logs a warning so a truncated
    result is never silent. A pattern-fingerprint prefilter is a later optimization for
    large corpora (ECFP bits cannot screen substructures soundly).
    """
    max_length = settings.substructure_query_max_length
    if len(query) > max_length:
        raise FingerprintError(
            f"substructure query exceeds {max_length} characters ({len(query)}); "
            "pass a smaller fragment (or raise CHEMCLAW_SUBSTRUCTURE_QUERY_MAX_LENGTH)"
        )
    pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
    if pattern is None:
        raise FingerprintError(f"unparseable substructure query: {query!r}")
    if pattern.GetNumAtoms() == 0:
        raise FingerprintError(f"empty substructure query (no atoms): {query!r}")
    cap = settings.substructure_scan_max_records
    records = await store.all_records(limit=cap)
    if len(records) == cap:
        log.warning(
            "substructure scan hit the %d-record cap; matches may be incomplete "
            "(raise CHEMCLAW_SUBSTRUCTURE_SCAN_MAX_RECORDS or narrow the corpus)",
            cap,
        )
    max_matches = settings.fingerprint_max_top_k
    matches: list[SubstructureHit] = []
    for record in records:
        mol = Chem.MolFromSmiles(record.label)
        if mol is not None and mol.HasSubstructMatch(pattern):
            matches.append(SubstructureHit(id=record.id, label=record.label))
            if len(matches) == max_matches:
                log.warning(
                    "substructure result capped at %d matches (id order); "
                    "narrow the query or raise CHEMCLAW_FINGERPRINT_MAX_TOP_K",
                    max_matches,
                )
                break
    return matches
