"""Behavioral tests for the mcp-molfp capability (plan steps 3.1-3.3).

Proves the acceptance core of CHECKMATE 3 without a database: ECFP4 is deterministic
and config-sized, Tanimoto ranking returns most-similar-first neighbors honoring the
threshold and top_k, and substructure search filters by exact fragment containment.
The Postgres backend reproduces the same ranking in SQL (tested in CI).
"""

import asyncio
import time
from collections.abc import Callable

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp import search
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.molfp.search import (
    ScanOutcome,
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)
from chemclaw.science.fingerprints.store import (
    FingerprintError,
    FingerprintRecord,
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


def test_find_similar_ranks_by_tanimoto() -> None:
    """A query returns neighbors most-similar-first, filtered by threshold and top_k."""

    async def _run() -> None:
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
            (hits[i].similarity or 0.0) >= (hits[i + 1].similarity or 0.0)
            for i in range(len(hits) - 1)
        )

        # top_k truncates to the closest neighbors only.
        assert len((await find_similar_molecules(store, "CCO", top_k=2, threshold=0.1)).hits) == 2

    asyncio.run(_run())


def test_threshold_excludes_weak_matches() -> None:
    """Raising the threshold drops loosely related hits."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("propanol", "CCCO"))
        # Ethanol vs propanol ~0.56; a 0.9 threshold rejects it.
        assert (await find_similar_molecules(store, "CCO", threshold=0.9)).hits == []
        assert len((await find_similar_molecules(store, "CCO", threshold=0.5)).hits) == 1

    asyncio.run(_run())


def test_similarity_excludes_other_fingerprint_definitions() -> None:
    """A store bound to a definition ranks only records built under that same definition.

    This is the durable store's cross-definition guard (a changed Morgan radius yields
    equal-width but incomparable bits): a store pinned to the current definition must not
    return a record indexed under a different one, even if its raw bits look similar.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_substructure_matches_fragment() -> None:
    """Substructure search returns exactly the molecules containing the query fragment."""

    async def _run() -> None:
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

    asyncio.run(_run())


def test_substructure_bad_query_raises() -> None:
    """An unparseable substructure query is a clear error (G4)."""

    async def _run() -> None:
        with pytest.raises(FingerprintError, match="substructure query"):
            await find_substructure_matches(InMemoryFingerprintStore(), "%%%")

    asyncio.run(_run())


def test_substructure_empty_query_raises() -> None:
    """An empty query is an input error, not a silent empty result (G4).

    `MolFromSmarts("")` parses to a zero-atom pattern that matches nothing, so without
    the guard the tool reads as "no stored molecule contains the fragment".
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("ethanol", "CCO"))
        with pytest.raises(FingerprintError, match="empty substructure query"):
            await find_substructure_matches(store, "")

    asyncio.run(_run())


def test_substructure_oversized_query_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model-supplied query beyond the configured length bound is rejected (SEC-4).

    SMARTS matching is subgraph isomorphism run in-process over the scanned corpus, so a
    pathological multi-KB pattern must be refused up front, not matched for minutes.
    """
    monkeypatch.setattr(settings, "substructure_query_max_length", 16)

    async def _run() -> None:
        with pytest.raises(FingerprintError, match="exceeds 16 characters"):
            await find_substructure_matches(InMemoryFingerprintStore(), "C" * 17)

    asyncio.run(_run())


def test_substructure_hits_are_lean_and_capped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Substructure hits carry only id + label, and a broad query is capped, not unbounded.

    The fingerprint bits are an internal storage detail (~2KB of '0'/'1' per record); the
    MCP tool ships hits into the model context, so the result shape must stay lean and the
    hit count bounded by `fingerprint_max_top_k` — with a warning, never silently.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 2)

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        for cid, smiles in [("ethanol", "CCO"), ("propanol", "CCCO"), ("butanol", "CCCCO")]:
            await store.add(record_for(cid, smiles))
        with caplog.at_level("WARNING"):
            hits = (await find_substructure_matches(store, "CO")).hits
        assert len(hits) == 2  # three molecules match; the cap truncates to two
        assert any("substructure result capped" in r.message for r in caplog.records)
        assert not any(hasattr(h, "bits") for h in hits)  # lean shape: no fingerprint payload

    asyncio.run(_run())


