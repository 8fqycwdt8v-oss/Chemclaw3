"""Behavioral tests for the mcp-molfp capability, without a database.

ECFP4 is deterministic and config-sized; Tanimoto ranking returns most-similar-first neighbours
honouring threshold and top_k; substructure search filters by exact fragment containment; and the
result says when it is partial, approximate or from an empty index. The Postgres backend is
tested against the same contract in `test_molfp_postgres.py`.
"""

import asyncio
import math
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import psycopg
import pytest
from rdkit import Chem, RDConfig

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.chem import STANDARDIZATION_VERSION, substructure_pattern
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp import search, substructure_index
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.molfp.search import (
    ScanOutcome,
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)
from chemclaw.science.fingerprints.molfp.substructure_index import ScanDeadlineExceeded
from chemclaw.science.fingerprints.store import (
    FingerprintError,
    FingerprintRecord,
    FingerprintSearch,
    InMemoryFingerprintStore,
    Match,
    PostgresFingerprintStore,
    find_matches,
    log_index_size,
    tanimoto,
)


def test_ecfp_is_deterministic_and_config_sized() -> None:
    """The same SMILES yields the same fingerprint, sized to the configured width."""
    a = ecfp_bitstring("CCO")
    assert a == ecfp_bitstring("CCO")
    assert len(a) == settings.ecfp_bits
    assert set(a) <= {"0", "1"}


def test_unparseable_smiles_raises() -> None:
    """A bad SMILES is a clear FingerprintError, not a crash (G4)."""
    with pytest.raises(FingerprintError, match="unparseable SMILES"):
        ecfp_bitstring("not-a-molecule(((")


def test_empty_smiles_raises() -> None:
    """An empty/whitespace SMILES is rejected, not fingerprinted to all zeros (G4).

    RDKit parses "" to a zero-atom Mol; without the guard the all-zero fingerprint
    silently searches as "no similar molecules known" instead of an input error.
    """
    for smiles in ["", "   "]:
        with pytest.raises(FingerprintError):
            ecfp_bitstring(smiles)


def test_tanimoto_bounds() -> None:
    """Identical fingerprints score 1.0; structurally disjoint ones score 0.0."""
    ethanol = ecfp_bitstring("CCO")
    assert tanimoto(ethanol, ethanol) == 1.0
    assert tanimoto(ecfp_bitstring("CCO"), ecfp_bitstring("c1ccccc1")) == 0.0
    assert tanimoto("0" * 8, "0" * 8) == 0.0  # two empty fps: defined as 0


async def test_find_similar_ranks_by_tanimoto() -> None:
    """A query returns neighbors most-similar-first, filtered by threshold and top_k."""
    store = InMemoryFingerprintStore()
    for cid, smiles in [
        ("ethanol", "CCO"),
        ("propanol", "CCCO"),
        ("butanol", "CCCCO"),
        ("benzene", "c1ccccc1"),
    ]:
        await store.add(record_for(cid, smiles))

    hits = (await find_similar_molecules(store, "CCO", threshold=0.1)).hits
    found = [h.smiles for h in hits]
    assert found[0] == "CCO"  # exact match ranks first
    assert "c1ccccc1" not in found  # disjoint, below threshold
    # Similarity is monotonically non-increasing down the list.
    assert all(
        (hits[i].similarity or 0.0) >= (hits[i + 1].similarity or 0.0) for i in range(len(hits) - 1)
    )

    # top_k truncates to the closest neighbors only.
    assert len((await find_similar_molecules(store, "CCO", top_k=2, threshold=0.1)).hits) == 2


async def test_threshold_excludes_weak_matches() -> None:
    """Raising the threshold drops loosely related hits."""
    store = InMemoryFingerprintStore()
    await store.add(record_for("propanol", "CCCO"))
    # Ethanol vs propanol ~0.56; a 0.9 threshold rejects it.
    assert (await find_similar_molecules(store, "CCO", threshold=0.9)).hits == []
    assert len((await find_similar_molecules(store, "CCO", threshold=0.5)).hits) == 1


async def test_similarity_excludes_other_fingerprint_definitions() -> None:
    """A store bound to a definition ranks only records built under that definition.

    A changed Morgan radius yields equal-width but incomparable bits, so such records must not be
    returned even if their bits look similar.
    """
    store = InMemoryFingerprintStore(definition=molecule_definition())
    await store.add(record_for("current", "CCO"))  # stamped with the current definition
    # Same molecule, same width, but a different (stale) definition signature.
    stale = FingerprintRecord(
        id="stale", label="CCO", bits=ecfp_bitstring("CCO"), definition="ecfp:r9:b2048"
    )
    await store.add(stale)

    hits = (await find_similar_molecules(store, "CCO", threshold=0.1)).hits
    # Both rows carry the same structure, so the exclusion shows in the count: the
    # stale-definition row is filtered out by the store, not ranked below the current one.
    assert len(hits) == 1
    assert hits[0].smiles == "CCO"


async def test_substructure_matches_fragment() -> None:
    """Substructure search returns exactly the molecules containing the query fragment."""
    store = InMemoryFingerprintStore()
    for cid, smiles in [
        ("aspirin", "CC(=O)Oc1ccccc1C(=O)O"),
        ("benzene", "c1ccccc1"),
        ("ethanol", "CCO"),
        ("acetic_acid", "CC(=O)O"),
    ]:
        await store.add(record_for(cid, smiles))

    ring = {r.smiles for r in (await find_substructure_matches(store, "c1ccccc1")).hits}
    assert ring == {"CC(=O)Oc1ccccc1C(=O)O", "c1ccccc1"}  # only the aromatic molecules

    acids = {r.smiles for r in (await find_substructure_matches(store, "C(=O)[OH]")).hits}
    assert acids == {"CC(=O)Oc1ccccc1C(=O)O", "CC(=O)O"}  # carboxylic-acid SMARTS


async def test_substructure_bad_query_raises() -> None:
    """An unparseable substructure query is a clear error (G4)."""
    with pytest.raises(FingerprintError, match="substructure query"):
        await find_substructure_matches(InMemoryFingerprintStore(), "%%%")


async def test_substructure_empty_query_raises() -> None:
    """An empty query is an input error, not a silent empty result (G4).

    `MolFromSmarts("")` parses to a zero-atom pattern that matches nothing, so without
    the guard the tool reads as "no stored molecule contains the fragment".
    """
    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))
    with pytest.raises(FingerprintError, match="empty substructure query"):
        await find_substructure_matches(store, "")


async def test_substructure_oversized_query_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model-supplied query beyond the configured length bound is rejected (SEC-4).

    SMARTS matching is subgraph isomorphism run in-process over the scanned corpus, so a
    pathological multi-KB pattern must be refused up front, not matched for minutes.
    """
    monkeypatch.setattr(settings, "substructure_query_max_length", 16)

    with pytest.raises(FingerprintError, match="exceeds 16 characters"):
        await find_substructure_matches(InMemoryFingerprintStore(), "C" * 17)


async def test_substructure_hits_are_lean_and_capped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Substructure hits carry only id and label, and a broad query is capped with a warning.

    The tool ships hits into the model context, so the shape stays lean (no fingerprint bits) and
    the count is bounded by `fingerprint_max_top_k`.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 2)

    store = InMemoryFingerprintStore()
    for cid, smiles in [("ethanol", "CCO"), ("propanol", "CCCO"), ("butanol", "CCCCO")]:
        await store.add(record_for(cid, smiles))
    with caplog.at_level("WARNING"):
        hits = (await find_substructure_matches(store, "CO")).hits
    assert len(hits) == 2  # three molecules match; the cap truncates to two
    assert any("substructure result capped" in r.message for r in caplog.records)
    assert not any(hasattr(h, "bits") for h in hits)  # lean shape: no fingerprint payload


async def test_a_truncated_scan_does_not_render_as_a_genuine_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan the record cap cut short does not answer "we have no precedent for this".

    With the only match beyond the cap, the payload must flag the truncation to the model rather
    than reporting a genuine negative.
    """
    monkeypatch.setattr(settings, "substructure_scan_max_records", 20)

    store = InMemoryFingerprintStore()
    for i in range(20):
        await store.add(record_for(f"{100 + i}", "CCO"))
    await store.add(record_for("900", "CC(=O)N=[N+]=[N-]"))  # last by id, never reached
    result = await find_substructure_matches(store, "[N-]=[N+]=N")
    assert result.hits == [] and result.index_empty is False
    assert result.scan_truncated is True
    payload = result.model_dump()
    assert payload["scan_truncated"] is True
    assert "genuine negative" not in payload["verdict"]
    assert "SEARCH INCOMPLETE" in payload["verdict"]


