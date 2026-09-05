"""Postgres backend for the calculation store (plan step 1b.3).

Implements the same `ResultStore` interface as `InMemoryStore`, backed by the
`calculation_results` table (see `infra/sql/001_calculation_results.sql`), so
results survive process restarts and are shared across workers. A `put` is an
upsert keyed by the flat calculation key; a `get` is a single primary-key lookup.
The DSN comes from the one config source.
"""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    ResultStore,
    StoredResult,
    molecule_hash,
)

_UPSERT = """
    INSERT INTO calculation_results
        (key, calc_type, calc_version, input_hash, params_hash, result, provenance,
         compute_seconds, structure_id, epoch)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (key) DO UPDATE SET
        result = EXCLUDED.result,
        provenance = EXCLUDED.provenance,
        -- Keep a recorded geometry when a rewrite does not carry one, by the same rule as the
        -- cost below: a row written before migration 048, or by a caller that did not ask the
        -- server for an identity, must not erase what an earlier write knew.
        structure_id = CASE
            WHEN EXCLUDED.structure_id <> '' THEN EXCLUDED.structure_id
            ELSE calculation_results.structure_id
        END,
        -- The same rule for the epoch (migration 083): a writer that records one states it, and a
        -- writer that does not must not erase one. There is no third case — `StoredResult.epoch`
        -- defaults to `CALCULATION_EPOCH`, so only an explicit "" arrives empty.
        epoch = CASE
            WHEN EXCLUDED.epoch <> '' THEN EXCLUDED.epoch
            ELSE calculation_results.epoch
        END,
        -- **The cost belongs to the payload it measured, and a rewrite that changes the payload
        -- may not keep the old one.** A plain COALESCE kept whatever was already there whenever
        -- the incoming write had no cost of its own, so two writers on one key produced a row
        -- carrying B's result and A's `compute_seconds` — measured: payload `{"who": "B"}`,
        -- 100.0 s from A. That number is what `durable/artifact_eviction.py` ranks by, so the
        -- mixed row misprices an eviction decision with a cost nothing on the row ever paid.
        -- Keeping it is right for a re-`put` of the *same* payload (a backfill, a structure_id
        -- fill-in), which is the case the COALESCE was written for; for a different payload with
        -- no stated cost, NULL is the honest answer.
        compute_seconds = CASE
            WHEN EXCLUDED.compute_seconds IS NOT NULL THEN EXCLUDED.compute_seconds
            WHEN EXCLUDED.result = calculation_results.result
                THEN calculation_results.compute_seconds
            ELSE NULL
        END,
        created_at = now()
"""

_SELECT = (
    "SELECT result, provenance, compute_seconds, structure_id, epoch "
    "FROM calculation_results WHERE key = %s"
)

# The browse query (`find`). Every filter is `%s IS NULL OR <column> = %s`-shaped so one prepared
# statement serves every combination — the alternative is assembling SQL from whichever filters
# were set, which is how a query builder starts. Ordered newest-first and capped by the caller,
# because an unbounded scan of the one table that is never evicted (D-011) is not a query.
_FIND = """
    SELECT key, calc_type, calc_version, input_hash, params_hash,
           result, provenance, compute_seconds, created_at, structure_id, epoch
      FROM calculation_results
     WHERE (%(calc_type)s::text IS NULL OR calc_type = %(calc_type)s)
       AND (%(calc_version)s::text IS NULL OR calc_version = %(calc_version)s)
       AND (%(input_hash)s::text IS NULL OR input_hash = %(input_hash)s)
       AND (%(structure_id)s::text IS NULL OR structure_id = %(structure_id)s)
       AND (%(since)s::timestamptz IS NULL OR created_at >= %(since)s)
       AND (%(until)s::timestamptz IS NULL OR created_at <= %(until)s)
     ORDER BY created_at DESC
     LIMIT %(limit)s
"""


