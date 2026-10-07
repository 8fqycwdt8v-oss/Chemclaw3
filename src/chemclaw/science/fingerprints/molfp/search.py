"""Molecule search over a fingerprint store: Tanimoto neighbours and substructure matches."""

import asyncio
import logging
import time
from typing import NamedTuple

from pydantic import BaseModel
from rdkit import Chem

from chemclaw.core.chem import (
    InvalidSmilesError,
    compound_id,
    compound_id_of_standard,
    substructure_pattern,
)
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.molfp.substructure_index import (
    ScanDeadlineExceeded,
    index_for,
)
from chemclaw.science.fingerprints.store import (
    FingerprintError,
    FingerprintRecord,
    FingerprintSearch,
    FingerprintStore,
    find_matches,
    index_is_empty,
    index_is_partial,
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

    `compound_note_id` is the structure's id, and **it resolves whether or not a note was ever
    written under it**: `expand_note` reads a written note first, then a note filed under another
    id that carries this structure, and otherwise the structure itself back out of this index
    (`indexed_structure`). It used to name "the note an ingest produces", and since an ELN run
    became a record rather than a note no ingest produces one, so most hits cited nothing. It is
    `None` when the stored structure does not parse: ingestion
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


async def indexed_structure(store: FingerprintStore, note_id: str) -> str | None:
    """The indexed structure whose `compound_note_id` is `note_id`, or `None` if none is.

    For `expand_note` on a hit with no note behind it; scans the capped slice hashing each stored
    label.
    """
    for record in await store.all_records(limit=settings.substructure_scan_max_records):
        if compound_id_of_standard(record.label) == note_id:
            return record.label
    return None


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
) -> FingerprintSearch[MoleculeHit]:
    """Return molecules structurally similar to `smiles`, most similar first.

    Raises `FingerprintError` on an unparseable query. The `FingerprintSearch` result says why it
    might be empty or short: `index_empty`, `hits_truncated`, `approximate`, `index_partial`.
    """
    matches, truncated = await find_matches(store, ecfp_bitstring(smiles), top_k, threshold)
    return FingerprintSearch[MoleculeHit](
        subject="molecule",
        hits=[MoleculeHit.for_molecule(match.label, match.similarity) for match in matches],
        index_empty=await index_is_empty(store, matches),
        index_partial=await index_is_partial(store),
        hits_truncated=truncated,
        approximate=store.approximate,
    )


async def find_substructure_matches(
    store: FingerprintStore, query: str
) -> FingerprintSearch[MoleculeHit]:
    """Return stored molecules that contain the `query` fragment.

    SMARTS (SMILES as fallback), matched exactly with RDKit. Query length
    (`substructure_query_max_length`), scan size (`substructure_scan_max_records`) and result count
    (`fingerprint_max_top_k`) are bounded, and truncation is reported in the result. Uses the cached
    substructure index when available, else `_match_record_by_record`.

    Matching runs in a worker thread under `substructure_match_timeout_seconds`, passed in as a
    deadline the scan checks between time-sized chunks, since a thread cannot be cancelled. On
    timeout the caller gets a `FingerprintError` naming the bound.
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
    # Read one row past the cap to observe truncation; a corpus exactly at the cap is complete.
    probed = await store.all_records(limit=cap + 1)
    scan_truncated = len(probed) > cap
    records = probed[:cap]
    if scan_truncated:
        log.warning(
            "substructure scan hit the %d-record cap; matches may be incomplete "
            "(raise CHEMCLAW_SUBSTRUCTURE_SCAN_MAX_RECORDS or narrow the corpus)",
            cap,
        )
    timeout = settings.substructure_match_timeout_seconds
    try:
        # Both halves of one bound: the deadline stops the worker between chunks; `wait_for`
        # releases the caller if one chunk outlasts it. Both raise `TimeoutError`.
        scan = await asyncio.wait_for(
            asyncio.to_thread(_scan_for_matches, records, pattern, time.monotonic() + timeout),
            timeout=timeout,
        )
    except TimeoutError as exc:
        # Name which scan gave up (indexing or matching) and how far it got, since the remedies
        # differ; a bare `wait_for` timeout carries no message.
        gave_up = str(exc) or "the scan was still inside one chunk when the bound passed"
        raise FingerprintError(
            f"substructure search for {query!r} exceeded {timeout}s over {len(records)} "
            f"molecule(s): {gave_up}. "
            "Narrow the pattern, lower CHEMCLAW_SUBSTRUCTURE_SCAN_MAX_RECORDS, or raise "
            "CHEMCLAW_SUBSTRUCTURE_MATCH_TIMEOUT_SECONDS"
        ) from exc
    if scan.unreadable:
        log.warning(
            "%d stored molecule(s) could not be parsed and were not matched; the scan is "
            "reported as incomplete",
            scan.unreadable,
        )
    return FingerprintSearch[MoleculeHit](
        subject="molecule",
        hits=scan.hits,
        index_empty=not records,
        # An unparseable stored row was never matched, which is what `scan_truncated` means: a miss
        # is not a negative.
        scan_truncated=scan_truncated or bool(scan.unreadable),
        hits_truncated=scan.hits_truncated,
    )


class ScanOutcome(NamedTuple):
    """What one substructure pass found, and the two ways it fell short of the whole corpus.

    `hits_truncated`: the count is a floor. `unreadable`: some rows were not examined, so a miss is
    not a negative.
    """

    hits: list[MoleculeHit]
    hits_truncated: bool
    unreadable: int


def _scan_for_matches(
    records: list[FingerprintRecord], pattern: Chem.Mol, deadline: float
) -> ScanOutcome:
    """Match `pattern` against the corpus slice, stopping at the result cap or at `deadline`.

    Synchronous (worker thread). Asks for `cap + 1` hits so truncation is observed. Unparseable rows
    are skipped and counted.

    Raises:
        ScanDeadlineExceeded: The deadline passed first; a `TimeoutError` carrying `reached`.
    """
    max_matches = settings.fingerprint_max_top_k
    index = index_for(records, deadline)
    if index is None:
        found, unreadable = _match_record_by_record(records, pattern, max_matches + 1, deadline)
    else:
        found = index.labels_matching(pattern, max_matches + 1, deadline)
        unreadable = index.unreadable
    hits_truncated = len(found) > max_matches
    if hits_truncated:
        log.warning(
            "substructure result capped at %d matches (id order); "
            "narrow the query or raise CHEMCLAW_FINGERPRINT_MAX_TOP_K",
            max_matches,
        )
    return ScanOutcome(
        [MoleculeHit.for_molecule(label) for label in found[:max_matches]],
        hits_truncated,
        unreadable,
    )


def _match_record_by_record(
    records: list[FingerprintRecord], pattern: Chem.Mol, limit: int, deadline: float
) -> tuple[list[str], int]:
    """Match `pattern` by parsing each stored SMILES in turn — the scan with no index behind it.

    Keeps parsing past the hit cap so `unreadable` covers the whole slice, as on the indexed path.

    Raises:
        ScanDeadlineExceeded: The deadline passed first; `because` names this path.
    """
    found: list[str] = []
    unreadable = 0
    for examined, record in enumerate(records):
        if time.monotonic() >= deadline:
            raise ScanDeadlineExceeded(
                examined,
                len(records),
                "matching them one at a time because no index was available",
            )
        molecule = Chem.MolFromSmiles(record.label)
        if molecule is None:
            unreadable += 1
            continue
        if len(found) < limit and molecule.HasSubstructMatch(pattern):
            found.append(record.label)
    return found, unreadable