async def test_a_capped_hit_list_says_the_count_is_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit count is not the total when the scan stopped at the result cap.

    The mirror of the truncated-scan case: `fingerprint_max_top_k` stops the scan at the
    cap-th match, so the count is a lower bound, and the verdict has to say so in the payload.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 3)

    store = InMemoryFingerprintStore()
    for i in range(6):
        await store.add(record_for(f"{100 + i}", "CCO"))
    result = await find_substructure_matches(store, "CCO")
    assert len(result.hits) == 3
    assert result.hits_truncated is True and result.scan_truncated is False
    assert "PARTIAL RESULT" in result.model_dump()["verdict"]


async def test_a_similarity_hit_list_cut_at_top_k_says_so_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A similarity hit list cut at top_k says so too.

    More qualifying molecules than `fingerprint_top_k` must set `hits_truncated`, or the count reads
    as a total. A page holding everything qualifying is not partial, pinned below.
    """
    monkeypatch.setattr(settings, "fingerprint_top_k", 10)

    store = InMemoryFingerprintStore()
    for i in range(18):
        await store.add(record_for(f"m{i:02d}", "CCO"))  # all identical, so all qualify
    cut = await find_similar_molecules(store, "CCO")
    assert len(cut.hits) == 10 and cut.hits_truncated is True
    assert "PARTIAL RESULT" in cut.model_dump()["verdict"]

    # Exactly the page size, nothing beyond it: a complete answer.
    exact = InMemoryFingerprintStore()
    for i in range(10):
        await exact.add(record_for(f"m{i:02d}", "CCO"))
    whole = await find_similar_molecules(exact, "CCO")
    assert len(whole.hits) == 10 and whole.hits_truncated is False
    assert whole.verdict == "10 indexed molecule(s) matched this query."


async def test_a_corpus_holding_exactly_the_result_cap_is_not_reported_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly `fingerprint_max_top_k` matches is a complete answer, not a lower bound.

    `hits_truncated` must not equal `len(hits) == cap`. Both spellings are pinned: exactly cap
    matches, and cap matches followed by non-matching records, which were examined.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 3)

    exact = InMemoryFingerprintStore()
    for i in range(3):
        await exact.add(record_for(f"{100 + i}", "CCO"))
    result = await find_substructure_matches(exact, "CCO")
    assert len(result.hits) == 3 and result.hits_truncated is False
    assert result.verdict == "3 indexed molecule(s) matched this query."

    with_tail = InMemoryFingerprintStore()
    for i in range(3):
        await with_tail.add(record_for(f"{100 + i}", "CCO"))
    await with_tail.add(record_for("900", "c1ccccc1"))  # scanned, does not match
    tailed = await find_substructure_matches(with_tail, "CCO")
    assert len(tailed.hits) == 3 and tailed.hits_truncated is False
    assert "PARTIAL RESULT" not in tailed.verdict

    # One more match than the cap is the case the flag is *for*.
    over = InMemoryFingerprintStore()
    for i in range(4):
        await over.add(record_for(f"{100 + i}", "CCO"))
    assert (await find_substructure_matches(over, "CCO")).hits_truncated is True


async def test_a_corpus_holding_exactly_the_scan_cap_is_a_complete_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corpus of exactly `substructure_scan_max_records` is a complete scan.

    The flag must distinguish "read cap records and there were more" from "that was all of them", or
    a clean negative is reported as inconclusive.
    """
    monkeypatch.setattr(settings, "substructure_scan_max_records", 5)

    exact = InMemoryFingerprintStore()
    for i in range(5):
        await exact.add(record_for(f"{100 + i}", "CCO"))
    result = await find_substructure_matches(exact, "[N-]=[N+]=N")
    assert result.hits == [] and result.scan_truncated is False
    assert "genuine negative result" in result.verdict

    over = InMemoryFingerprintStore()
    for i in range(6):
        await over.add(record_for(f"{100 + i}", "CCO"))
    truncated = await find_substructure_matches(over, "[N-]=[N+]=N")
    assert truncated.scan_truncated is True
    assert "SEARCH INCOMPLETE" in truncated.verdict


async def test_a_row_that_no_longer_parses_makes_the_scan_incomplete() -> None:
    """A row that no longer parses makes the scan incomplete.

    An unparseable SMILES is skipped so one bad row cannot hide every hit, but it was not examined,
    so `scan_truncated` must be set rather than reporting a genuine negative.
    """
    store = InMemoryFingerprintStore()
    await store.add(record_for("ok", "CCO"))
    # Bypass `record_for`, which would refuse to fingerprint it — this is a row that parsed
    # when it was indexed and does not now (a lenient canonicalization, a changed RDKit).
    await store.add(FingerprintRecord(id="broken", label="not-a-molecule", bits="01"))
    result = await find_substructure_matches(store, "[N-]=[N+]=N")
    assert result.hits == [] and result.scan_truncated is True
    assert "genuine negative" not in result.verdict
    assert "SEARCH INCOMPLETE" in result.verdict


async def test_a_complete_substructure_scan_reports_no_truncation() -> None:
    """The common case stays unchanged: both flags false, and the verdict keeps its wording.

    The counterfactual for the two tests above — without it they would also pass on a build
    that flagged every search as partial.
    """
    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))
    hit = await find_substructure_matches(store, "CCO")
    assert hit.scan_truncated is False and hit.hits_truncated is False
    assert hit.verdict == "1 indexed molecule(s) matched this query."
    miss = await find_substructure_matches(store, "[N-]=[N+]=N")
    assert miss.hits == [] and "genuine negative" in miss.verdict


def _sleeping_scan(seconds: float) -> Callable[..., ScanOutcome]:
    """A stand-in for the CPU-bound scan that blocks its thread for `seconds`, then matches nothing.

    A real pathological SMARTS would take minutes and is not reproducible across RDKit versions;
    what both tests need is only that the scan blocks a *thread*, which this reproduces exactly.
    """

    def _scan(*_args: object, **_kwargs: object) -> ScanOutcome:
        time.sleep(seconds)
        return ScanOutcome([], False, 0)

    return _scan


async def test_slow_substructure_match_times_out_with_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathological match is abandoned at the wall-clock bound, not run to completion.

    Query length and scan size bound the *inputs*; a short adversarial recursive SMARTS can
    still match for minutes, so the caller must be released with an actionable error.
    """
    monkeypatch.setattr(settings, "substructure_match_timeout_seconds", 0.05)
    # The stand-in sleeps well past the bound but stays short: the timeout releases the caller,
    # it cannot kill the thread, and `asyncio.run` waits for the executor on shutdown.
    monkeypatch.setattr(search, "_scan_for_matches", _sleeping_scan(0.5))

    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))
    with pytest.raises(FingerprintError, match="exceeded 0.05s"):
        await find_substructure_matches(store, "CO")


async def test_substructure_match_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other sessions keep making progress while a slow match runs (the property that matters).

    The scan is served by the async front door: run in-loop, one adversarial pattern stalls
    *every* streamed turn, not just its own. Running it in a worker thread is what prevents that.
    """
    monkeypatch.setattr(settings, "substructure_match_timeout_seconds", 5.0)
    monkeypatch.setattr(search, "_scan_for_matches", _sleeping_scan(0.3))
    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))
    await asyncio.gather(find_substructure_matches(store, "CO"), _tick())

    assert ticks == 20  # the concurrent task ran to completion during the blocking match


