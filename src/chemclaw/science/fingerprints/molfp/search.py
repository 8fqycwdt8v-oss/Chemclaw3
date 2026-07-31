"""High-level molecule search over a fingerprint store (plan step 3.3).

The two molecule capability entry points the MCP server and agent call:
`find_similar_molecules` (Tanimoto neighbors) and `find_substructure_matches` (molecules
containing a query fragment). Both take the store as a seam so they are backend-agnostic
and testable with the in-memory store. Defaults (top_k, threshold) come from config — the
capability surfaces them; the `reaction-search` skill decides how to set them (G6).
"""

import asyncio
import logging

from pydantic import BaseModel
from rdkit import Chem

from chemclaw.core.chem import InvalidSmilesError, compound_id, substructure_pattern
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.store import (
    FingerprintError,
    FingerprintRecord,
    FingerprintStore,
    find_matches,
)

log = logging.getLogger(__name__)


class MoleculeHit(BaseModel):
    """A molecule hit: the compound note to cite, the structure, and (for similarity) its score.

    Deliberately lean — no bits, no definition. The fingerprint is an internal storage
    detail no search consumer uses, and returning it would ship ~2KB of '0'/'1' noise per
    hit into the model context over MCP. The stored record id is dropped for the same
    reason: for molecules it is the SMILES again (`ingest.eln.ingest` keys the index by
    structure), so it carried no information the `smiles` field does not.

    **Why the note id is on the hit.** Reaction hits have always carried `reaction-<id>`,
    and molecule hits carried nothing, so the model was told to bridge by re-running
    `find_notes` on each SMILES — the literal substring path KM-4 flags as fragile. Compound
    notes now exist with structure-derived ids, so the citation is simply computed here.

    `compound_note_id` names the note a *merged* ingest produces. A structure whose note is
    still on its PR-gate branch is proposed, not merged, and the citation will not resolve
    until it is — the same latency `reaction_note_id` has always had, and the reason
    `eln.compound.compound_dependencies` makes a note land together with the compound notes
    it depends on (STO-7). It is `None` when the stored structure does not parse: ingestion
    canonicalizes leniently, so a junk label can reach the index, and one unciteable row must
    not raise out of a search that has real hits to return.
    """

    compound_note_id: str | None
    smiles: str
    similarity: float | None = None

    @classmethod
    def for_molecule(cls, smiles: str, similarity: float | None = None) -> "MoleculeHit":
        """Build a hit for a stored structure, deriving the note id from the structure itself."""
        try:
            note_id: str | None = compound_id(smiles)
        except InvalidSmilesError:
            log.warning("indexed molecule %r does not parse; hit cites no compound note", smiles)
            note_id = None
        return cls(compound_note_id=note_id, smiles=smiles, similarity=similarity)


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
) -> list[MoleculeHit]:
    """Return molecules structurally similar to `smiles`, most similar first.

    `top_k` and `threshold` default to the configured values. Raises `FingerprintError`
    on an unparseable query so the caller never searches with a meaningless fingerprint.
    """
    matches = await find_matches(store, ecfp_bitstring(smiles), top_k, threshold)
    return [MoleculeHit.for_molecule(match.label, match.similarity) for match in matches]


async def find_substructure_matches(store: FingerprintStore, query: str) -> list[MoleculeHit]:
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

    Each hit carries the compound note to cite (`MoleculeHit`), so a functional-group query
    lands on the graph directly instead of via a substring search for the SMILES.

    The matching itself runs **off the event loop** in a worker thread, bounded by
    `substructure_match_timeout_seconds`. Bounding the inputs is not enough: a short but
    adversarial recursive SMARTS can still match for minutes, and this call is served by the
    async front door, so an in-loop scan would stall *every* session's stream, not just its
    own. On timeout the caller gets a `FingerprintError` naming the bound. Honest limit: the
    timeout releases the event loop and the caller — it cannot kill the RDKit thread, which
    holds one CPU until the pattern completes (RDKit exposes no interruption hook).
    """
    max_length = settings.substructure_query_max_length
    if len(query) > max_length:
        raise FingerprintError(
            f"substructure query exceeds {max_length} characters ({len(query)}); "
            "pass a smaller fragment (or raise CHEMCLAW_SUBSTRUCTURE_QUERY_MAX_LENGTH)"
        )
    try:
        pattern = substructure_pattern(query)
    except InvalidSmilesError as exc:
        # Re-raised as this module's own error so the connector's failure type is unchanged; the
        # *rule* for what a valid pattern is now lives in one place (`core.chem`).
        raise FingerprintError(str(exc)) from exc
    cap = settings.substructure_scan_max_records
    records = await store.all_records(limit=cap)
    if len(records) == cap:
        log.warning(
            "substructure scan hit the %d-record cap; matches may be incomplete "
            "(raise CHEMCLAW_SUBSTRUCTURE_SCAN_MAX_RECORDS or narrow the corpus)",
            cap,
        )
    timeout = settings.substructure_match_timeout_seconds
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_scan_for_matches, records, pattern), timeout=timeout
        )
    except TimeoutError as exc:
        raise FingerprintError(
            f"substructure match for {query!r} exceeded {timeout}s over {len(records)} molecules; "
            "narrow the pattern (or raise CHEMCLAW_SUBSTRUCTURE_MATCH_TIMEOUT_SECONDS)"
        ) from exc


def _scan_for_matches(records: list[FingerprintRecord], pattern: Chem.Mol) -> list[MoleculeHit]:
    """Match `pattern` against each record, stopping at the result cap (the CPU-bound half).

    Split out as a plain synchronous function so it can run in a worker thread: it is the only
    part of the search that burns CPU, and keeping it separate makes the async wrapper's one
    responsibility — bounding it — obvious. A record whose stored SMILES no longer parses is
    skipped rather than aborting the scan (one bad row must not hide every real hit).
    """
    max_matches = settings.fingerprint_max_top_k
    matches: list[MoleculeHit] = []
    for record in records:
        mol = Chem.MolFromSmiles(record.label)
        if mol is not None and mol.HasSubstructMatch(pattern):
            matches.append(MoleculeHit.for_molecule(record.label))
            if len(matches) == max_matches:
                log.warning(
                    "substructure result capped at %d matches (id order); "
                    "narrow the query or raise CHEMCLAW_FINGERPRINT_MAX_TOP_K",
                    max_matches,
                )
                break
    return matches
