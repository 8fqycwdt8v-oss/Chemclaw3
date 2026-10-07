"""Postgres backend for the calculation store, over `calculation_results`.

`put` upserts by the flat calculation key; `get` is a primary-key lookup.
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
    CalculationPage,
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

# What a browse matches. Every filter is `%s IS NULL OR <column> = %s`, so one prepared statement
# serves every combination; shared by page and count so both describe the same rows.
_WHERE = """
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
"""

# The browse query (`find`). Ordered newest-first and capped by the caller, because an unbounded
# scan of the one table that is never evicted (D-011) is not a query.
_FIND = f"""
    SELECT key, calc_type, calc_version, input_hash, params_hash,
           result, provenance, compute_seconds, created_at, structure_id, epoch
      FROM calculation_results
{_WHERE}
     ORDER BY created_at DESC
     LIMIT %(limit)s
"""

# How many rows the same query matches, so a full page reads as "20 of 30". A separate statement
# rather than `count(*) OVER ()`, which would make the page pay for the total by defeating the
# `LIMIT`.
_COUNT = f"""
    SELECT count(*)
      FROM calculation_results
{_WHERE}
"""


class PostgresStore:
    """Durable `ResultStore` backed by Postgres.

    A short-lived connection per call; calculations are coarse-grained relative to their cost.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout.

        Pooled when the process opened a pool, a dedicated connect otherwise. An unreachable
        database reports "Postgres unreachable at <host>", and a hung query is cancelled.
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
        # `checked_payload` refuses a non-object jsonb top level by name rather than failing later
        # with an anonymous `TypeError`.
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

        `json_column` is a backstop for writers that bypass `cached_compute`'s `checked_payload`.
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

        One indexed `= ANY` probe for a whole corpus's `calc_refs`; returns keys only.
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

    async def find(self, query: CalculationQuery) -> CalculationPage:
        """Return results matching `query`, newest first, capped at `query.limit`.

        A molecule filter compares the canonical-SMILES `input_hash`; rows from another epoch are
        excluded, matching `store._matches`. The page carries `total_matched` and `unreadable`
        (dropped rows); the count may over-count, never under-count.
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
                await cur.execute(_COUNT, params)
                counted = await cur.fetchone()
        total = int(counted[0]) if counted is not None else len(rows)
        readable = [stored for row in rows if (stored := _readable_row(row)) is not None]
        return CalculationPage(
            readable,
            total_matched=total,
            truncated=total > query.limit,
            unreadable=len(rows) - len(readable),
        )


def _readable_row(row: TupleRow) -> StoredResult | None:
    """One `find` row, or `None` with a warning when its payload is not a result.

    Dropped only on this browse path, so one corrupt row cannot take down a listing; `get` by key
    still refuses it by name.
    """
    try:
        return _stored_from_row(row)
    except CorruptCacheRow as exc:
        logger.warning("skipping a calculation_results row in the browse: %s", exc)
        return None


def _stored_from_row(row: TupleRow) -> StoredResult:
    """Rebuild a `StoredResult` from a `find` row, key components included.

    Reads the key columns rather than parsing `key`, since a version may contain the flat form's
    separators.
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

    The one place that names the production backend; tests monkeypatch it at the importing module.
    """
    return PostgresStore()