# A hyperbranched C121 dendrimer and a 48-character SMARTS ending in an atom no organic record
# carries: the pattern matches almost to the end and never completes, making subgraph isomorphism
# expensive (RDKit doing real work). The tests measure one record's match first and derive their
# deadline from it, so machine or RDKit speed does not change the property.
def _hyperbranched(depth: int) -> str:
    """A tri-branched all-carbon dendrimer: 121 atoms at depth 4."""
    if depth == 0:
        return "C"
    return f"C({_hyperbranched(depth - 1)})({_hyperbranched(depth - 1)}){_hyperbranched(depth - 1)}"


def _binary_query(depth: int) -> str:
    """A symmetric binary-tree SMARTS: 15 atoms at depth 3, every bond `any`."""
    if depth == 0:
        return "C"
    return f"C(~{_binary_query(depth - 1)})~{_binary_query(depth - 1)}"


_DENDRIMER = _hyperbranched(4)
_UNMATCHABLE = _binary_query(3) + "~[Si]"


def _dendrimer_records(count: int) -> list[FingerprintRecord]:
    """`count` distinct records of the pathological molecule above."""
    return [record_for(f"dendrimer-{index}", _DENDRIMER) for index in range(count)]


def _one_match_seconds() -> float:
    """What one such match costs here, so a deadline can be expressed in records rather than ms."""
    molecule = Chem.MolFromSmiles(_DENDRIMER)
    pattern = substructure_pattern(_UNMATCHABLE)
    started = time.perf_counter()
    molecule.HasSubstructMatch(pattern)
    return time.perf_counter() - started


def test_a_scan_past_its_deadline_stops_instead_of_matching_the_rest_of_the_corpus() -> None:
    """A scan past its deadline stops instead of matching the rest of the corpus.

    `asyncio.wait_for` releases the caller but cannot stop the worker thread, which runs in the
    loop's default executor shared with bearer validation, so the deadline must reach the thread.
    Asserted as a record count: `ScanDeadlineExceeded.reached`. Chunks start at `_FIRST_CHUNK` and
    grow by at most `_CHUNK_GROWTH`, so the most records reachable before the deadline is
    `_FIRST_CHUNK + _CHUNK_GROWTH` on any machine, which is the bar. On an unmatchable pattern the
    unbounded arm can only return no hits by examining every record, so `hits == []` is its control.
    """
    pattern = substructure_pattern(_UNMATCHABLE)
    per_record = _one_match_seconds()
    records = _dendrimer_records(16)
    reachable = substructure_index._FIRST_CHUNK + substructure_index._CHUNK_GROWTH

    with pytest.raises(ScanDeadlineExceeded) as stopped:
        search._scan_for_matches(records, pattern, time.monotonic() + per_record * 2)

    outcome = search._scan_for_matches(records, pattern, time.monotonic() + 3600)

    assert outcome.hits == [], (
        "unmatchable, so the unbounded run can only have returned nothing by examining all 16 — "
        "which is what makes it the whole-corpus control"
    )
    assert stopped.value.reached <= reachable, (
        f"the scan reached {stopped.value.reached} of {len(records)} records past its deadline, "
        f"where the chunking allows at most {reachable} — one record, then at most "
        f"{substructure_index._CHUNK_GROWTH}x it. That is the bound failing to reach the worker "
        "thread: it releases the caller and "
        "cannot stop a thread, so what is left running is a full scan nobody is waiting for"
    )
    assert stopped.value.reached < len(records), (
        f"the scan reached every one of {len(records)} records while still raising, which is the "
        "deadline being checked after the work rather than before it"
    )


def test_a_timed_out_substructure_search_leaves_no_thread_matching_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out substructure search leaves no thread matching behind.

    `asyncio.run` waits for the default executor on shutdown, so an orphaned scan would show up as
    that wait, long after the caller's `FingerprintError`.
    """
    per_record = _one_match_seconds()
    monkeypatch.setattr(settings, "substructure_match_timeout_seconds", per_record * 2)

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        for record in _dendrimer_records(16):
            await store.add(record)
        with pytest.raises(FingerprintError, match="exceeded"):
            await find_substructure_matches(store, _UNMATCHABLE)

    started = time.perf_counter()
    asyncio.run(_run())
    elapsed = time.perf_counter() - started

    # One molecule's match is the residue RDKit's lack of an interruption hook leaves; sixteen of
    # them is the corpus the old scan ran out after the caller had already been refused.
    assert elapsed < per_record * 8, (
        f"the process waited {elapsed:.3f}s for a scan the caller abandoned "
        f"({elapsed / per_record:.1f} records' worth)"
    )


async def test_agent_supplied_top_k_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large model-supplied `top_k` is clamped to `fingerprint_max_top_k` (SEC-4).

    The similarity tools take `top_k` from the model and it lands in a SQL `LIMIT`; clamp it
    so an arbitrarily large value cannot become an unbounded query — mirrors `graph_max_hops`.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 2)

    store = InMemoryFingerprintStore()
    for cid, smiles in [
        ("ethanol", "CCO"),
        ("propanol", "CCCO"),
        ("butanol", "CCCCO"),
        ("pentanol", "CCCCCO"),
    ]:
        await store.add(record_for(cid, smiles))

    # Four records clear the threshold, but the clamp caps the returned neighbors at 2.
    hits = (await find_similar_molecules(store, "CCO", top_k=1_000_000, threshold=0.1)).hits
    assert len(hits) == 2


async def test_agent_supplied_threshold_is_clamped() -> None:
    """A model-supplied `threshold` is clamped to Tanimoto's [0, 1] range.

    A negative value would accept disjoint structures as neighbours, and >1 would report "no
    precedent" instead of an exact match.
    """

    class _RecordingStore:
        """Minimal FingerprintStore capturing what threshold reaches the backend."""

        def __init__(self) -> None:
            self.thresholds: list[float] = []

        @property
        def approximate(self) -> bool:
            """Never: this double scores nothing, so it can never miss a true neighbour.

            Stated explicitly so `find_matches` reads a declared answer, not an unset attribute.
            """
            return False

        async def add(self, record: FingerprintRecord) -> None:
            raise NotImplementedError

        async def add_many(self, records: Sequence[FingerprintRecord]) -> None:
            raise NotImplementedError

        async def all_records(self, limit: int | None = None) -> list[FingerprintRecord]:
            raise NotImplementedError

        async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
            self.thresholds.append(threshold)
            return []

        async def is_empty(self) -> bool:
            raise NotImplementedError

        async def count(self) -> int:
            raise NotImplementedError

        async def has_superseded_records(self) -> bool:
            raise NotImplementedError

        async def superseded_count(self) -> int:
            raise NotImplementedError

    recording = _RecordingStore()
    await find_matches(recording, "01", threshold=-5.0)
    await find_matches(recording, "01", threshold=1.5)
    assert recording.thresholds == [0.0, 1.0]

    # End to end: an over-1 threshold still returns the exact match instead of [].
    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))
    hits = (await find_similar_molecules(store, "CCO", threshold=99.0)).hits
    assert [h.smiles for h in hits] == ["CCO"]


async def test_agent_supplied_nan_threshold_is_refused_rather_than_emptying_the_search() -> None:
    """A NaN `threshold` is refused rather than silently emptying the search.

    `min(max(nan, 0.0), 1.0)` keeps NaN, and every comparison with it is False, so an exact match
    would come back as a "genuine negative". Pinned: the search refuses; the refusal is not a
    `FingerprintError` (which `retrieval.retrievers` treats as an empty answer); and an out-of-range
    threshold still answers.
    """
    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))

    with pytest.raises(ValueError, match="NaN") as excinfo:
        await find_similar_molecules(store, "CCO", threshold=float("nan"))
    # Not the domain family: catching that one is how a caller says "answer this empty".
    assert not isinstance(excinfo.value, FingerprintError)

    # ±inf has a nearest bound, so it still clamps rather than refusing...
    for unbounded in (float("inf"), float("-inf")):
        assert (await find_similar_molecules(store, "CCO", threshold=unbounded)).hits

    # ...and the ordinary out-of-range threshold keeps the behaviour merged before this.
    hits = (await find_similar_molecules(store, "CCO", threshold=99.0)).hits
    assert [h.smiles for h in hits] == ["CCO"]


async def test_all_records_limit_is_bounded_and_deterministic() -> None:
    """`all_records(limit=n)` returns the first n records in id order (bounded scan)."""
    store = InMemoryFingerprintStore()
    for cid in ["c", "a", "b"]:
        await store.add(record_for(cid, "CCO"))
    assert [r.id for r in await store.all_records(limit=2)] == ["a", "b"]
    assert len(await store.all_records()) == 3  # unbounded still returns all


async def test_substructure_scan_caps_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The substructure scan is bounded by config and warns (not silently) when it truncates."""
    monkeypatch.setattr(settings, "substructure_scan_max_records", 1)

    store = InMemoryFingerprintStore()
    for cid in ["aspirin", "benzene", "toluene"]:
        await store.add(record_for(cid, "c1ccccc1" if cid != "aspirin" else "Cc1ccccc1"))
    with caplog.at_level("WARNING"):
        hits = (await find_substructure_matches(store, "c1ccccc1")).hits
    # Only the one capped record is scanned, so at most one match is returned.
    assert len(hits) <= 1
    assert any("substructure scan hit" in r.message for r in caplog.records)


