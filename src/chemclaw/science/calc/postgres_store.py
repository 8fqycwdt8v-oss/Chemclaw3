"""Postgres backend for the calculation store (plan step 1b.3).

Implements the same `ResultStore` interface as `InMemoryStore`, backed by the
`calculation_results` table (see `infra/sql/001_calculation_results.sql`), so
results survive process restarts and are shared across workers. A `put` is an
upsert keyed by the flat calculation key; a `get` is a single primary-key lookup.
The DSN comes from the one config source.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.jsonb import json_column
from chemclaw.science.calc.store import (
    CALCULATION_EPOCH,
    CalculationKey,
    CalculationQuery,
    CorruptCacheRow,
    ResultStore,
    StoredResult,
    checked_payload,
    molecule_hash,
)

logger = logging.getLogger(__name__)

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
        -- Keep the recorded cost when a rewrite does not carry one, so a backfill or a
        -- re-`put` of an existing payload cannot erase what the original miss measured.
        compute_seconds = COALESCE(EXCLUDED.compute_seconds, calculation_results.compute_seconds),
        -- Keep a recorded epoch when a rewrite does not carry one, by the same rule as the two
        -- above: `ArrayOffloadingStore`'s rewrite and a backfill re-`put` a row they did not
        -- compute, and blanking the epoch would move a known-current row back into the
        -- "unrecorded" class the browse has to hand back and mark.
        epoch = CASE
            WHEN EXCLUDED.epoch <> '' THEN EXCLUDED.epoch
            ELSE calculation_results.epoch
        END
        -- `created_at` is deliberately not in this list, by the same rule again: the key is
        -- content-addressed, so a second `put` under it is the *same* calculation being rewritten
        -- — a backfill, an `ArrayOffloadingStore` rewrite — and `created_at = now()` restamped it
        -- as newly computed. `find`'s `since`/`until` and its newest-first order then described
        -- the last write, while `find_calculations` promises "results computed at or after it"
        -- and D-163 gives the column as "when the value was computed". `InMemoryStore` never
        -- restamped, so the two backends disagreed as well.
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
       -- The epoch predicate `_matches` states in Python, expressed as SQL because this store
       -- filters before it fetches. Not a parameter of `CalculationQuery`: a row a later epoch
       -- invalidated is wrong rather than old, and `''` is a row written before migration 090,
       -- which is unclassifiable rather than wrong.
       AND (epoch = '' OR epoch = %(epoch)s)
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
        """Return the stored result for `key`, or None on a miss."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT, (key.as_str(),))
                row = await cur.fetchone()
        if row is None:
            return None
        result, provenance, compute_seconds, structure_id, epoch = row
        # `checked_payload` rather than the old `result if isinstance(result, dict) else
        # json.loads(result)`: that else-branch was written for a driver that hands back a string,
        # and psycopg parses jsonb *whatever* its top level is — so an array, a string, a number or
        # a `null` (all storable under `JSONB NOT NULL`) reached `json.loads` as a `list`/`int` and
        # produced `TypeError: the JSON object must be str, bytes or bytearray, not list`, which
        # names neither the table nor the row. No supported driver returns a string here; if one
        # ever does, this refuses it by name instead of guessing.
        return StoredResult(
            key=key,
            result=checked_payload(key, result),
            provenance=provenance,
            compute_seconds=compute_seconds,
            structure_id=structure_id,
            epoch=epoch,
        )

    async def put(self, stored: StoredResult) -> None:
        """Persist `stored`, overwriting any existing result for its key.

        **The payload goes through `json_column`, which is a backstop and not the check.**
        `checked_payload` is the check, and it runs one door earlier in `cached_compute`; this door
        is public and has writers that never pass through that one — `ArrayOffloadingStore`'s
        rewrite, a backfill, and whatever comes next, by the same argument
        `publish_stored_result` makes about being paired with `put` rather than with
        `cached_compute`. Measured, a `float("nan")` arriving here came back as
        `InvalidTextRepresentation: invalid input syntax for type json / DETAIL: Token "NaN" is
        invalid` — a server-side error naming a JSON token, no field and no caller. `json_column`
        makes it a `ValueError` raised in this process at the column holding the value
        (`chemclaw.core.jsonb`, which carries the argument and the other four paths it covers).
        """
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
                        json_column(stored.result),
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

    async def find(self, query: CalculationQuery) -> list[StoredResult]:
        """Return results matching `query`, newest first, capped at `query.limit`.

        A molecule filter is applied as an `input_hash` equality, never a scan: the hash is
        `stable_hash(canonical_smiles)` and is not reversible, so the query molecule is hashed the
        same way a key is built and compared. Canonicalisation happens here rather than at the
        caller so `CCO` and `OCC` find the same rows.

        A row whose recorded `epoch` is neither the current one nor `''` is excluded, matching
        `store._matches`. The two are separate code for the reason every other filter here is —
        this one must run in SQL because it filters before it fetches — and
        `tests/test_postgres_store.py` pins them agreeing, which is the only thing that keeps a
        predicate stated twice from drifting.
        """
        params = {
            "calc_type": query.calc_type,
            "calc_version": query.calc_version,
            "input_hash": None if query.smiles is None else molecule_hash(query.smiles),
            "structure_id": query.structure_id,
            "since": query.since,
            "until": query.until,
            "epoch": CALCULATION_EPOCH,
            "limit": query.limit,
        }
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_FIND, params)
                rows = await cur.fetchall()
        return [stored for row in rows if (stored := _readable_row(row)) is not None]


def _readable_row(row: TupleRow) -> StoredResult | None:
    """One `find` row, or `None` with a warning when its payload is not a result.

    **Dropped rather than raised, and only on this path.** `get` addresses one key and must refuse
    a corrupt row by name — the caller asked for that row and would otherwise be handed a wrong
    answer. `find` is a browse ("what do we already have on this molecule"), and one poisoned row
    taking the whole listing down with a `TypeError` is what it did before: measured, seven rows
    of which one held a jsonb string answered zero. That is the same call
    `retrievers._chunks_from_hits` makes when an index hit's note no longer loads. The row is not
    hidden — it is logged here, and asking for it by key still refuses by name.
    """
    try:
        return _stored_from_row(row)
    except CorruptCacheRow as exc:
        logger.warning("skipping a calculation_results row in the browse: %s", exc)
        return None


def _stored_from_row(row: TupleRow) -> StoredResult:
    """Rebuild a `StoredResult` from a `find` row, key components included.

    `find` returns the key columns rather than parsing `key`, so a calculator version containing
    the separators the flat form uses cannot be split back wrongly — the flat string is an index
    key, not a serialization format.
    """
    _, calc_type, calc_version, input_hash, params_hash = row[:5]
    result, provenance, compute_seconds, created_at, structure_id, epoch = row[5:]
    stored_key = CalculationKey(
        calc_type=calc_type,
        calc_version=calc_version,
        input_hash=input_hash,
        params_hash=params_hash,
    )
    return StoredResult(
        key=stored_key,
        result=checked_payload(stored_key, result),
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
