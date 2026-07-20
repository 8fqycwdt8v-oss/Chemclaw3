"""Behavioral tests for the mcp-molfp capability (plan steps 3.1-3.3).

Proves the acceptance core of CHECKMATE 3 without a database: ECFP4 is deterministic
and config-sized, Tanimoto ranking returns most-similar-first neighbors honoring the
threshold and top_k, and substructure search filters by exact fragment containment.
The Postgres backend reproduces the same ranking in SQL (tested in CI).
"""

import asyncio

import pytest

from chemclaw.config import settings
from mcp_servers.molfp.fingerprint import FingerprintError, ecfp_bitstring, tanimoto
from mcp_servers.molfp.search import (
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)
from mcp_servers.molfp.store import InMemoryFingerprintStore


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