class _NullConnection:
    """A psycopg connection stand-in: enterable, and nothing is executed on it."""

    async def __aenter__(self) -> "_NullConnection":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def test_postgres_store_applies_the_configured_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Postgres store's queries are bounded by the configured statement timeout.

    Asserted on the libpq `options` the connect receives, since `db.connection` supplies the bound
    and the store passes no keyword. Verified offline by capturing the psycopg connect.
    """
    captured: dict[str, object] = {}

    async def _fake_connect(dsn: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return _NullConnection()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _fake_connect)
    store = PostgresFingerprintStore(
        "molecule_fingerprints", settings.ecfp_bits, molecule_definition()
    )

    async with store._connection():
        pass

    expected = int(settings.pg_statement_timeout_seconds * 1000)
    assert f"-c statement_timeout={expected}" in str(captured["options"])


# --- An empty index must not answer "nothing similar" --------------------------------------------
#
# An unpopulated fingerprint table answering `[]` is indistinguishable from a novel structure. These
# tests pin the distinction and that it survives serialization.


async def test_an_empty_index_reports_that_the_search_was_not_run() -> None:
    """No records: the result says the question was not answered, not that the answer is no."""
    search_result = await find_similar_molecules(InMemoryFingerprintStore(), "CCO")
    assert search_result.hits == []
    assert search_result.index_empty is True
    assert "SEARCH NOT RUN" in search_result.verdict
    assert "NOT evidence" in search_result.verdict


async def test_the_empty_index_signal_survives_model_dump() -> None:
    """The empty-index verdict survives `model_dump`, so the model writing the answer sees it.

    MCP ships `model_dump()`, which drops a plain property; `verdict` is a `computed_field` for that
    reason.
    """
    payload = (await find_similar_molecules(InMemoryFingerprintStore(), "CCO")).model_dump()
    assert payload["index_empty"] is True
    assert "SEARCH NOT RUN" in payload["verdict"]


async def test_a_populated_index_with_no_match_is_a_genuine_negative() -> None:
    """Records exist and none matched: the ordinary "no precedent" answer, clearly distinguished."""
    store = InMemoryFingerprintStore()
    await store.add(record_for("benzene", "c1ccccc1"))
    # Ethanol vs benzene share no bits, so the search is real and finds nothing.
    search_result = await find_similar_molecules(store, "CCO", threshold=0.5)
    assert search_result.hits == []
    assert search_result.index_empty is False
    assert "SEARCH NOT RUN" not in search_result.verdict
    assert "genuine negative" in search_result.verdict


def test_an_approximate_search_never_reports_a_genuine_negative() -> None:
    """An approximate search never reports a genuine negative.

    It compared against a candidate set the index proposed, not the corpus, so "no indexed molecule
    matched" answers a weaker question. Driven on the model, since the assertion is about the
    sentence the model reads: the two verdicts differ, and the approximate one lacks the exact one's
    wording.
    """
    exact = FingerprintSearch[Match](subject="molecule", hits=[])
    approximate = FingerprintSearch[Match](subject="molecule", hits=[], approximate=True)

    assert "genuine negative" in exact.verdict
    assert "genuine negative" not in approximate.verdict
    assert "NOT proof" in approximate.verdict
    # And it survives serialization, which is the whole reason `verdict` is a computed field.
    assert approximate.model_dump()["approximate"] is True
    assert "APPROXIMATE" in approximate.model_dump()["verdict"]


def test_an_approximate_page_is_not_presented_as_the_definitive_set() -> None:
    """A full approximate page is not presented as the definitive set.

    A closer precedent may sit outside the proposed candidates. Distinct from `hits_truncated`
    ("more matched than fit"): this says the hits may not be the best.
    """
    hit = Match(id="m1", label="CCO", similarity=0.9)
    exact = FingerprintSearch[Match](subject="molecule", hits=[hit])
    approximate = FingerprintSearch[Match](subject="molecule", hits=[hit], approximate=True)

    assert exact.verdict == "1 indexed molecule(s) matched this query."
    assert approximate.verdict.startswith("APPROXIMATE RESULT:")
    assert "may exist" in approximate.verdict


async def test_the_in_memory_backend_is_exact_and_says_so() -> None:
    """The in-memory backend is exact and says so.

    It scores every searchable record, which is what makes it the reference for the durable exact
    arm; the exactness setting selects between SQL statements this backend does not have.
    """
    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))
    assert store.approximate is False
    settings.fingerprint_search_exactness = "approximate"
    try:
        assert store.approximate is False
        result = await find_similar_molecules(store, "CCO", threshold=0.1)
    finally:
        settings.fingerprint_search_exactness = "exact"
    assert result.approximate is False
    assert "APPROXIMATE" not in result.verdict


def test_the_durable_store_reports_the_configured_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PostgresFingerprintStore.approximate` reads the setting per call; no database needed.

    The store is built once per process, so a value frozen at construction could disagree with the
    arm `find_similar` actually ran.
    """
    store = PostgresFingerprintStore("molecule_fingerprints", settings.ecfp_bits, "ecfp:r2:b2048")
    assert store.approximate is False
    monkeypatch.setattr(settings, "fingerprint_search_exactness", "approximate")
    assert store.approximate is True


async def test_a_hit_is_unaffected_by_the_emptiness_signal() -> None:
    """Regression guard: a real match still reports its hits, with the index not flagged empty."""
    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))
    search_result = await find_similar_molecules(store, "CCO", threshold=0.1)
    assert [h.smiles for h in search_result.hits] == ["CCO"]
    assert search_result.index_empty is False
    assert search_result.verdict.startswith("1 indexed molecule(s) matched")