def test_a_truncated_scan_does_not_render_as_a_genuine_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan the record cap cut short must not answer "we have no precedent for this".

    The regression: with the sole azide sorted last by id and the cap set below the corpus
    size, the scan never reached it and the payload read `hits: []`, `index_empty: false`,
    verdict "this is a genuine negative result". The truncation went to the log only, which
    the model never sees — the same failure `index_empty` exists to prevent, one cap over.
    """
    monkeypatch.setattr(settings, "substructure_scan_max_records", 20)

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_capped_hit_list_says_the_count_is_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit count is not the total when the scan stopped at the result cap.

    The mirror of the truncated-scan case: `fingerprint_max_top_k` stops the scan at the
    cap-th match, so the count is a lower bound, and the verdict has to say so in the payload.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 3)

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        for i in range(6):
            await store.add(record_for(f"{100 + i}", "CCO"))
        result = await find_substructure_matches(store, "CCO")
        assert len(result.hits) == 3
        assert result.hits_truncated is True and result.scan_truncated is False
        assert "PARTIAL RESULT" in result.model_dump()["verdict"]

    asyncio.run(_run())


def test_a_similarity_hit_list_cut_at_top_k_says_so_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sibling entry point had the same silence, and a comment declaring it correct.

    `find_similar_molecules` returns at most `fingerprint_top_k` (default 10) of however many
    clear the threshold, and set neither flag — so 18 qualifying molecules rendered as
    `"10 indexed molecule(s) matched this query."`, which reads as a total. That is exactly what
    `hits_truncated` was added to say on the substructure entry point next door, in the same
    commit, and `store.py`'s comment asserted the omission ("every other entry point leaves them
    so") rather than noticing it.

    A page that holds everything qualifying is still not partial — pinned below, because a flag
    that fires on every full page is the `len == cap` inference again.
    """
    monkeypatch.setattr(settings, "fingerprint_top_k", 10)

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_corpus_holding_exactly_the_result_cap_is_not_reported_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly `fingerprint_max_top_k` matches is a *complete* answer, not a lower bound.

    The boundary the first version of this flag could not see: it returned `True` the instant
    the cap-th match was appended, so `hits_truncated` was identical to `len(hits) == cap` for
    every input — the very inference `_scan_for_matches`'s docstring says it exists to replace.
    A corpus of exactly `cap` matches (default 100) rendered as `PARTIAL RESULT: … Do not report
    it as the complete set`, which is the lane's own defect pointing the other way.

    Both spellings are pinned: cap matches and nothing else, and cap matches followed by
    non-matching records (the flag must not fire merely because records remained unexamined —
    they were examined and did not match).
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 3)

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_corpus_holding_exactly_the_scan_cap_is_a_complete_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corpus of exactly `substructure_scan_max_records` was fully examined.

    `scan_truncated` was `len(records) == cap`, so a store sitting exactly on the cap (default
    5000) turned a true "no azide on file" into `SEARCH INCOMPLETE: … Report the search as
    inconclusive`. A clean negative reported as inconclusive is the same untruth as an
    incomplete scan reported as a negative — the flag has to distinguish "read cap records and
    there were more" from "read cap records and that was all of them".
    """
    monkeypatch.setattr(settings, "substructure_scan_max_records", 5)

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_row_that_no_longer_parses_makes_the_scan_incomplete() -> None:
    """A record the scan could not read is a record it did not examine, and that is the flag.

    `_scan_for_matches` skips an unparseable stored SMILES so one bad row cannot hide every real
    hit — but it recorded nothing, so a corpus whose only azide carried a malformed label answered
    `hits: []` under "this is a genuine negative result". That is precisely what `scan_truncated`
    is documented to rule out ("not every stored record was examined"), reached by the other of
    the two ways it can happen.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("ok", "CCO"))
        # Bypass `record_for`, which would refuse to fingerprint it — this is a row that parsed
        # when it was indexed and does not now (a lenient canonicalization, a changed RDKit).
        await store.add(FingerprintRecord(id="broken", label="not-a-molecule", bits="01"))
        result = await find_substructure_matches(store, "[N-]=[N+]=N")
        assert result.hits == [] and result.scan_truncated is True
        assert "genuine negative" not in result.verdict
        assert "SEARCH INCOMPLETE" in result.verdict

    asyncio.run(_run())


