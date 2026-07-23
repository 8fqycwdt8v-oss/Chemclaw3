"""Behavioral tests for the mcp-molfp capability (plan steps 3.1-3.3).

Proves the acceptance core of CHECKMATE 3 without a database: ECFP4 is deterministic
and config-sized, Tanimoto ranking returns most-similar-first neighbors honoring the
threshold and top_k, and substructure search filters by exact fragment containment.
The Postgres backend reproduces the same ranking in SQL (tested in CI).
"""

import asyncio

import pytest

from chemclaw import db
from chemclaw.config import settings
from mcp_servers.fpstore import (
    FingerprintError,
    FingerprintRecord,
    InMemoryFingerprintStore,
    Match,
    PostgresFingerprintStore,
    find_matches,
    tanimoto,
)
from mcp_servers.molfp.fingerprint import ecfp_bitstring, molecule_definition
from mcp_servers.molfp.search import (
    find_similar_molecules,
    find_substructure_matches,
    record_for,
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

        hits = await find_similar_molecules(store, "CCO", threshold=0.1)
        ids = [h.id for h in hits]
        assert ids[0] == "ethanol"  # exact match ranks first
        assert "benzene" not in ids  # disjoint, below threshold
        # Similarity is monotonically non-increasing down the list.
        assert all(hits[i].similarity >= hits[i + 1].similarity for i in range(len(hits) - 1))

        # top_k truncates to the closest neighbors only.
        assert len(await find_similar_molecules(store, "CCO", top_k=2, threshold=0.1)) == 2

    asyncio.run(_run())


def test_threshold_excludes_weak_matches() -> None:
    """Raising the threshold drops loosely related hits."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for("propanol", "CCCO"))
        # Ethanol vs propanol ~0.56; a 0.9 threshold rejects it.
        assert await find_similar_molecules(store, "CCO", threshold=0.9) == []
        assert len(await find_similar_molecules(store, "CCO", threshold=0.5)) == 1

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

        hits = await find_similar_molecules(store, "CCO", threshold=0.1)
        ids = [h.id for h in hits]
        assert ids == ["current"]  # the stale-definition row is excluded, not ranked

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

        ring = {r.id for r in await find_substructure_matches(store, "c1ccccc1")}
        assert ring == {"aspirin", "benzene"}  # only the aromatic molecules

        acids = {r.id for r in await find_substructure_matches(store, "C(=O)[OH]")}
        assert acids == {"aspirin", "acetic_acid"}  # carboxylic-acid SMARTS

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
            hits = await find_substructure_matches(store, "CO")
        assert len(hits) == 2  # three molecules match; the cap truncates to two
        assert any("substructure result capped" in r.message for r in caplog.records)
        assert not any(hasattr(h, "bits") for h in hits)  # lean shape: no fingerprint payload

    asyncio.run(_run())


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
        hits = await find_similar_molecules(store, "CCO", top_k=1_000_000, threshold=0.1)
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

    async def _run() -> None:
        recording = _RecordingStore()
        await find_matches(recording, "01", threshold=-5.0)
        await find_matches(recording, "01", threshold=1.5)
        assert recording.thresholds == [0.0, 1.0]

        # End to end: an over-1 threshold still returns the exact match instead of [].
        store = InMemoryFingerprintStore()
        await store.add(record_for("ethanol", "CCO"))
        hits = await find_similar_molecules(store, "CCO", threshold=99.0)
        assert [h.id for h in hits] == ["ethanol"]

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
            hits = await find_substructure_matches(store, "c1ccccc1")
        # Only the one capped record is scanned, so at most one match is returned.
        assert len(hits) <= 1
        assert any("substructure scan hit" in r.message for r in caplog.records)

    asyncio.run(_run())


def test_postgres_store_applies_the_configured_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Postgres backend must bound its (slow HNSW) queries like every other store (COR-5/CON-2).

    A regression pin for the fpstore-only omission: `_connect` must forward
    `pg_statement_timeout_seconds` to the shared `db.connect`, so a long similarity scan is
    cancelled rather than pinning its worker. Verified offline by capturing the connect call.
    """
    captured: dict[str, object] = {}

    async def _fake_connect(dsn: str, **kwargs: object) -> object:
        captured["dsn"] = dsn
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db, "connect", _fake_connect)
    store = PostgresFingerprintStore(
        "molecule_fingerprints", settings.ecfp_bits, molecule_definition()
    )
    asyncio.run(store._connect())

    assert captured["statement_timeout_seconds"] == settings.pg_statement_timeout_seconds