async def test_an_index_of_only_stale_definitions_counts_as_empty() -> None:
    """An index holding only stale definitions counts as empty.

    Search returns none of those rows, so reporting the index as populated would turn an unanswered
    question into a negative.
    """
    store = InMemoryFingerprintStore(definition=molecule_definition())
    await store.add(
        FingerprintRecord(
            id="stale", label="CCO", bits=ecfp_bitstring("CCO"), definition="ecfp:r9:b2048"
        )
    )
    search_result = await find_similar_molecules(store, "CCO", threshold=0.1)
    assert search_result.index_empty is True
    assert await store.count() == 0


async def test_substructure_search_makes_the_same_distinction() -> None:
    """The third tool over the same index has the same failure mode, so it gets the same answer."""
    empty = await find_substructure_matches(InMemoryFingerprintStore(), "c1ccccc1")
    assert empty.hits == [] and empty.index_empty is True
    assert "SEARCH NOT RUN" in empty.model_dump()["verdict"]

    store = InMemoryFingerprintStore()
    await store.add(record_for("ethanol", "CCO"))
    populated = await find_substructure_matches(store, "c1ccccc1")
    assert populated.hits == [] and populated.index_empty is False
    assert "genuine negative" in populated.verdict


async def test_the_emptiness_probe_is_skipped_when_the_search_found_hits() -> None:
    """The probe may not become a per-call cost: a search with hits already proved the index full.

    `is_empty` runs on the durable backend as a real query; paying for it when the answer is
    already known would be a performance defect on the hot path.
    """

    class _CountingStore(InMemoryFingerprintStore):
        """An in-memory store that records how often it was asked whether it is empty."""

        probes = 0

        async def is_empty(self) -> bool:
            type(self).probes += 1
            return await super().is_empty()

    store = _CountingStore()
    await store.add(record_for("ethanol", "CCO"))
    assert (await find_similar_molecules(store, "CCO", threshold=0.1)).hits  # a hit
    assert _CountingStore.probes == 0
    # Benzene shares no bits with the indexed ethanol, so this search legitimately finds none.
    assert (await find_similar_molecules(store, "c1ccccc1", threshold=0.1)).hits == []
    assert _CountingStore.probes == 1  # asked only once the result was empty


async def test_the_startup_report_warns_only_when_the_index_is_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator half: the owning connector says at startup what its index holds.

    WARNING for an empty index (actionable and wrong), INFO with the count otherwise — so a
    half-finished backfill is visible as a number rather than hidden behind a boolean.
    """
    store = InMemoryFingerprintStore()
    with caplog.at_level("INFO"):
        await log_index_size(store, "molecule")
        empty_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("index is EMPTY" in r.getMessage() for r in empty_records)

        caplog.clear()
        await store.add(record_for("ethanol", "CCO"))
        await log_index_size(store, "molecule")
        assert any("1 record(s) indexed" in r.getMessage() for r in caplog.records)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_the_startup_report_never_takes_the_connector_down() -> None:
    """A report that cannot read the database logs and returns — a diagnostic may not be fatal."""

    class _BrokenStore(InMemoryFingerprintStore):
        """A store whose count fails the way an unreachable Postgres does."""

        async def count(self) -> int:
            raise ConnectionError("Postgres unreachable at postgres://host/db")

    asyncio.run(log_index_size(_BrokenStore(), "molecule"))  # must not raise


# --- a partly re-indexed corpus ----------------------------------------------------------
# D-2026-09-09-a-rebuild-nothing-counts-reports-as-finished------


def _superseded(record: FingerprintRecord) -> FingerprintRecord:
    """The same record as the previous fingerprint definition stored it.

    The version is substituted by name rather than by literal, so the fixture stays an older
    standardization when the constant moves.
    """
    return record.model_copy(
        update={"definition": record.definition.replace(STANDARDIZATION_VERSION, "std-superseded")}
    )


async def test_one_rebuilt_row_does_not_make_a_stale_corpus_answerable() -> None:
    """One rebuilt row does not make a stale corpus answerable.

    After a definition bump, one re-indexed row flips `index_empty` to False; without counting
    superseded rows the search would answer confidently from a small fraction of the corpus. The
    result must report the index as partial.
    """
    store = InMemoryFingerprintStore(molecule_definition())
    for i, smiles in enumerate(["CCO", "CCCO", "CCCCO", "c1ccccc1", "CC(=O)O"]):
        await store.add(_superseded(record_for(f"old-{i}", smiles)))
    await store.add(record_for("rebuilt", "CCO"))

    result = await find_similar_molecules(store, "CCO")
    assert [hit.smiles for hit in result.hits] == ["CCO"]
    assert result.index_empty is False, "one rebuilt row is not an empty index"
    assert result.index_partial is True
    assert result.verdict != "1 indexed molecule(s) matched this query."
    assert "SUPERSEDED" in result.model_dump()["verdict"]


async def test_the_instant_of_a_definition_bump_is_still_reported_as_a_search_not_run() -> None:
    """At the instant of a definition bump, the search is still reported as not run.

    With nothing searchable, "SEARCH NOT RUN" is the right sentence; a partial-index notice must not
    displace it.
    """
    store = InMemoryFingerprintStore(molecule_definition())
    for i, smiles in enumerate(["CCO", "CCCO", "CCCCO"]):
        await store.add(_superseded(record_for(f"only-old-{i}", smiles)))

    result = await find_similar_molecules(store, "CCO")
    assert result.hits == []
    assert result.index_empty is True and result.index_partial is True
    assert "SEARCH NOT RUN" in result.verdict


async def test_a_fully_rebuilt_index_is_not_flagged_partial() -> None:
    """The counterfactual: without it every assertion above passes on a build that always flags."""
    store = InMemoryFingerprintStore(molecule_definition())
    for i, smiles in enumerate(["CCO", "CCCO", "c1ccccc1"]):
        await store.add(record_for(f"current-{i}", smiles))

    result = await find_similar_molecules(store, "CCO", threshold=0.9)
    assert result.index_partial is False
    assert result.verdict == "1 indexed molecule(s) matched this query."
    assert await store.superseded_count() == 0


async def test_no_hits_over_a_partly_rebuilt_index_is_not_a_genuine_negative() -> None:
    """No hits over a partly rebuilt index is not a genuine negative.

    "Every stored record was compared" is false while much of the corpus awaits re-indexing.
    """
    store = InMemoryFingerprintStore(molecule_definition())
    await store.add(_superseded(record_for("old-azide", "CCCCN=[N+]=[N-]")))
    await store.add(record_for("rebuilt-benzene", "c1ccccc1"))

    result = await find_similar_molecules(store, "CCCCN=[N+]=[N-]", threshold=0.5)
    assert result.hits == [] and result.index_empty is False
    assert "genuine negative" not in result.verdict
    assert "SEARCH INCOMPLETE" in result.verdict
    assert "SUPERSEDED" in result.verdict


async def test_a_substructure_scan_is_not_partial_because_it_reads_every_definition() -> None:
    """A substructure scan reads every definition, so it is never partial and does not say it is.

    `all_records` is deliberately unfiltered by definition, and a stale row's SMILES is still a
    correct substructure hit.
    """
    store = InMemoryFingerprintStore(molecule_definition())
    await store.add(_superseded(record_for("old-ethanol", "CCO")))

    result = await find_substructure_matches(store, "CCO")
    assert [hit.smiles for hit in result.hits] == ["CCO"]
    assert result.index_partial is False
    assert result.verdict == "1 indexed molecule(s) matched this query."


def test_the_two_notices_compose_instead_of_shadowing_each_other() -> None:
    """A partial index and an approximate arm are independent facts, so both notices are said.

    The no-hits arm must compose clauses rather than branch, like the hits arm.
    """
    both = FingerprintSearch[Match](
        subject="molecule", hits=[], index_partial=True, approximate=True
    )
    assert "SUPERSEDED" in both.verdict and "APPROXIMATELY" in both.verdict
    assert "NOT proof" in both.verdict

    page = FingerprintSearch[Match](
        subject="reaction",
        hits=[Match(id="r1", label="CCO>>CC=O", similarity=0.9)],
        index_partial=True,
        approximate=True,
    )
    assert page.verdict.startswith("PARTIAL AND APPROXIMATE RESULT:")
    assert "SUPERSEDED" in page.verdict and "may exist" in page.verdict


async def test_the_operator_log_names_the_rows_waiting_to_be_rebuilt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator log names the rows waiting to be rebuilt.

    The search surface carries only a boolean; the log carries both counts, because the operator's
    action differs: rebuild, finish rebuilding, dispose of the shelved superseded generation, or
    nothing. A finished rebuild still reports PARTIAL because the superseded generation is shelved,
    so no share is derived from the counts.
    """
    store = InMemoryFingerprintStore(molecule_definition())
    for i, smiles in enumerate(["CCO", "CCCO", "c1ccccc1"]):
        await store.add(_superseded(record_for(f"log-old-{i}", smiles)))

    with caplog.at_level("INFO"):
        await log_index_size(store, "molecule")
        assert "is EMPTY" in caplog.text
        assert "3 record(s) are stored under a superseded definition" in caplog.text

        caplog.clear()
        await store.add(record_for("log-new", "CCS"))
        await log_index_size(store, "molecule")
        assert "is PARTIAL" in caplog.text
        assert "1 record(s) indexed under the current definition and 3 under a" in caplog.text
        assert caplog.records[-1].levelname == "WARNING"

        # The rebuild finishes, but the superseded generation is shelved, so the index still says
        # PARTIAL. The log is where the difference shows: the searchable count is the whole corpus,
        # and what remains is a disposal under the owning principal rather than a rebuild.
        caplog.clear()
        for i, smiles in enumerate(["CCO", "CCCO", "c1ccccc1"]):
            await store.add(record_for(f"log-old-{i}", smiles))
        await log_index_size(store, "molecule")
        assert "is PARTIAL" in caplog.text and "is EMPTY" not in caplog.text
        assert "4 record(s) indexed under the current definition and 3 under a" in caplog.text
        assert "dispos" in caplog.text and "owns the schema" in caplog.text
        assert caplog.records[-1].levelname == "WARNING"

        # And the counterfactual the whole warning rests on: an index that never held a second
        # generation says nothing at all.
        caplog.clear()
        fresh = InMemoryFingerprintStore(molecule_definition())
        await fresh.add(record_for("log-fresh", "CCS"))
        await log_index_size(fresh, "molecule")
        assert "is PARTIAL" not in caplog.text and "is EMPTY" not in caplog.text
        assert caplog.records[-1].levelname == "INFO"