def test_a_complete_substructure_scan_reports_no_truncation() -> None:
    """The common case stays unchanged: both flags false, and the verdict keeps its wording.

    The counterfactual for the two tests above — without it they would also pass on a build
    that flagged every search as partial.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("ethanol", "CCO"))
        hit = await find_substructure_matches(store, "CCO")
        assert hit.scan_truncated is False and hit.hits_truncated is False
        assert hit.verdict == "1 indexed molecule(s) matched this query."
        miss = await find_substructure_matches(store, "[N-]=[N+]=N")
        assert miss.hits == [] and "genuine negative" in miss.verdict

    asyncio.run(_run())


def _sleeping_scan(seconds: float) -> Callable[..., ScanOutcome]:
    """A stand-in for the CPU-bound scan that blocks its thread for `seconds`, then matches nothing.

    A real pathological SMARTS would take minutes and is not reproducible across RDKit versions;
    what both tests need is only that the scan blocks a *thread*, which this reproduces exactly.
    """

    def _scan(*_args: object, **_kwargs: object) -> ScanOutcome:
        time.sleep(seconds)
        return ScanOutcome([], False, 0)

    return _scan


def test_slow_substructure_match_times_out_with_a_clear_error(
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

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("ethanol", "CCO"))
        with pytest.raises(FingerprintError, match="exceeded 0.05s"):
            await find_substructure_matches(store, "CO")

    asyncio.run(_run())


def test_substructure_match_does_not_block_the_event_loop(
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

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("ethanol", "CCO"))
        await asyncio.gather(find_substructure_matches(store, "CO"), _tick())

    asyncio.run(_run())
    assert ticks == 20  # the concurrent task ran to completion during the blocking match


def test_agent_supplied_top_k_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large model-supplied `top_k` is clamped to `fingerprint_max_top_k` (SEC-4).

    The similarity tools take `top_k` from the model and it lands in a SQL `LIMIT`; clamp it
    so an arbitrarily large value cannot become an unbounded query — mirrors `graph_max_hops`.
    """
    monkeypatch.setattr(settings, "fingerprint_max_top_k", 2)

    async def _run() -> None:
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

    asyncio.run(_run())


def test_agent_supplied_threshold_is_clamped() -> None:
    """A model-supplied `threshold` is clamped to Tanimoto's [0, 1] range (SEC-4).

    `threshold` lands in the SQL similarity comparison exactly like `top_k` lands in
    `LIMIT`, so the config-side `[0, 1]` bound must also hold for the per-call override:
    a negative value would bless disjoint structures as neighbors, and >1 would silently
    report "no precedent" instead of returning an exact match.
    """

    class _RecordingStore:
        """Minimal FingerprintStore capturing what threshold reaches the backend."""

        def __init__(self) -> None:
            self.thresholds: list[float] = []

        async def add(self, record: FingerprintRecord) -> None:
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

    async def _run() -> None:
        recording = _RecordingStore()
        await find_matches(recording, "01", threshold=-5.0)
        await find_matches(recording, "01", threshold=1.5)
        assert recording.thresholds == [0.0, 1.0]

        # End to end: an over-1 threshold still returns the exact match instead of [].
        store = InMemoryFingerprintStore()
        await store.add(record_for("ethanol", "CCO"))
        hits = (await find_similar_molecules(store, "CCO", threshold=99.0)).hits
        assert [h.smiles for h in hits] == ["CCO"]

    asyncio.run(_run())


