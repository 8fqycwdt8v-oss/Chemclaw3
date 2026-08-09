"""Integration tests for the Postgres fingerprint store (plan steps 3.2/3.3).

Runs against a real pgvector database (CI provides one; the offline sandbox skips).
Proves the durable backend honors the same `FingerprintStore` contract as the in-memory
one: Tanimoto ranking in SQL returns most-similar-first, the threshold filters, and
substructure search works over it via the shared, backend-agnostic search functions.
"""

import asyncio

import pytest

from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.molfp.search import (
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)
from chemclaw.science.fingerprints.store import (
    InMemoryFingerprintStore,
    PostgresFingerprintStore,
    find_matches,
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

        hits = (await find_similar_molecules(store, "CCO", top_k=3, threshold=0.1)).hits
        assert hits[0].smiles == "CCO"
        assert hits[0].similarity == pytest.approx(1.0)
        assert "c1ccccc1" not in {h.smiles for h in hits}  # disjoint, below threshold
        assert all(
            (hits[i].similarity or 0.0) >= (hits[i + 1].similarity or 0.0)
            for i in range(len(hits) - 1)
        )

    asyncio.run(_run())


def test_tie_break_order_matches_the_in_memory_backend() -> None:
    """Equal-similarity hits come back in the same id order from both backends.

    The in-memory reference tie-breaks by Python's code-point sort; the SQL side must
    order identically (`id COLLATE "C"`), or the database's locale collation (e.g.
    en_US.UTF-8 puts 'a1' before 'B1') silently breaks the documented cross-backend
    determinism for mixed-case ids.

    Asserted at the *store* level (`find_matches`), which is where the collation lives and the
    only level that can see it: two records sharing one structure differ solely by id, and the
    molecule search presents a hit by its structure and the note it cites, not by its row id.
    """

    async def _run() -> None:
        pg_store = await _store_or_skip()
        mem_store = InMemoryFingerprintStore(definition=molecule_definition())
        octanol = "CCCCCCCCO"  # unique to this test so a high threshold isolates the tie
        for cid in ["pg-collate-a1", "pg-collate-B1"]:
            await pg_store.add(record_for(cid, octanol))
            await mem_store.add(record_for(cid, octanol))

        bits = ecfp_bitstring(octanol)
        pg_hits, _ = await find_matches(pg_store, bits, top_k=None, threshold=0.99)
        mem_hits, _ = await find_matches(mem_store, bits, top_k=None, threshold=0.99)
        pg_ids = [h.id for h in pg_hits if h.id.startswith("pg-collate-")]
        mem_ids = [h.id for h in mem_hits]
        assert pg_ids == mem_ids == ["pg-collate-B1", "pg-collate-a1"]  # code-point order

    asyncio.run(_run())


def test_upsert_and_substructure_over_postgres() -> None:
    """Re-adding an id replaces it; substructure search works over the durable backend."""

    async def _run() -> None:
        store = await _store_or_skip()
        await store.add(record_for("pg-mol", "CCO"))
        await store.add(record_for("pg-mol", "CC(=O)O"))  # replace ethanol with acetic acid

        acids = {r.smiles for r in (await find_substructure_matches(store, "C(=O)[OH]")).hits}
        assert "CC(=O)O" in acids  # the replaced record now matches the acid pattern

    asyncio.run(_run())


def test_emptiness_and_count_are_scoped_to_the_stores_definition() -> None:
    """The durable backend must answer "is anything searchable here?" as honestly as memory does.

    Asserted through a store pinned to a definition nothing was ever indexed under, which is both
    the robust way to test emptiness against a shared database (other tests' rows are invisible to
    it) and a real deployment state: after a fingerprint-definition change every existing row falls
    out of search (runbook (vi)), so a table full of stale rows is an index that answers nothing.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        orphaned = PostgresFingerprintStore(
            "molecule_fingerprints", settings.ecfp_bits, "ecfp:never-indexed:b2048"
        )
        assert await orphaned.is_empty() is True
        assert await orphaned.count() == 0
        # And the honesty travels all the way out to the search a chemist sees.
        search = await find_similar_molecules(orphaned, "CCO", threshold=0.1)
        assert search.hits == []
        assert search.index_empty is True
        assert "SEARCH NOT RUN" in search.model_dump()["verdict"]

        current = await _store_or_skip()
        await current.add(record_for("pg-count", "CCO"))
        assert await current.is_empty() is False
        assert await current.count() >= 1
        populated = await find_similar_molecules(current, "CCO", threshold=0.1)
        assert populated.index_empty is False

    asyncio.run(_run())