async def test_the_reference_shelves_a_superseded_generation_rather_than_evicting_it() -> None:
    """The in-memory reference shelves a superseded generation rather than evicting it.

    It is what the SQL is asserted against, so it must key by definition too; keyed by `(source,
    id)` alone, two pods with different `ecfp_radius` would evict each other's rows and never
    converge. A store pinned to one definition still ranks only its own generation.
    """
    old_definition = molecule_definition() + "-previous"
    old = InMemoryFingerprintStore(old_definition)
    new = InMemoryFingerprintStore(molecule_definition())
    was = record_for("shelved", "CCO").model_copy(update={"definition": old_definition})
    now = record_for("shelved", "c1ccccc1")
    for store in (old, new):
        await store.add(was)
        await store.add(now)

    assert await old.count() == 1 and await new.count() == 1
    assert [hit.label for hit in await old.find_similar(ecfp_bitstring("CCO"), 5, 0.99)] == [
        "CCO"
    ], "the newer definition's write evicted the generation it superseded"
    assert [hit.label for hit in await new.find_similar(ecfp_bitstring("c1ccccc1"), 5, 0.99)] == [
        "c1ccccc1"
    ]
    assert await new.find_similar(ecfp_bitstring("CCO"), 5, 0.99) == []
    # And one molecule is one row to the substructure scan, whichever generation it came from.
    assert [r.id for r in await new.all_records(limit=10)] == ["shelved"]
    assert [r.definition for r in await new.all_records(limit=10)] == [molecule_definition()]


# --------------------------------------------------------------------------------------------
# The `rdSubstructLibrary` index behind `_scan_for_matches`, and its cache:
# `chemclaw.science.fingerprints.molfp.substructure_index`.
# --------------------------------------------------------------------------------------------


def _nci_corpus(limit: int) -> list[str]:
    """`limit` SMILES from the NCI sample RDKit ships, as a realistic drug-like corpus.

    A pattern-fingerprint screen is only exercised over molecules diverse enough for it to reject
    some. It is RDKit's package data (`rdkit/Data/NCI/first_5K.smi`), read from the installed
    dependency, not a corpus under `data/`.
    """
    path = Path(RDConfig.RDDataDir) / "NCI" / "first_5K.smi"
    if not path.exists():  # pragma: no cover - only on an RDKit build that drops its sample data
        pytest.skip(f"RDKit sample corpus is not installed at {path}")
    return [line.split()[0] for line in path.read_text().splitlines()[:limit]]


def _loop_matches(labels: list[str], pattern: Chem.Mol) -> tuple[list[str], int]:
    """The per-record scan: parse every label and ask each molecule.

    Written out rather than imported, so the replaced algorithm and its replacement are compared.
    """
    matches: list[str] = []
    unreadable = 0
    for label in labels:
        molecule = Chem.MolFromSmiles(label)
        if molecule is None:
            unreadable += 1
            continue
        if molecule.HasSubstructMatch(pattern):
            matches.append(label)
    return matches, unreadable


# The four query classes the adoption was measured on, plus the shapes a pattern-fingerprint screen
# is most likely to get wrong if it is unsound: recursive SMARTS, ring/aromaticity primitives,
# any-atom/any-bond wildcards and an element nothing in the corpus carries.
_DIFFERENTIAL_QUERIES = [
    "C(=O)N",  # amide — the broad, many-hit case
    "c1ccccc1C(=O)N",  # aryl amide — narrow and specific
    "[Se]",  # an element the screen should reject on almost every molecule
    "C(~*)(~*)(~*)~*",  # adversarial: wildcards give the screen nothing to work with
    "c1ccccc1",
    "[CX3](=O)[OX2H1]",
    "[NX3;H2,H1;!$(NC=O)]",  # recursive SMARTS
    "[$([NX3](=O)=O),$([NX3+](=O)[O-])]",  # recursive SMARTS, two alternatives
    "[R2]",
    "[nH]",
    "[F,Cl,Br,I]",
    "*~*~*~*~*~*~*~*",
]


def test_the_index_returns_exactly_what_the_per_record_loop_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The index returns exactly the hits, in the same order, as the per-record loop.

    `rdSubstructLibrary` screens with a pattern fingerprint first; a screen unsound for some SMARTS
    class would silently drop real hits. Asserted over twelve query classes, as a list since order
    is what the model cites. A malformed row keeps the `unreadable` count honest.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 100_000)  # compare whole hit lists
    labels = [*_nci_corpus(1200), "not-a-molecule((("]
    records = [
        FingerprintRecord(id=f"{index:05d}", label=label, bits="01")
        for index, label in enumerate(labels)
    ]

    for query in _DIFFERENTIAL_QUERIES:
        pattern = substructure_pattern(query)
        expected, unreadable = _loop_matches(labels, pattern)
        outcome = search._scan_for_matches(records, pattern, time.monotonic() + 600)
        assert [hit.smiles for hit in outcome.hits] == expected, query
        assert outcome.unreadable == unreadable == 1, query
        assert outcome.hits_truncated is False, query


