"""High-level molecule search over a fingerprint store (plan step 3.3).

The two molecule capability entry points the MCP server and agent call:
`find_similar_molecules` (Tanimoto neighbors) and `find_substructure_matches` (molecules
containing a query fragment). Both take the store as a seam so they are backend-agnostic
and testable with the in-memory store. Defaults (top_k, threshold) come from config — the
capability surfaces them; the `reaction-search` skill decides how to set them (G6).
"""

import asyncio
import logging
import time
from typing import NamedTuple

from pydantic import BaseModel
from rdkit import Chem

from chemclaw.core.chem import InvalidSmilesError, compound_id, substructure_pattern
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.molfp.substructure_index import index_for
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

    `compound_note_id` names the note an ingest produces. A structure indexed before its note has
    been written has no note for the citation to resolve to yet — a smaller window than under the
    PR-gate this replaces, where it lasted until a human merged, but not a closed one: the ingest
    indexes and writes in separate steps. It is the same latency `reaction_note_id` has always had,
    and the reason
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
) -> FingerprintSearch[MoleculeHit]:
    """Return molecules structurally similar to `smiles`, most similar first.

    `top_k` and `threshold` default to the configured values. Raises `FingerprintError`
    on an unparseable query so the caller never searches with a meaningless fingerprint.

    Returns a `FingerprintSearch`, not a bare list, so "we have no analog on file" and "nothing
    has been indexed" cannot arrive as the same empty list — see that model's docstring. For the
    same reason a full page carries `hits_truncated` when more molecules cleared the threshold than
    `top_k` could hold: the page is a floor, and it read as a total. `approximate` is the third
    member of that family and comes straight off the store: a deployment may search the index
    approximately (`fingerprint_search_exactness`), and under that trade an empty result is no
    longer evidence that we have no analog on file. `index_partial` is the fourth, and it is the
    one that survives a *definition* change rather than a configuration: a corpus mid-rebuild
    holds rows this store cannot compare, and one rebuilt row is enough to make `index_empty`
    False while the search still answers over a fraction of the corpus.
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
    would flood the model context). **`index_partial` is deliberately not set here and its
    absence is not an omission**: this scan reads `all_records`, which is unfiltered by
    definition on purpose (a stale-definition row's stored SMILES is still a correct
    substructure hit), so a corpus mid-rebuild is searched whole by this entry point and only
    by this one. Hitting either cap is reported **in the result**
    (`scan_truncated`/`hits_truncated`, and the `verdict` sentence built from them), not only in
    the log: the log is read by an operator after the fact, while the payload is what the model
    holds when it writes the answer, and a scan the record cap cut short used to render as "this
    is a genuine negative result".

    **The matching itself no longer re-parses the corpus.** It used to: one `Chem.MolFromSmiles`
    per stored record per query, which is 326 ms over a 4,991-molecule corpus before any chemistry
    happens. `substructure_index` holds that slice pre-parsed in RDKit's `SubstructLibrary`,
    screened by pattern fingerprint in C++ — 12.9 ms for the same query, 8-25x across the four
    query classes measured there, under the conditions that module records. It is only a win
    because the index is **cached across queries**: building it costs 1,155 ms, so a per-query
    rebuild would be three times *slower* than the loop it replaces, and the cache's key and its
    bound are that module's subject. This is the in-process
    index, not `docs/planning/DEFERRED.md`'s `pattern_bits` GIN screen, which is a database-side
    prefilter for a corpus far past this cap; that row stays open.

    **And the loop is still here, because an index is an optimisation and a search is not.**
    `index_for` returns `None` when there is no index to be had inside this caller's bounds, and
    `_match_record_by_record` then answers exactly as this function did before the index existed.
    What that fixes is a corpus past roughly 20,000 records, which could previously never be
    searched at all: the build was charged against `substructure_match_timeout_seconds`, nothing is
    cached when a build is abandoned, and so every retry restarted from zero — measured, three
    failures out of three against 2.01 s and 2,684 hits for the loop over the same corpus. The
    build now has its own budget (`substructure_index_build_timeout_seconds`), which is the
    separation the defect was: how long an index may take to build and how long a chemist waits for
    an answer were one number.

    Each hit carries the compound note to cite (`MoleculeHit`), so a functional-group query
    lands on the graph directly instead of via a substring search for the SMILES.

    Returns a `FingerprintSearch` for the same reason similarity search does: "no indexed molecule
    bears this group" and "nothing is indexed" are different answers. Here the distinction is free
    — the scan already holds every record it could have matched — and it is *not* scoped to the
    store's fingerprint definition, because this search re-matches the stored SMILES with RDKit and
    never touches the bits, so a stale-definition row is still a real molecule in the corpus.

    The matching itself runs **off the event loop** in a worker thread, bounded by
    `substructure_match_timeout_seconds`. Bounding the inputs is not enough: a short but
    adversarial recursive SMARTS can still match for minutes, and this call is served by the
    async front door, so an in-loop scan would stall *every* session's stream, not just its
    own. On timeout the caller gets a `FingerprintError` naming the bound.

    **The bound is carried into the worker as a deadline, not only awaited from outside it.**
    `asyncio.wait_for` releases the caller and cannot stop the thread, so a scan that outran the
    bound used to run the *whole remaining corpus* out in the background — measured, a 60-character
    SMARTS against a corpus of 121-atom hyperbranched molecules costs 343 ms *per molecule*, so a
    5 s bound over the shipped 5 000-record cap orphans a thread for ~28 minutes. Those threads come
    from the loop's default executor, which is also where `chemclaw.api.auth` validates every bearer
    token. `_scan_for_matches` therefore checks the deadline itself and gives up there. Honest
    limit, narrowed rather than removed: RDKit exposes no interruption hook, so the thread still
    runs to the end of the work it is inside when the deadline passes.

    **What "the work it is inside" now means is one chunk rather than one molecule**, because the
    matching runs through a C++ `GetMatches` call that cannot be interrupted mid-way. The chunk is
    sized in *time* rather than in records — `substructure_scan_deadline_slice_seconds` of work at
    the rate the preceding chunk actually ran at, starting from a single record — so on the
    pathological corpus the measurement above came from it settles at one or two molecules, which is
    the granularity this paragraph has always claimed, while an ordinary corpus is covered in about
    seven calls. A fixed number of records would have been the wrong unit, for the same reason the
    bound exists: per-molecule cost spans five orders of magnitude here.
    `CorpusIndex.labels_matching` carries that arithmetic.
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
    # Read one row past the cap to *observe* truncation instead of inferring it from
    # `len(records) == cap`, which cannot tell "there were more" from "that was all of them" and
    # so turned a corpus sitting exactly on the cap into `SEARCH INCOMPLETE` over a clean
    # negative. One extra row is the whole cost; the surplus is dropped before matching.
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
        # Both halves of one bound: the deadline stops the worker, `wait_for` still releases the
        # caller — the deadline is only checked between records, so one pathological molecule can
        # outlast it and the caller must not wait for that. Both raise `TimeoutError`, so the
        # refusal below is the same either way.
        scan = await asyncio.wait_for(
            asyncio.to_thread(_scan_for_matches, records, pattern, time.monotonic() + timeout),
            timeout=timeout,
        )
    except TimeoutError as exc:
        # The scan says how far it got and which of the two scans it was; `wait_for`'s own
        # TimeoutError carries no message at all, which is exactly the case where the thread is
        # still inside one uninterruptible chunk and nobody can say. Naming the real one matters
        # because the remedies differ: this message used to say "the match exceeded 5.0s, narrow
        # the pattern" over a corpus whose *indexing* had run out of time with the pattern never
        # matched once, so the one thing it told a chemist to do could not have helped.
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
        # A row whose stored structure no longer parses was fetched and never matched, which is
        # what `scan_truncated` means — "not every stored record was examined". Folded in here
        # rather than given a flag of its own: the model's move is identical either way (do not
        # report a miss as a negative), and a second boolean would split one instruction in two.
        scan_truncated=scan_truncated or bool(scan.unreadable),
        hits_truncated=scan.hits_truncated,
    )


class ScanOutcome(NamedTuple):
    """What one substructure pass found, and the two ways it fell short of the whole corpus.

    A tuple rather than three positional returns because the two caveats are read together and
    each answers a different question: `hits_truncated` says the count is a floor,
    `unreadable` says the *corpus* was not fully examined and so a miss is not a negative.
    """

    hits: list[MoleculeHit]
    hits_truncated: bool
    unreadable: int


def _scan_for_matches(
    records: list[FingerprintRecord], pattern: Chem.Mol, deadline: float
) -> ScanOutcome:
    """Match `pattern` against the corpus slice, stopping at the result cap or at `deadline`.

    Split out as a plain synchronous function so it can run in a worker thread: it is the only
    part of the search that burns CPU, and keeping it separate makes the async wrapper's one
    responsibility — bounding it — obvious. That is also why the index below is built here rather
    than in the coroutine: a build is the most expensive thing on this path, and it belongs off the
    event loop with the matching it serves.

    A record whose stored SMILES no longer parses is skipped rather than aborting the scan (one bad
    row must not hide every real hit) — and **counted**, because skipping it silently meant a corpus
    whose one azide row carried a malformed label still answered "this is a genuine negative
    result". The parse now happens once, when the index is built (`CorpusIndex`), for the reason
    that docstring gives: the holder skips sanitisation, so nothing downstream of it would ever
    notice a malformed row.

    Returns the matches **and whether a match was left out**, rather than letting the caller infer
    truncation from `len(matches) == cap`: a corpus holding exactly `cap` matches is complete, and
    reporting it as partial is the same class of untrue statement in the other direction. Which is
    why the scan asks for **one more hit than it may return** — `maxResults=cap + 1` — and treats
    the surplus as the observation. `maxResults=cap` would reproduce exactly the inference this
    refuses, since the search would stop at the cap-th match without ever asking whether another
    existed. One surplus match is also all it costs: the search stops at the first match it cannot
    return instead of scanning on to find every remaining one, which is what the per-record loop
    here had to do.

    **`deadline` is what makes the wall-clock bound true of the *thread* and not only of the
    caller** — see `find_substructure_matches` for the measurement, and
    `CorpusIndex.labels_matching` for why a chunk is a time slice rather than a record count. A scan
    that gives up raises rather than returning what it had: a partial scan reported as a result is
    the "no precedent exists" answer this module refuses everywhere else.

    Args:
        records: The capped corpus slice to match, in id order.
        pattern: The compiled query.
        deadline: `time.monotonic()` value past which the scan stops.

    Returns:
        The hits, whether a further match was found and dropped, and how many stored rows could not
        be parsed into the index at all.

    Raises:
        TimeoutError: The deadline passed before every record was examined. The caller turns it
            into the same `FingerprintError` `asyncio.wait_for` produces.
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

    **This is the floor the index has to beat, and therefore also the floor it falls back to.** An
    index is an optimisation: when one is not available — the corpus is too large to index inside
    `substructure_index_build_timeout_seconds`, another thread is still building this corpus's,
    the caller has no time to build one — the honest answer is the algorithm that needs no index,
    not a refusal. Measured on 19,996 NCI records, this answers the amide query in 2.01 s where
    charging the build to the match budget failed on three attempts out of three.

    It is what `find_substructure_matches` did before `rdSubstructLibrary` arrived, with one
    deliberate difference: it keeps *parsing* after the hit cap is reached, so `unreadable` counts
    the whole slice. That is what the indexed path reports, and two paths that disagreed about
    whether every stored record was examined would make `scan_truncated` a property of which one
    ran. Only the subgraph matching stops at the cap, which is the part that can run for minutes on
    an adversarial pattern. **It is not free and the number is here rather than the claim**: over
    4,999 NCI records at the shipped `fingerprint_max_top_k` of 100, a broad query that fills the
    cap in its first few hundred records costs 334 ms parsing the rest against 40 ms stopping
    there. That is the price of `scan_truncated` meaning the same thing on both paths, and it is
    paid only when there is no index.

    Args:
        records: The capped corpus slice to match, in id order.
        pattern: The compiled query.
        limit: How many matches to collect before matching stops — the result cap plus one.
        deadline: `time.monotonic()` value past which the scan stops.

    Returns:
        The matching labels in stored order, at most `limit` of them, and how many stored rows
        could not be parsed at all.

    Raises:
        TimeoutError: The deadline passed before every record was examined.
    """
    found: list[str] = []
    unreadable = 0
    for examined, record in enumerate(records):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"substructure scan gave up after {examined} of {len(records)} molecule(s), "
                "matching them one at a time because no index was available"
            )
        molecule = Chem.MolFromSmiles(record.label)
        if molecule is None:
            unreadable += 1
            continue
        if len(found) < limit and molecule.HasSubstructMatch(pattern):
            found.append(record.label)
    return found, unreadable
