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


def test_the_durable_page_is_the_exact_top_k_not_an_approximation() -> None:
    """The durable search returns the *exact* page, ties included — the property an ANN loses.

    `PostgresFingerprintStore`'s docstring used to say this search was "accelerated by the table's
    HNSW `bit_jaccard_ops` index … approximate by design". Measured on 200 000 `bit(2048)` rows,
    the planner never takes that plan: the `definition` equality and the threshold predicate cost
    it the ordered index scan, so what ships is an exact sequential scan — 17.6 ms there, ~0.088
    µs/row, i.e. ~880 ms at the 10^7 rows Pistachio implies. Ordering by the index first and
    filtering afterwards is 14x faster (1.25 ms, roughly flat in N) and is **not** the same answer:
    over 60 queries at `hnsw.ef_search=200` with a 10x over-fetch it returned a different result
    set for 22 of them.

    The mechanism is ties rather than recall, which is why this test is written the way it is.
    Tanimoto over sparse bit vectors puts many rows at *identical* similarity, and `ORDER BY
    distance, id COLLATE "C"` breaks those ties across the whole table — something no truncated
    candidate set can reproduce. So: 200 rows of one structure, a page of 50. Exactly the 50
    lowest ids must come back, in order. An ANN would return 50 equally-similar rows in graph
    order, pass every similarity assertion in this file, and quietly answer a different question.

    That is a decision (a structural search that may silently miss a precedent) rather than a
    refactor, so it belongs in an ADR — and this test is what makes taking it deliberate.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        mem_store = InMemoryFingerprintStore(definition=molecule_definition())
        # One structure unique to this test, so a 0.99 threshold isolates these rows from every
        # other fixture in the shared table and the whole page is a tie.
        structure = "CCCCCCCCCCCCO"
        ids = [f"pg-exact-{index:03d}" for index in range(200)]
        records = [record_for(cid, structure) for cid in ids]
        await store.add_many(records)
        for record in records:
            await mem_store.add(record)

        bits = ecfp_bitstring(structure)
        page, truncated = await find_matches(store, bits, top_k=50, threshold=0.99)
        reference, _ = await find_matches(mem_store, bits, top_k=50, threshold=0.99)

        assert [hit.id for hit in page] == sorted(ids)[:50], (
            "the durable page is not the exact lowest-id half of the tie — an approximate scan "
            "returns 50 equally-similar rows in whatever order it found them"
        )
        assert [hit.id for hit in page] == [hit.id for hit in reference], (
            "the two backends disagree about which 50 of 200 tied rows the page holds"
        )
        assert truncated, "150 rows over the page went unreported"

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
