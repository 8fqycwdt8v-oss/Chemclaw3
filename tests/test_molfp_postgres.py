"""Integration tests for the Postgres fingerprint store (plan steps 3.2/3.3).

Runs against a real pgvector database (CI provides one; the offline sandbox skips).
Proves the durable backend honors the same `FingerprintStore` contract as the in-memory
one: Tanimoto ranking in SQL returns most-similar-first, the threshold filters, and
substructure search works over it via the shared, backend-agnostic search functions.
"""

import asyncio

import pytest

from chemclaw.config import settings
from mcp_servers.fpstore import PostgresFingerprintStore
from mcp_servers.molfp.fingerprint import molecule_definition
from mcp_servers.molfp.search import (
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)
from tests.pg import migrated_db_or_skip


async def _store_or_skip() -> PostgresFingerprintStore:
    """Return a migrated Postgres fingerprint store, or skip if no database is reachable."""
    await migrated_db_or_skip()
    return PostgresFingerprintStore(
        "molecule_fingerprints", settings.ecfp_bits, molecule_definition()
    )


def test_similarity_ranking_in_sql() -> None:
    """The SQL backend ranks Tanimoto neighbors most-similar-first, honoring threshold."""

    async def _run() -> None:
        store = await _store_or_skip()
        for cid, smiles in [
            ("pg-ethanol", "CCO"),
            ("pg-propanol", "CCCO"),
            ("pg-butanol", "CCCCO"),
            ("pg-benzene", "c1ccccc1"),
        ]:
            await store.add(record_for(cid, smiles))

        hits = await find_similar_molecules(store, "CCO", top_k=3, threshold=0.1)
        assert hits[0].id == "pg-ethanol"
        assert hits[0].similarity == pytest.approx(1.0)
        assert "pg-benzene" not in {h.id for h in hits}  # disjoint, below threshold
        assert all(hits[i].similarity >= hits[i + 1].similarity for i in range(len(hits) - 1))

    asyncio.run(_run())


def test_upsert_and_substructure_over_postgres() -> None:
    """Re-adding an id replaces it; substructure search works over the durable backend."""

    async def _run() -> None:
        store = await _store_or_skip()
        await store.add(record_for("pg-mol", "CCO"))
        await store.add(record_for("pg-mol", "CC(=O)O"))  # replace ethanol with acetic acid

        acids = {r.id for r in await find_substructure_matches(store, "C(=O)[OH]")}
        assert "pg-mol" in acids  # the replaced record now matches the acid pattern

    asyncio.run(_run())