def test_all_records_limit_is_bounded_and_deterministic() -> None:
    """`all_records(limit=n)` returns the first n records in id order (bounded scan)."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        for cid in ["c", "a", "b"]:
            await store.add(record_for(cid, "CCO"))
        assert [r.id for r in await store.all_records(limit=2)] == ["a", "b"]
        assert len(await store.all_records()) == 3  # unbounded still returns all

    asyncio.run(_run())


def test_substructure_scan_caps_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The substructure scan is bounded by config and warns (not silently) when it truncates."""
    monkeypatch.setattr(settings, "substructure_scan_max_records", 1)

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        for cid in ["aspirin", "benzene", "toluene"]:
            await store.add(record_for(cid, "c1ccccc1" if cid != "aspirin" else "Cc1ccccc1"))
        with caplog.at_level("WARNING"):
            hits = (await find_substructure_matches(store, "c1ccccc1")).hits
        # Only the one capped record is scanned, so at most one match is returned.
        assert len(hits) <= 1
        assert any("substructure scan hit" in r.message for r in caplog.records)

    asyncio.run(_run())


class _NullConnection:
    """A psycopg connection stand-in: enterable, and nothing is executed on it."""

    async def __aenter__(self) -> "_NullConnection":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def test_postgres_store_applies_the_configured_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Postgres backend must bound its (slow HNSW) queries like every other store (COR-5/CON-2).

    A regression pin for the fpstore-only omission. It asserts on the libpq `options` the connect
    actually receives rather than on the keyword `_connection` passes: since
    D-2026-08-08-a-borrowed-connection-is-bounded-by-default the store passes no keyword at all and
    `db.connection` supplies the bound, so the old assertion would have proven only that this
    store still repeats itself — not that a long similarity scan is cancelled rather than pinning
    its worker. Verified offline by capturing the psycopg connect.
    """
    captured: dict[str, object] = {}

    async def _fake_connect(dsn: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return _NullConnection()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _fake_connect)
    store = PostgresFingerprintStore(
        "molecule_fingerprints", settings.ecfp_bits, molecule_definition()
    )

    async def _enter() -> None:
        async with store._connection():
            pass

    asyncio.run(_enter())

    expected = int(settings.pg_statement_timeout_seconds * 1000)
    assert f"-c statement_timeout={expected}" in str(captured["options"])


# --- An empty index must not answer "nothing similar" --------------------------------------------
#
# The live-run defect (docs/archive/live-grounded-2026-08-03.md, finding 6): the fingerprint tables
# were never backfilled, so the one tool whose job is "have we seen this before" answered `[]` —
# indistinguishable from a genuinely novel structure. These tests pin the distinction *and* that it
# survives serialization, which is where the same fix failed before (`ScreenResult.verdict`).


def test_an_empty_index_reports_that_the_search_was_not_run() -> None:
    """No records: the result says the question was not answered, not that the answer is no."""

    async def _run() -> None:
        search_result = await find_similar_molecules(InMemoryFingerprintStore(), "CCO")
        assert search_result.hits == []
        assert search_result.index_empty is True
        assert "SEARCH NOT RUN" in search_result.verdict
        assert "NOT evidence" in search_result.verdict

    asyncio.run(_run())


def test_the_empty_index_signal_survives_model_dump() -> None:
    """The verdict must be *serialized*, or the model that writes the answer never sees it.

    This is the whole reason `verdict` is a `computed_field` and not a bare `property`: MCP ships
    `model_dump()`, and a plain property is dropped there — exactly how `ScreenResult.verdict`
    ended up with zero production callers while a chemist was told "no hazards detected".
    """

    async def _run() -> None:
        payload = (await find_similar_molecules(InMemoryFingerprintStore(), "CCO")).model_dump()
        assert payload["index_empty"] is True
        assert "SEARCH NOT RUN" in payload["verdict"]

    asyncio.run(_run())


def test_a_populated_index_with_no_match_is_a_genuine_negative() -> None:
    """Records exist and none matched: the ordinary "no precedent" answer, clearly distinguished."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("benzene", "c1ccccc1"))
        # Ethanol vs benzene share no bits, so the search is real and finds nothing.
        search_result = await find_similar_molecules(store, "CCO", threshold=0.5)
        assert search_result.hits == []
        assert search_result.index_empty is False
        assert "SEARCH NOT RUN" not in search_result.verdict
        assert "genuine negative" in search_result.verdict

    asyncio.run(_run())


