"""High-level molecule search over a fingerprint store.

`find_similar_molecules` (Tanimoto neighbors) and `find_substructure_matches` (molecules containing
a fragment). Both take the store as a seam. Defaults come from config; the `reaction-search` skill
decides how to set them.
"""

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

    The inverse of `MoleculeHit.for_molecule`, for `expand_note` on a hit id with no note behind it
    (e.g. a molecule indexed from an ELN record). A scan, since the id is a hash: over the same
    capped slice `substructure_matches` reads, hashing each stored (already standardized) label with
    `compound_id_of_standard`. Runs only when the id names no note.
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

    `top_k` and `threshold` default to the configured values. Raises `FingerprintError` on an
    unparseable query.

    Returns a `FingerprintSearch` so an empty answer says why: `index_empty` (nothing indexed),
    `hits_truncated` (more cleared the threshold than `top_k` holds), `approximate` (the store
    searched approximately, so empty is not proof of absence), and `index_partial` (a definition
    rebuild is in progress and only part of the corpus is comparable).
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

    The query is SMARTS (a SMILES is valid SMARTS), with a SMILES parse as fallback; exact RDKit
    matching, not a score. Guards on the model-supplied query: length is bounded by
    `substructure_query_max_length` (subgraph isomorphism is worst-case exponential) and an empty
    pattern is rejected. The scan is capped at `substructure_scan_max_records` and the result at
    `fingerprint_max_top_k`; hitting either is reported in the result
    (`scan_truncated`/`hits_truncated`, `verdict`) because the model never sees the log.
    `index_partial` is not set: this reads stored SMILES, not bits, so the whole corpus is searched
    regardless of fingerprint definition.

    Matching uses the cached `substructure_index` (`SubstructLibrary`), falling back to
    `_match_record_by_record` when no index is available within this caller's bounds; the index
    build has its own budget (`substructure_index_build_timeout_seconds`), separate from the match
    budget.

    Matching runs in a worker thread bounded by `substructure_match_timeout_seconds`, and the bound
    is passed in as a deadline the scan checks itself, since `asyncio.wait_for` cannot stop a thread
    and the default executor is shared (e.g. with token validation). RDKit cannot be interrupted
    mid-call, so the scan works in chunks sized in time (`substructure_scan_deadline_slice_seconds`;
    see `CorpusIndex.labels_matching`). On timeout the caller gets a `FingerprintError` naming the
    bound. Each hit carries the compound note to cite.
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

    `hits_truncated` says the count is a floor; `unreadable` says the corpus was not fully examined,
    so a miss is not a negative. No record count is carried: the two scan paths count differently,
    and the bounded run's count is on `ScanDeadlineExceeded.reached`.
    """

    hits: list[MoleculeHit]
    hits_truncated: bool
    unreadable: int


def _scan_for_matches(
    records: list[FingerprintRecord], pattern: Chem.Mol, deadline: float
) -> ScanOutcome:
    """Match `pattern` against the corpus slice, stopping at the result cap or at `deadline`.

    Synchronous so it runs in a worker thread, index build included. Unparseable records are skipped
    and counted, so a malformed row cannot turn a search into a false negative. The search asks for
    one more hit than it may return (`maxResults=cap + 1`), so truncation is observed rather than
    inferred. Past the deadline it raises rather than returning a partial scan as a result.

    Args:
        records: The capped corpus slice to match, in id order.
        pattern: The compiled query.
        deadline: `time.monotonic()` value past which the scan stops.

    Returns:
        The hits, whether a further match was found and dropped, and how many stored rows could not
        be parsed into the index at all.

    Raises:
        ScanDeadlineExceeded: The deadline passed before every record was examined. A
            `TimeoutError`, so the caller handles it like `asyncio.wait_for`'s; it carries
            `reached`.
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

    The fallback when no index is available (corpus too large to index in budget, another thread
    building it, no time to build). It keeps parsing past the hit cap so `unreadable` covers the
    whole slice, matching the indexed path's meaning of `scan_truncated`; only the subgraph matching
    stops at the cap.

    Args:
        records: The capped corpus slice to match, in id order.
        pattern: The compiled query.
        limit: How many matches to collect before matching stops — the result cap plus one.
        deadline: `time.monotonic()` value past which the scan stops.

    Returns:
        The matching labels in stored order, at most `limit` of them, and how many stored rows could
        not be parsed at all.

    Raises:
        ScanDeadlineExceeded: The deadline passed before every candidate was examined. The same
            class the indexed path raises; `total` counts every record here, and `because`
            distinguishes the messages since the remedies differ.
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