class PostgresStore:
    """Durable `ResultStore` backed by Postgres.

    Opens a short-lived connection per call: calculations are coarse-grained and
    infrequent relative to their cost, so a connection pool would be premature
    complexity here (KISS). Introduce pooling only if store traffic proves it.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout.

        Pooled per process when the process opened a pool (`chemclaw.core.db.pooling`), so a
        request path pays no TCP+auth handshake; a dedicated connect otherwise. Either way a
        down or misconfigured database reports "Postgres unreachable at <host>" rather than a
        raw psycopg traceback, and a hung query is cancelled rather than pinning the enclosing
        activity for its whole budget.
        """
        async with db.connection(self._dsn) as conn:
            yield conn

    async def get(self, key: CalculationKey) -> StoredResult | None:
        """Return the stored result for `key`, or None on a miss.

        **The row as written, including any offloaded artifact's content address.** A payload whose
        by-product the eviction sweep has since reclaimed still comes back — deciding that such a
        row is a miss needs the artifact store, which this backend has no business holding, and
        `ArrayOffloadingStore.get` is the one place that contract lives (D-124). A caller that
        reads an offloaded family through this store directly gets addresses rather than arrays,
        which is what the row says and not a defect in it.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT, (key.as_str(),))
                row = await cur.fetchone()
        if row is None:
            return None
        result, provenance, compute_seconds, structure_id, epoch = row
        # JSONB comes back already parsed by psycopg; str only if driver differs.
        payload = result if isinstance(result, dict) else json.loads(result)
        return StoredResult(
            key=key,
            result=payload,
            provenance=provenance,
            compute_seconds=compute_seconds,
            structure_id=structure_id,
            epoch=epoch,
        )

    async def put(self, stored: StoredResult) -> None:
        """Persist `stored`, overwriting any existing result for its key."""
        key = stored.key
        async with self._connection() as conn:
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
                        stored.compute_seconds,
                        stored.structure_id,
                        stored.epoch,
                    ),
                )
            await conn.commit()

    async def known(self, keys: Sequence[str]) -> set[str]:
        """Which of `keys` the cache holds — the `kg.validate.CalculationExistence` answer.

        One indexed `= ANY` probe rather than a `get` per key, because `make kg-validate` asks
        for a whole corpus's `calc_refs` at once. Returns keys, not rows: existence is the whole
        question, and the payloads would be dead weight on a gate that only prints ids.

        The question is about the *calculation*, so a row whose by-product has been evicted still
        answers yes, and correctly: the note cites the calculation, the calculation ran, and its
        answer is on the row. What was reclaimed is an array a further calculation could have been
        seeded from, which `list_artifacts` reports the absence of by name.
        """
        if not keys:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT key FROM calculation_results WHERE key = ANY(%s)", (list(keys),)
                )
                rows = await cur.fetchall()
        return {row[0] for row in rows}

    async def calc_types(self) -> set[str]:
        """Every `calc_type` this store holds — the vocabulary a `calc_type` filter is judged on.

        A `DISTINCT` over the leading column of `calc_results_type_version_idx`, so it is an
        index-only scan rather than a table scan, and it is asked only when a filtered `find` came
        back empty — the path where the alternative is telling a model that nothing has ever been
        computed.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT DISTINCT calc_type FROM calculation_results")
                rows = await cur.fetchall()
        return {row[0] for row in rows}

    async def find(self, query: CalculationQuery) -> list[StoredResult]:
        """Return results matching `query`, newest first, capped at `query.limit`.

        A molecule filter is applied as an `input_hash` equality, never a scan: the hash is
        `stable_hash(canonical_smiles)` and is not reversible, so the query molecule is hashed the
        same way a key is built and compared. Canonicalisation happens here rather than at the
        caller so `CCO` and `OCC` find the same rows.
        """
        params = {
            "calc_type": query.calc_type,
            "calc_version": query.calc_version,
            "input_hash": None if query.smiles is None else molecule_hash(query.smiles),
            "structure_id": query.structure_id,
            "since": query.since,
            "until": query.until,
            "limit": query.limit,
        }
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_FIND, params)
                rows = await cur.fetchall()
        return [_stored_from_row(row) for row in rows]


def _stored_from_row(row: TupleRow) -> StoredResult:
    """Rebuild a `StoredResult` from a `find` row, key components included.

    `find` returns the key columns rather than parsing `key`, so a calculator version containing
    the separators the flat form uses cannot be split back wrongly — the flat string is an index
    key, not a serialization format.
    """
    _, calc_type, calc_version, input_hash, params_hash = row[:5]
    result, provenance, compute_seconds, created_at, structure_id, epoch = row[5:]
    return StoredResult(
        key=CalculationKey(
            calc_type=calc_type,
            calc_version=calc_version,
            input_hash=input_hash,
            params_hash=params_hash,
        ),
        # JSONB comes back already parsed by psycopg; str only if the driver differs.
        result=result if isinstance(result, dict) else json.loads(result),
        provenance=provenance,
        compute_seconds=compute_seconds,
        created_at=created_at,
        structure_id=structure_id,
        epoch=epoch,
    )


def default_store() -> ResultStore:
    """Return the production result store.

    The one place that names the production backend, so a tool module does not have to
    know which one it is. Every tool that needs a store imports this and tests swap it at
    the importing module (`monkeypatch.setattr(<module>, "default_store", ...)`) — it lives
    here rather than in one tool module because storage is not a calculator concept, and
    the BO featurizer needs the same seam as the calculators (Rule of Three: two callers
    plus the test seam, one definition).
    """
    return PostgresStore()
