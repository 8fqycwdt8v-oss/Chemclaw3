"""Postgres backend for the fingerprint store (plan steps 3.2/3.3).

Implements the same `FingerprintStore` interface as `InMemoryFingerprintStore`, backed
by the `molecule_fingerprints` table (see `infra/sql/002_molecule_fingerprints.sql`).
Similarity ranking is Tanimoto (= 1 - Jaccard distance) computed in SQL and accelerated
by the HNSW `bit_jaccard_ops` index, so search scales to real corpora. Its query
semantics (threshold `>=`, `similarity desc, id` order) match the in-memory backend, but
HNSW is an *approximate* nearest-neighbour index: on a large corpus its order and
membership agree with the exact in-memory backend only up to HNSW recall (`hnsw.ef_search`),
and the post-index `WHERE` filter can return fewer than `top_k` even when more rows
qualify. The DSN comes from the one config source.
"""

import psycopg

from chemclaw.config import settings
from mcp_servers.molfp.store import Match, MoleculeRecord

# The cast width is the one config value, not a repeated literal — and it must match the
# `bit(N)` column in infra/sql/002. If they disagree, Postgres raises a bit-length error
# on insert/search (a loud failure), instead of silently padding to the column width.
_WIDTH = settings.ecfp_bits

_UPSERT = f"""
    INSERT INTO molecule_fingerprints (id, smiles, bits)
    VALUES (%(id)s, %(smiles)s, %(bits)s::bit({_WIDTH}))
    ON CONFLICT (id) DO UPDATE SET
        smiles = EXCLUDED.smiles,
        bits = EXCLUDED.bits
"""

_ALL = "SELECT id, smiles, bits::text FROM molecule_fingerprints"

# Tanimoto = 1 - Jaccard distance (`<%%>`; `%` is doubled to escape psycopg formatting).
# Filter by the threshold first, then rank by distance and truncate — the same
# "threshold then top-k" semantics as the in-memory backend. Ties break by id.
_SIMILAR = f"""
    SELECT id, smiles, 1 - (bits <%%> %(q)s::bit({_WIDTH})) AS similarity
    FROM molecule_fingerprints
    WHERE 1 - (bits <%%> %(q)s::bit({_WIDTH})) >= %(threshold)s
    ORDER BY bits <%%> %(q)s::bit({_WIDTH}), id
    LIMIT %(k)s
"""


class PostgresFingerprintStore:
    """Durable `FingerprintStore` backed by Postgres + pgvector.

    Opens a short-lived connection per call, matching the calculation store's choice:
    fingerprint writes/searches are infrequent relative to their value, so a pool would
    be premature (KISS).
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def add(self, record: MoleculeRecord) -> None:
        """Insert or replace a molecule fingerprint by id."""
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                _UPSERT, {"id": record.id, "smiles": record.smiles, "bits": record.bits}
            )
            await conn.commit()

    async def all_records(self) -> list[MoleculeRecord]:
        """Return every stored record (bits as a text bitstring)."""
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_ALL)
                rows = await cur.fetchall()
        return [MoleculeRecord(id=r[0], smiles=r[1], bits=r[2]) for r in rows]

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Return up to `top_k` records with Tanimoto >= `threshold`, most similar first."""
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SIMILAR, {"q": query_bits, "threshold": threshold, "k": top_k})
                rows = await cur.fetchall()
        return [Match(id=r[0], smiles=r[1], similarity=float(r[2])) for r in rows]