def test_a_chiral_query_is_matched_the_way_the_loop_matched_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chiral query is matched as `HasSubstructMatch` matched it, with chirality off.

    `GetMatches` defaults `useChirality` to True and `HasSubstructMatch` to False, so the scan
    passes it explicitly. The second assertion pins that the upstream defaults still differ, so an
    RDKit change turns red with the reason attached.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 100_000)
    labels = [*_nci_corpus(1200)]
    records = [
        FingerprintRecord(id=f"{index:05d}", label=label, bits="01")
        for index, label in enumerate(labels)
    ]
    pattern = substructure_pattern("[C@H](O)(C)C")
    expected, _ = _loop_matches(labels, pattern)
    assert expected, "the fixture corpus must actually contain the chiral motif"

    outcome = search._scan_for_matches(records, pattern, time.monotonic() + 600)
    assert [hit.smiles for hit in outcome.hits] == expected

    index = substructure_index.index_for(records, time.monotonic() + 600)
    assert index is not None, "the fixture corpus is small enough to index inside the budget"
    at_upstream_default = index.library.GetMatches(pattern, maxResults=100_000)
    assert len(at_upstream_default) != len(expected), (
        "GetMatches and HasSubstructMatch now agree on useChirality; the explicit argument in "
        "CorpusIndex.labels_matching no longer defends against anything and its docstring is stale"
    )


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count real index builds without replacing one: a spy over `_build`, not a stand-in.

    The cache's whole claim is "this expensive thing happens once", and the only honest way to
    observe that is to let it happen and count it — a fake would be asserting against itself.
    """
    built = [0]
    real = substructure_index._build

    def _counting(
        labels: list[str], budget: float, deadline: float
    ) -> substructure_index.CorpusIndex:
        built[0] += 1
        return real(labels, budget, deadline)

    monkeypatch.setattr(substructure_index, "_build", _counting)
    return built


@pytest.fixture(autouse=True)
def _clear_the_substructure_index_cache() -> Iterator[None]:
    """Give every test its own index cache, so a counted build is one this test caused.

    The cache is process-global by design (keyed on the corpus), so it would leak between tests.
    """
    substructure_index._INDEXES = BoundedLru(
        lambda: settings.substructure_index_cache_entries,
    )
    substructure_index._BUILDS.clear()
    yield


async def test_the_index_is_built_once_and_reused_by_every_later_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The index is built once and reused by every later query.

    A build costs far more than one per-record scan, so a per-query rebuild would be slower than no
    index at all.
    """
    built = _count_builds(monkeypatch)

    store = InMemoryFingerprintStore()
    for identifier, smiles in [("a", "CCO"), ("b", "c1ccccc1"), ("c", "CC(=O)O")]:
        await store.add(record_for(identifier, smiles))
    first = await find_substructure_matches(store, "CO")
    second = await find_substructure_matches(store, "c1ccccc1")
    third = await find_substructure_matches(store, "C(=O)O")
    assert [h.smiles for h in first.hits] == ["CCO", "CC(=O)O"]  # both carry a C-O bond
    assert [h.smiles for h in second.hits] == ["c1ccccc1"]
    assert [h.smiles for h in third.hits] == ["CC(=O)O"]

    assert built == [1], f"three queries over one corpus built {built[0]} indexes"


async def test_a_rewritten_label_invalidates_the_index_although_the_row_count_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewritten label invalidates the index although the row count is unchanged.

    The upsert rewrites `label` in place with no revision column, so the cache key is a digest of
    the labels; a count, max id or timestamp would keep answering from a structure no longer stored.
    Driven end to end.
    """
    built = _count_builds(monkeypatch)

    store = InMemoryFingerprintStore()
    await store.add(record_for("only", "CCO"))
    assert (await find_substructure_matches(store, "[N-]=[N+]=N")).hits == []
    # The same id, so the store replaces the row rather than adding one: same count, same
    # ordering, same everything the schema could offer as a revision signal.
    await store.add(record_for("only", "CC(=O)N=[N+]=[N-]"))
    assert await store.count() == 1
    found = await find_substructure_matches(store, "[N-]=[N+]=N")
    assert [h.smiles for h in found.hits] == ["CC(=O)N=[N+]=[N-]"]

    assert built == [2], "the rewritten corpus was answered from the index built for the old one"


def test_the_index_cache_holds_no_more_than_the_configured_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The index cache holds no more than `substructure_index_cache_entries` indexes.

    Unbounded, it would keep one index per corpus generation ingest ever produced.
    """
    monkeypatch.setattr(settings, "substructure_index_cache_entries", 2)
    pattern = substructure_pattern("CCO")
    for generation in range(5):
        records = [FingerprintRecord(id="only", label="C" * (generation + 2) + "O", bits="01")]
        search._scan_for_matches(records, pattern, time.monotonic() + 600)
    assert len(substructure_index._INDEXES) == 2


