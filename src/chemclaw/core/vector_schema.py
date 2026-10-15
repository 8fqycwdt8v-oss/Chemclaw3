"""Which pgvector column holds an embedding of a given width, and whether the corpus agrees.

A `vector(N)` column has one width for life, so a model with another width gets a sibling column
(`embedding_<N>`) beside the original `embedding` (1536); `infra/sql/130_embedding_width.sql`
creates it from `settings.embedding_dim` when `core/migrate.py` runs. A row's `embedding_key`
(`core/embeddings.py::embedding_config_key`) names the provider, model and width that made its
vector, so it is also the per-row record this module decodes and checks.

Invariants: the column for a width is a pure function of the width; a row carries its live vector
in the column its key's width names and is read only when its key is the live one; nothing here
writes, so it is safe on any connection.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

from chemclaw.core.config import LEGACY_VECTOR_DIM
from chemclaw.core.embeddings import embedding_config_key, note_embedding_key

#: The tables whose rows carry a dense vector in Postgres.
VECTOR_TABLES = ("note_index", "document_chunks")

#: The name the original `vector(1536)` columns keep.
LEGACY_COLUMN = "embedding"

_KEY = re.compile(
    r"^(?P<provider>[^:]+):(?P<endpoint>[^:]+):d(?P<dim>\d+):(?P<model>.*)$", re.DOTALL
)


def live_embedding_keys() -> dict[str, str]:
    """The `embedding_key` a write to each vector table would stamp now."""
    return {"note_index": note_embedding_key(), "document_chunks": embedding_config_key()}


def embedding_column(dim: int) -> str:
    """The column storing `dim`-wide embeddings: `embedding` for 1536, else `embedding_<dim>`."""
    return LEGACY_COLUMN if dim == LEGACY_VECTOR_DIM else f"{LEGACY_COLUMN}_{dim}"


@dataclass(frozen=True)
class EmbeddingIdentity:
    """What a stored `embedding_key` says made the vector: provider, endpoint slot, width, model."""

    provider: str
    endpoint: str
    dim: int
    model: str

    def describe(self) -> str:
        """A short operator-readable spelling: `openai_compatible/text-embed d768`."""
        return f"{self.provider}/{self.model} d{self.dim}"


def parse_embedding_key(key: str | None) -> EmbeddingIdentity | None:
    """Decode a stored `embedding_key`; `None` for NULL or a key this format does not describe.

    Tolerates the suffixes a stored key may carry (`|ntv3` for notes, `@store:collection` for an
    external store) by leaving them on the model, since only the width is relied on.
    """
    if key is None:
        return None
    matched = _KEY.match(key)
    if matched is None:
        return None
    return EmbeddingIdentity(
        provider=matched["provider"],
        endpoint=matched["endpoint"],
        dim=int(matched["dim"]),
        model=matched["model"].split("|", 1)[0],
    )


def _describe_key(key: str | None) -> str:
    """One key as an operator reads it, tolerating NULL and a key from another format."""
    if key is None:
        return "(unrecorded)"
    identity = parse_embedding_key(key)
    return identity.describe() if identity is not None else key


@dataclass(frozen=True)
class TableVectorState:
    """One table's vector column for the live width, and the keys in it that are not the live one.

    `column_width` is `None` when the column does not exist (the migrator has not seen this width).
    `foreign_keys` holds every other distinct `embedding_key`, NULL included, as stored.
    """

    table: str
    column: str
    column_width: int | None
    foreign_keys: tuple[str | None, ...]

    def width_ok(self, dim: int) -> bool:
        """Whether the live column exists with exactly the configured width."""
        return self.column_width == dim

    @property
    def mixed(self) -> bool:
        """Whether some row was embedded by a configuration other than the live one."""
        return bool(self.foreign_keys)

    def describe_foreign(self) -> str:
        """The other configurations present, for a log line or a status."""
        return ", ".join(sorted(_describe_key(key) for key in self.foreign_keys)) or "none"


_COLUMN_WIDTH = sql.SQL(
    "SELECT a.atttypmod FROM pg_attribute a WHERE a.attrelid = to_regclass(%s) "
    "AND a.attname = %s AND a.attnum > 0 AND NOT a.attisdropped"
)

# Every embedding column a table carries, so a superseded width can be found and emptied.
_COLUMNS_OF = sql.SQL(
    "SELECT a.attname, a.atttypmod FROM pg_attribute a WHERE a.attrelid = to_regclass(%s) "
    "AND a.attname ~ '^embedding(_[0-9]+)?$' AND a.attnum > 0 AND NOT a.attisdropped"
)

# The distinct `embedding_key`s by walking the btree from key to next key, so the cost follows the
# number of configurations (one or two), never the number of rows. NULL sorts last, so it is asked
# for separately.
_DISTINCT_KEYS = sql.SQL(
    """
    WITH RECURSIVE keys AS (
        (SELECT embedding_key FROM {table} WHERE embedding_key IS NOT NULL
         ORDER BY embedding_key LIMIT 1)
        UNION ALL
        SELECT (SELECT embedding_key FROM {table}
                WHERE embedding_key > keys.embedding_key ORDER BY embedding_key LIMIT 1)
        FROM keys WHERE keys.embedding_key IS NOT NULL
    )
    SELECT embedding_key FROM keys WHERE embedding_key IS NOT NULL
    """
)

_ANY_NULL_KEY = sql.SQL("SELECT EXISTS (SELECT 1 FROM {table} WHERE embedding_key IS NULL)")


async def embedding_columns(conn: psycopg.AsyncConnection[Any], table: str) -> dict[str, int]:
    """Every embedding column of `table` (`name -> width`); empty when the table does not exist."""
    cursor = await conn.execute(_COLUMNS_OF, (table,))
    return {str(name): int(typmod) for name, typmod in await cursor.fetchall()}


async def column_width(conn: psycopg.AsyncConnection[Any], table: str, column: str) -> int | None:
    """The `N` of `table.column`'s `vector(N)`, or `None` when the column does not exist."""
    cursor = await conn.execute(_COLUMN_WIDTH, (table, column))
    row = await cursor.fetchone()
    return int(row[0]) if row is not None else None


