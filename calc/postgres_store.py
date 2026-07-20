"""Postgres backend for the calculation store (plan step 1b.3).

Implements the same `ResultStore` interface as `InMemoryStore`, backed by the
`calculation_results` table (see `infra/sql/001_calculation_results.sql`), so
results survive process restarts and are shared across workers. A `put` is an
upsert keyed by the flat calculation key; a `get` is a single primary-key lookup.
The DSN comes from the one config source.
"""

import json

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from calc.store import CalculationKey, StoredResult
from chemclaw.config import settings

_UPSERT = """
    INSERT INTO calculation_results
        (key, calc_type, calc_version, input_hash, params_hash, result, provenance)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (key) DO UPDATE SET
        result = EXCLUDED.result,
        provenance = EXCLUDED.provenance,
        created_at = now()
"""

_SELECT = "SELECT result, provenance FROM calculation_results WHERE key = %s"


class PostgresStore:
    """Durable `ResultStore` backed by Postgres.

    Opens a short-lived connection per call: calculations are coarse-grained and
    infrequent relative to their cost, so a connection pool would be premature
    complexity here (KISS). Introduce pooling only if store traffic proves it.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def _connect(self) -> psycopg.AsyncConnection[TupleRow]:
        """Open a connection that fails fast on an unreachable database.

        Without `connect_timeout`, an unreachable host hangs the calling activity
        until its start-to-close timeout — the bound belongs to the connect.
        """
        return await psycopg.AsyncConnection.connect(
            self._dsn, connect_timeout=settings.pg_connect_timeout_seconds
        )

    async def get(self, key: CalculationKey) -> StoredResult | None:
        """Return the stored result for `key`, or None on a miss."""
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT, (key.as_str(),))
                row = await cur.fetchone()
        if row is None:
            return None
        result, provenance = row
        # JSONB comes back already parsed by psycopg; str only if driver differs.
        payload = result if isinstance(result, dict) else json.loads(result)
        return StoredResult(key=key, result=payload, provenance=provenance)

    async def put(self, stored: StoredResult) -> None:
        """Persist `stored`, overwriting any existing result for its key."""
        key = stored.key
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _UPSERT,
                    (
                        key.as_str(),
                        key.calc_type,
                        key.calc_version,
                        key.input_hash,
                        key.params_hash,
                        Jsonb(stored.result),
                        stored.provenance,
                    ),
                )
            await conn.commit()