def test_a_hit_is_unaffected_by_the_emptiness_signal() -> None:
    """Regression guard: a real match still reports its hits, with the index not flagged empty."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("ethanol", "CCO"))
        search_result = await find_similar_molecules(store, "CCO", threshold=0.1)
        assert [h.smiles for h in search_result.hits] == ["CCO"]
        assert search_result.index_empty is False
        assert search_result.verdict.startswith("1 indexed molecule(s) matched")

    asyncio.run(_run())


def test_an_index_of_only_stale_definitions_counts_as_empty() -> None:
    """Rows the store cannot rank are not "records we searched" — they are nothing, honestly.

    A definition change orphans every row until it is re-indexed (runbook (vi)). Search returns
    none of them, so reporting the index as populated would produce exactly the defect this
    distinction exists to prevent, one config change further along.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore(definition=molecule_definition())
        await store.add(
            FingerprintRecord(
                id="stale", label="CCO", bits=ecfp_bitstring("CCO"), definition="ecfp:r9:b2048"
            )
        )
        search_result = await find_similar_molecules(store, "CCO", threshold=0.1)
        assert search_result.index_empty is True
        assert await store.count() == 0

    asyncio.run(_run())


def test_substructure_search_makes_the_same_distinction() -> None:
    """The third tool over the same index has the same failure mode, so it gets the same answer."""

    async def _run() -> None:
        empty = await find_substructure_matches(InMemoryFingerprintStore(), "c1ccccc1")
        assert empty.hits == [] and empty.index_empty is True
        assert "SEARCH NOT RUN" in empty.model_dump()["verdict"]

        store = InMemoryFingerprintStore()
        await store.add(record_for("ethanol", "CCO"))
        populated = await find_substructure_matches(store, "c1ccccc1")
        assert populated.hits == [] and populated.index_empty is False
        assert "genuine negative" in populated.verdict

    asyncio.run(_run())


def test_the_emptiness_probe_is_skipped_when_the_search_found_hits() -> None:
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

    async def _run() -> None:
        store = _CountingStore()
        await store.add(record_for("ethanol", "CCO"))
        assert (await find_similar_molecules(store, "CCO", threshold=0.1)).hits  # a hit
        assert _CountingStore.probes == 0
        # Benzene shares no bits with the indexed ethanol, so this search legitimately finds none.
        assert (await find_similar_molecules(store, "c1ccccc1", threshold=0.1)).hits == []
        assert _CountingStore.probes == 1  # asked only once the result was empty

    asyncio.run(_run())


def test_the_startup_report_warns_only_when_the_index_is_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator half: the owning connector says at startup what its index holds.

    WARNING for an empty index (actionable and wrong), INFO with the count otherwise — so a
    half-finished backfill is visible as a number rather than hidden behind a boolean.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_the_startup_report_never_takes_the_connector_down() -> None:
    """A report that cannot read the database logs and returns — a diagnostic may not be fatal."""

    class _BrokenStore(InMemoryFingerprintStore):
        """A store whose count fails the way an unreachable Postgres does."""

        async def count(self) -> int:
            raise ConnectionError("Postgres unreachable at postgres://host/db")

    asyncio.run(log_index_size(_BrokenStore(), "molecule"))  # must not raise