async def recorded_keys(conn: psycopg.AsyncConnection[Any], table: str) -> tuple[str | None, ...]:
    """Every distinct `embedding_key` in `table`, NULL (a row written before keys existed) last."""
    identifier = sql.Identifier(table)
    cursor = await conn.execute(_DISTINCT_KEYS.format(table=identifier))
    keys: list[str | None] = [str(row[0]) for row in await cursor.fetchall()]
    cursor = await conn.execute(_ANY_NULL_KEY.format(table=identifier))
    row = await cursor.fetchone()
    if row is not None and row[0]:
        keys.append(None)
    return tuple(keys)


async def _table_exists(conn: psycopg.AsyncConnection[Any], table: str) -> bool:
    """Whether `table` resolves on this connection's `search_path`."""
    cursor = await conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    row = await cursor.fetchone()
    return bool(row and row[0])


async def _held_under(
    conn: psycopg.AsyncConnection[Any], table: str, key: str | None, chunkings: Collection[str]
) -> bool:
    """Whether any row of `table` cut under one of `chunkings` carries exactly this key.

    Asked per recorded key, through the key's index, so the cost follows the rows of the *other*
    configurations (the transient case) and not the table.
    """
    statement = sql.SQL(
        "SELECT EXISTS (SELECT 1 FROM {table} WHERE embedding_key "
        + ("IS NULL" if key is None else "= %(key)s")
        + " AND chunking_key = ANY(%(chunkings)s))"
    ).format(table=sql.Identifier(table))
    cursor = await conn.execute(statement, {"key": key, "chunkings": sorted(chunkings)})
    row = await cursor.fetchone()
    return bool(row and row[0])


async def inspect_tables(
    conn: psycopg.AsyncConnection[Any],
    dim: int,
    live_keys: Mapping[str, str],
    document_chunkings: Collection[str] | None = None,
) -> list[TableVectorState]:
    """Each vector table's live column width and the keys in it other than `live_keys[table]`.

    `live_keys` maps a table to the `embedding_key` a write there would stamp now; it is passed in
    because the note index adds its own text-derivation suffix, which this layer cannot know. A
    table this connection cannot see is left out: an unmigrated database is the ledger's to report.
    `document_chunkings`, when given, limits `document_chunks` to the cuttings the enabled shares
    use, which is the set a re-embed converges; a disabled share's rows keep their key until it is
    enabled again and are not "mixed".
    """
    column = embedding_column(dim)
    states = []
    for table in VECTOR_TABLES:
        if not await _table_exists(conn, table):
            continue
        live = live_keys[table]
        keys = [key for key in await recorded_keys(conn, table) if key != live]
        if table == "document_chunks" and document_chunkings is not None:
            keys = [key for key in keys if await _held_under(conn, table, key, document_chunkings)]
        states.append(
            TableVectorState(
                table=table,
                column=column,
                column_width=await column_width(conn, table, column),
                foreign_keys=tuple(keys),
            )
        )
    return states
