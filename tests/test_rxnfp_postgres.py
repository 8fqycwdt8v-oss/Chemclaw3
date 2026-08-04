"""Integration test for the Postgres reaction fingerprint store (plan step 3.4).

Runs against a real pgvector database (CI provides one; the offline sandbox skips).
Proves the reaction table + generic backend rank DRFP Tanimoto neighbors in SQL.
"""

import asyncio

import pytest

from chemclaw.core.config import settings
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions, record_for_reaction
from chemclaw.science.fingerprints.store import PostgresFingerprintStore
from tests.pg import migrated_db_or_skip

_ESTER_ETHYL = "CCO.CC(=O)O>>CCOC(C)=O"
_ESTER_PROPYL = "CCCO.CC(=O)O>>CCCOC(C)=O"
_HALOGENATION = "c1ccccc1.BrBr>>Brc1ccccc1"


async def _store_or_skip() -> PostgresFingerprintStore:
    """Return a migrated Postgres reaction store, or skip if no database is reachable."""
    await migrated_db_or_skip()
    return PostgresFingerprintStore(
        "reaction_fingerprints", settings.drfp_bits, reaction_definition()
    )


def test_reaction_similarity_ranking_in_sql() -> None:
    """The SQL backend ranks DRFP Tanimoto neighbors most-similar-first, honoring threshold."""

    async def _run() -> None:
        store = await _store_or_skip()
        for rid, rxn in [
            ("pg-ethyl", _ESTER_ETHYL),
            ("pg-propyl", _ESTER_PROPYL),
            ("pg-halogenation", _HALOGENATION),
        ]:
            await store.add(record_for_reaction(rid, rxn))

        hits = (await find_similar_reactions(store, _ESTER_ETHYL, top_k=2, threshold=0.1)).hits
        assert hits[0].id == "pg-ethyl"
        assert hits[0].similarity == pytest.approx(1.0)
        assert "pg-halogenation" not in {h.id for h in hits}

    asyncio.run(_run())


def test_an_unbuilt_reaction_index_says_so_over_postgres() -> None:
    """The live-run defect's own backend: an index with nothing searchable must report it.

    Pinned to a definition nothing was indexed under, so the assertion holds against the shared
    CI database — and mirrors the real state it stands in for, a table never backfilled.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        orphaned = PostgresFingerprintStore(
            "reaction_fingerprints", settings.drfp_bits, "drfp:never-indexed:b2048"
        )
        assert await orphaned.is_empty() is True
        assert await orphaned.count() == 0

        search = await find_similar_reactions(orphaned, _ESTER_ETHYL, threshold=0.1)
        assert search.hits == []
        assert search.index_empty is True
        assert "SEARCH NOT RUN" in search.model_dump()["verdict"]

        current = await _store_or_skip()
        await current.add(record_for_reaction("pg-empty-guard", _ESTER_PROPYL))
        assert await current.is_empty() is False
        assert await current.count() >= 1
        assert (
            await find_similar_reactions(current, _ESTER_PROPYL, threshold=0.1)
        ).index_empty is False

    asyncio.run(_run())