def test_concurrent_misses_on_one_corpus_build_one_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent misses on one corpus build one index.

    The scan runs in the default executor, so concurrent misses are concurrent threads in
    `index_for`; the second waits on the build lock and finds the entry.
    """
    built = _count_builds(monkeypatch)
    records = [record_for(f"{index:03d}", "CCO") for index in range(50)]
    pattern = substructure_pattern("CO")
    outcomes: list[ScanOutcome] = []
    barrier = threading.Barrier(4)

    def _scan() -> None:
        barrier.wait()
        outcomes.append(search._scan_for_matches(records, pattern, time.monotonic() + 600))

    threads = [threading.Thread(target=_scan) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 4
    assert all(len(outcome.hits) == 50 for outcome in outcomes)
    assert built == [1], f"four concurrent misses built {built[0]} indexes"


async def test_an_unreadable_row_is_counted_even_when_the_result_cap_stops_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable row is counted even when the result cap stops the scan.

    Parsing happens once at index build over the whole slice, so `unreadable` and `scan_truncated`
    describe the corpus, not one query. This can only move the flag towards "not every record was
    examined", the conservative direction.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 2)

    store = InMemoryFingerprintStore()
    for index in range(4):
        await store.add(record_for(f"{100 + index}", "CCO"))
    # Bypass `record_for`, which would refuse to fingerprint it — a row that parsed when it was
    # indexed and does not now. Last by id, so the capped scan never reached it.
    await store.add(FingerprintRecord(id="900", label="not-a-molecule", bits="01"))

    result = await find_substructure_matches(store, "CO")
    assert len(result.hits) == 2 and result.hits_truncated is True
    assert result.scan_truncated is True
    # Both flags at once render as one sentence, and the half this test is about is the one
    # telling the model an operator has an index to repair.
    assert "repair the index" in result.verdict


def test_a_caller_with_no_time_left_builds_nothing_and_is_told_what_ran_out() -> None:
    """A caller with no time left builds nothing, caches nothing, and is told which scan ran out.

    A build is the most expensive step and runs in the shared executor. An abandoned partial index
    is not cached, since it would answer later queries over a fraction of the corpus unflagged; and
    the refusal names the indexing, not a match that never ran.
    """
    records = [record_for(f"{index:04d}", "CCO") for index in range(400)]
    pattern = substructure_pattern("CO")
    with pytest.raises(TimeoutError, match="gave up after 0 of 400 molecule\\(s\\)") as raised:
        search._scan_for_matches(records, pattern, time.monotonic() - 1)
    assert "one at a time" in str(raised.value), (
        "the refusal must say which of the two scans ran out of time; the remedies differ"
    )
    assert len(substructure_index._INDEXES) == 0

    # And the same records index fine once there is time for them, so the refusal above was the
    # deadline rather than the corpus.
    outcome = search._scan_for_matches(records, pattern, time.monotonic() + 600)
    assert len(outcome.hits) == settings.fingerprint_max_top_k
    assert len(substructure_index._INDEXES) == 1


def test_a_corpus_too_large_to_index_is_searched_record_by_record_instead_of_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corpus too large to index in its budget is searched record by record instead of refused.

    The build has its own budget, and a build that does not fit is skipped: `index_for` returns None
    and the scan matches per record, so a large corpus still answers. Driven with an impossible
    budget over the twelve query classes, since the fallback must answer exactly as the index would.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 100_000)
    monkeypatch.setattr(settings, "substructure_index_build_timeout_seconds", 0.001)
    labels = [*_nci_corpus(1200), "not-a-molecule((("]
    records = [
        FingerprintRecord(id=f"{index:05d}", label=label, bits="01")
        for index, label in enumerate(labels)
    ]

    for query in _DIFFERENTIAL_QUERIES:
        pattern = substructure_pattern(query)
        expected, unreadable = _loop_matches(labels, pattern)
        outcome = search._scan_for_matches(records, pattern, time.monotonic() + 600)
        assert [hit.smiles for hit in outcome.hits] == expected, query
        assert outcome.unreadable == unreadable == 1, query

    assert len(substructure_index._INDEXES) == 0, (
        "a build that could not meet its budget must cache nothing; a library holding a fraction "
        "of the corpus answers later queries over that fraction with no flag saying so"
    )


def test_a_build_that_cannot_meet_its_budget_costs_the_query_a_fraction_of_that_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build that cannot meet its budget gives up after a small fraction of the corpus.

    The budget is a projection from the build's measured rate every `_BUILD_CHECK_STRIDE` records,
    not a stopwatch run to exhaustion, which would spend the build budget before the fallback scan
    starts. Asserted as records parsed, the mechanism itself, with the budget calibrated from a full
    build in this run. The bar is a fraction of the corpus rather than a multiple of the stride, so
    widening the stride cannot let the refused build parse everything.
    """
    labels = _nci_corpus(1200)
    records = [
        FingerprintRecord(id=f"{index:05d}", label=label, bits="01")
        for index, label in enumerate(labels)
    ]

    started = time.perf_counter()
    substructure_index._build(labels, math.inf, time.monotonic() + 600)
    whole_build_seconds = time.perf_counter() - started
    monkeypatch.setattr(
        settings, "substructure_index_build_timeout_seconds", whole_build_seconds / 2
    )

    parsed = 0
    real_parse = Chem.MolFromSmiles

    def _counting_parse(label: str) -> Chem.Mol | None:
        nonlocal parsed
        parsed += 1
        return real_parse(label)

    monkeypatch.setattr(Chem, "MolFromSmiles", _counting_parse)
    refused = substructure_index.index_for(records, time.monotonic() + 600)

    assert refused is None, (
        "the build met a budget of half what the whole build costs, so this fixture is not "
        "exercising a refusal at all and everything below it is vacuous"
    )
    assert parsed <= len(labels) // 8, (
        f"the refused build parsed {parsed} of {len(labels)} molecule(s) against a budget of half "
        f"the whole build ({whole_build_seconds:.3f}s). A build that projects gives up at the "
        f"first check past the budget, which is _BUILD_CHECK_STRIDE = "
        f"{substructure_index._BUILD_CHECK_STRIDE} records; one that runs its deadline out reaches "
        "half the corpus. This is the second."
    )


def test_a_query_whose_index_is_cached_is_not_blocked_by_an_unrelated_corpus_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query whose index is cached is not blocked by an unrelated corpus building.

    Each new molecule mints a new corpus key, so under ingest a single global lock held across
    builds would serialize every query. The build is blocked here, so the assertion is whether the
    cached query waits at all.
    """
    cached = [record_for(f"c{index:03d}", "CCO") for index in range(50)]
    other = [record_for(f"o{index:03d}", "c1ccccc1" + "C" * (index + 1)) for index in range(50)]
    pattern = substructure_pattern("CO")
    search._scan_for_matches(cached, pattern, time.monotonic() + 600)
    assert len(substructure_index._INDEXES) == 1, "the fixture must leave one index cached"

    building = threading.Event()
    release = threading.Event()
    real = substructure_index._build

    def _blocking(
        labels: list[str], budget: float, deadline: float
    ) -> substructure_index.CorpusIndex:
        building.set()
        release.wait(30.0)
        return real(labels, budget, deadline)

    monkeypatch.setattr(substructure_index, "_build", _blocking)
    blocked = threading.Thread(
        target=search._scan_for_matches, args=(other, pattern, time.monotonic() + 600)
    )
    blocked.start()
    try:
        assert building.wait(30.0), "the second corpus never reached its build"
        began = time.monotonic()
        outcome = search._scan_for_matches(cached, pattern, time.monotonic() + 1.0)
        waited = time.monotonic() - began
    finally:
        release.set()
        blocked.join(30.0)

    assert len(outcome.hits) == 50
    assert waited < 0.5, (
        f"the cached corpus answered after {waited:.4f}s while an unrelated corpus was being "
        "indexed; a build must be single-flighted per corpus, not per process"
    )


async def test_the_refusal_names_the_scan_that_ran_out_of_time_and_a_remedy_that_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal names the scan that ran out of time and a remedy that works.

    It carries what the scan that gave up said about itself; `asyncio.wait_for`'s message-less
    `TimeoutError` gets text saying so. Driven through the seam, since which bound wins is a
    scheduling accident.
    """
    store = InMemoryFingerprintStore()
    for record in (record_for("a", "CCO"), record_for("b", "CC(=O)O")):
        await store.add(record)

    def _out_of_time(*_args: object, **_kwargs: object) -> ScanOutcome:
        raise TimeoutError("substructure scan gave up after 7 of 9 molecule(s), matching them one")

    monkeypatch.setattr(search, "_scan_for_matches", _out_of_time)
    with pytest.raises(FingerprintError) as raised:
        await find_substructure_matches(store, "CO")
    message = str(raised.value)
    assert "gave up after 7 of 9" in message, message
    assert "CHEMCLAW_SUBSTRUCTURE_SCAN_MAX_RECORDS" in message, (
        "a corpus too large for the bound is the remedy the old message never named"
    )

    def _silent(*_args: object, **_kwargs: object) -> ScanOutcome:
        raise TimeoutError

    monkeypatch.setattr(search, "_scan_for_matches", _silent)
    with pytest.raises(FingerprintError) as raised:
        await find_substructure_matches(store, "CO")
    assert "still inside one chunk" in str(raised.value), (
        "wait_for's TimeoutError carries no message; the text must say so rather than invent one"
    )


def test_the_scan_stops_within_a_time_slice_of_its_deadline_not_a_record_count() -> None:
    """The scan stops within a time slice of its deadline, not within a record count.

    `GetMatches` cannot be interrupted, so the deadline is checked between chunks; per-molecule cost
    spans orders of magnitude, so chunks are sized in time. Asserted in the bound's own unit.
    """
    pattern = substructure_pattern(_UNMATCHABLE)
    per_record = _one_match_seconds()
    records = _dendrimer_records(24)
    # Build first, so what is measured below is the scan rather than the parse.
    search._scan_for_matches(records, pattern, time.monotonic() + 3600)

    slice_seconds = settings.substructure_scan_deadline_slice_seconds
    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        search._scan_for_matches(records, pattern, time.monotonic())
    overrun = time.perf_counter() - started

    # One chunk past the deadline at most, and the first chunk of any scan is a single record —
    # so a deadline that has already passed costs nothing at all, and nothing near the 24-record
    # corpus a record-counted chunk would have run out.
    assert overrun < max(slice_seconds, per_record) * 2, (
        f"the scan ran {overrun:.3f}s past a deadline that had already passed "
        f"({overrun / per_record:.1f} records' worth of a {len(records)}-record corpus)"
    )
