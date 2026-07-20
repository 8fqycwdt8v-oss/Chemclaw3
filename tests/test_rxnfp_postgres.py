"""Integration test for the Postgres reaction fingerprint store (plan step 3.4).

Runs against a real pgvector database (CI provides one; the offline sandbox skips).
Proves the reaction table + generic backend rank DRFP Tanimoto neighbors in SQL.
"""

import asyncio

import psycopg
import pytest

from calc.migrate import migrate
from chemclaw.config import settings
from mcp_servers.fpstore import PostgresFingerprintStore
from mcp_servers.rxnfp.search import find_similar_reactions, record_for_reaction

_ESTER_ETHYL = "CCO.CC(=O)O>>CCOC(C)=O"
_ESTER_PROPYL = "CCCO.CC(=O)O>>CCCOC(C)=O"
_HALOGENATION = "c1ccccc1.BrBr>>Brc1ccccc1"


async def _store_or_skip() -> PostgresFingerprintStore:
    """Return a migrated Postgres reaction store, or skip if no database is reachable."""
    try:
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        await conn.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (offline sandbox): {exc}")
    await migrate()
    return PostgresFingerprintStore("reaction_fingerprints", settings.drfp_bits)


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

        hits = await find_similar_reactions(store, _ESTER_ETHYL, top_k=2, threshold=0.1)
        assert hits[0].id == "pg-ethyl"
        assert hits[0].similarity == pytest.approx(1.0)
        assert "pg-halogenation" not in {h.id for h in hits}

    asyncio.run(_run())
