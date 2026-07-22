"""Generic fingerprint store — Tanimoto search over any bit-fingerprinted record.

Shared by the molecule (ECFP4) and reaction (DRFP) capabilities: the record shape, the
Tanimoto ranking, the store interface, and both backends are domain-neutral — a record
is an id, a human label (a SMILES or reaction SMILES), and a bit fingerprint. Each domain
supplies only its own fingerprint function, its table, and its bit width. This is the
Rule-of-Three extraction: the second fingerprint domain (reactions) made the duplication
real, so the ranking lives in exactly one place (DRY), just like the calculation store.
"""

from typing import Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw import db
from chemclaw.config import settings
from chemclaw.errors import ChemclawError


class FingerprintError(ChemclawError):
    """A fingerprint could not be computed or two fingerprints are incomparable (G4)."""


def tanimoto(bits_a: str, bits_b: str) -> float:
    """Tanimoto (Jaccard) similarity of two equal-length fingerprint bitstrings.

    `intersection / union` of set bits; two all-zero fingerprints are defined as 0.0
    (no shared structure). Works on the stored bitstrings directly, so the in-memory
    backend ranks without the source cheminformatics library — the same ordering the
    Postgres backend produces in SQL. (The all-zero case is a guard: a fingerprint from a
    real molecule/reaction always sets at least one bit, where pgvector's Jaccard would
    otherwise return NaN and the two backends could differ.)
    """
    if len(bits_a) != len(bits_b):
        raise FingerprintError("cannot compare fingerprints of different widths")
    a, b = int(bits_a, 2), int(bits_b, 2)
    union = (a | b).bit_count()
    return (a & b).bit_count() / union if union else 0.0


class FingerprintRecord(BaseModel):
    """A stored entity: a stable id, its human label (SMILES/reaction SMILES), its bits.

    `definition` is the signature of the fingerprint parameters that produced `bits` (e.g.
    `ecfp:r2:b2048`, `drfp:b2048`). Bits of equal width but different definition (a changed
    Morgan radius) are the same length yet incomparable, which the width check cannot catch;
    carrying the definition lets the durable store refuse to rank across definitions. Defaults
    to empty for a record built without one (an ephemeral, single-definition index).
    """

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    bits: str = Field(min_length=1)
    definition: str = ""


class Match(BaseModel):
    """A structural-search hit: the entity and its Tanimoto similarity to the query."""

    id: str
    label: str
    similarity: float


@runtime_checkable
class FingerprintStore(Protocol):
    """Persistence + similarity-search contract. Backends implement this."""

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by id."""
        ...

    async def all_records(self) -> list[FingerprintRecord]:
        """Return every stored record (used for substructure scans)."""
        ...

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Return up to `top_k` records with Tanimoto >= `threshold`, most similar first."""
        ...


class InMemoryFingerprintStore:
    """Process-local `FingerprintStore` for tests and single-run use.

    Computes exact Tanimoto ranking without a database — the reference the Postgres
    backend matches (same threshold and tie-break, exactly for small corpora, up to HNSW
    recall for large ones). Keyed by record id, so re-adding an id replaces it.
    """

    def __init__(self, definition: str | None = None) -> None:
        """Start with an empty index.

        If `definition` is set, similarity search returns only records built under that
        same fingerprint definition — the durable store's cross-definition guard, made
        testable without a database. Left `None` it ranks every record, which is correct
        for an ephemeral index always populated in a single configuration (tests, demo).
        """
        self._records: dict[str, FingerprintRecord] = {}
        self._definition = definition

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by id."""
        self._records[record.id] = record

    async def all_records(self) -> list[FingerprintRecord]:
        """Return every stored record."""
        return list(self._records.values())

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Rank stored records by Tanimoto to `query_bits`, filtered and truncated.

        Records whose definition differs from this store's (when one is set) are excluded —
        their equal-width bits are not comparable. Ties break by id so the ordering is
        deterministic and matches a Postgres `ORDER BY similarity DESC, id`.
        """
        scored = [
            Match(id=r.id, label=r.label, similarity=tanimoto(query_bits, r.bits))
            for r in self._records.values()
            if self._definition is None or r.definition == self._definition
        ]
        hits = [m for m in scored if m.similarity >= threshold]
        hits.sort(key=lambda m: (-m.similarity, m.id))
        return hits[:top_k]


class PostgresFingerprintStore:
    """Durable `FingerprintStore` backed by Postgres + pgvector, over one table.

    Table and bit width are constructor parameters (both trusted internal constants), so
    the same class serves the molecule and reaction fingerprint tables. Similarity is
    Tanimoto (= 1 - Jaccard distance) in SQL, accelerated by the table's HNSW
    `bit_jaccard_ops` index; the ranking semantics match the in-memory backend up to HNSW
    recall. Note the threshold interaction: the `WHERE` filter applies *after* the ordered
    HNSW scan, so a selective threshold can return fewer than `top_k` rows even when that
    many qualify in the table (bounded by `hnsw.ef_search`) — approximate by design.
    A short-lived connection per call (KISS — the calc store's choice).
    """

    def __init__(self, table: str, width: int, definition: str, dsn: str | None = None) -> None:
        """Bind to `table` with fingerprint `width` and `definition`, on the configured DSN.

        `table` and `width` come from trusted domain constants, never user input, so
        interpolating them into the SQL is safe; the identifier check below enforces
        that trust boundary against any future caller. If `width` disagrees with the
        table's `bit(N)` column, Postgres raises a bit-length error (a loud failure,
        not a silent pad).

        `definition` is the current fingerprint-parameter signature (e.g. `ecfp:r2:b2048`).
        Every row records the definition it was indexed under; similarity search filters to
        this store's definition, so changing the definition and re-indexing alongside older
        rows can never silently rank incomparable (same-width, different-radius) bits — the
        stale rows simply fall out of search until they are re-indexed.
        """
        if not table.isidentifier():
            raise ValueError(f"table must be a plain SQL identifier, got {table!r}")
        self._table = table
        self._definition = definition
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        self._upsert = (
            f"INSERT INTO {table} (id, label, bits, definition) "
            f"VALUES (%(id)s, %(label)s, %(bits)s::bit({width}), %(definition)s) "
            f"ON CONFLICT (id) DO UPDATE SET "
            f"label = EXCLUDED.label, bits = EXCLUDED.bits, definition = EXCLUDED.definition"
        )
        self._all = f"SELECT id, label, bits::text, definition FROM {table}"
        # `<%%>` is pgvector's Jaccard-distance operator (`%` doubled to escape psycopg).
        # Threshold-filter first (and to this store's definition), then rank by distance and
        # truncate — the in-memory backend's "threshold then top-k"; ties break by id.
        self._similar = (
            f"SELECT id, label, 1 - (bits <%%> %(q)s::bit({width})) AS similarity "
            f"FROM {table} "
            f"WHERE definition = %(definition)s "
            f"AND 1 - (bits <%%> %(q)s::bit({width})) >= %(threshold)s "
            f"ORDER BY bits <%%> %(q)s::bit({width}), id "
            f"LIMIT %(k)s"
        )

    async def _connect(self) -> psycopg.AsyncConnection[TupleRow]:
        """Open a connection that fails fast, with a clear message, when unreachable.

        Delegates to the shared `chemclaw.db.connect` so a down/misconfigured database
        reports "Postgres unreachable at <host>" instead of a raw psycopg traceback (DRY
        with the calculation store). Applies the configured per-statement timeout too, so a
        slow HNSW similarity scan is cancelled rather than pinning its worker — the same bound
        every other store carries.
        """
        return await db.connect(
            self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        )

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by id."""
        async with await self._connect() as conn:
            await conn.execute(
                self._upsert,
                {
                    "id": record.id,
                    "label": record.label,
                    "bits": record.bits,
                    "definition": record.definition,
                },
            )
            await conn.commit()

    async def all_records(self) -> list[FingerprintRecord]:
        """Return every stored record (bits as a text bitstring), regardless of definition.

        Unfiltered on purpose: the only consumer is substructure search, which re-matches
        the stored SMILES label with RDKit and never touches the bits, so a stale-definition
        row is still a correct substructure hit.
        """
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._all)
                rows = await cur.fetchall()
        return [FingerprintRecord(id=r[0], label=r[1], bits=r[2], definition=r[3]) for r in rows]

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Return up to `top_k` records with Tanimoto >= `threshold`, most similar first."""
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                params = {
                    "q": query_bits,
                    "threshold": threshold,
                    "k": top_k,
                    "definition": self._definition,
                }
                await cur.execute(self._similar, params)
                rows = await cur.fetchall()
        return [Match(id=r[0], label=r[1], similarity=float(r[2])) for r in rows]


async def find_matches(
    store: FingerprintStore,
    query_bits: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[Match]:
    """Search a store with the configured `top_k`/`threshold` defaults applied.

    The one place the generic search knobs fall back to config, so the molecule
    and reaction entry points cannot drift in how they default (DRY).
    """
    return await store.find_similar(
        query_bits,
        top_k if top_k is not None else settings.fingerprint_top_k,
        threshold if threshold is not None else settings.fingerprint_similarity_threshold,
    )


def default_molecule_store() -> PostgresFingerprintStore:
    """The production molecule (ECFP4) store — one place pairs table, width, and definition."""
    from mcp_servers.molfp.fingerprint import molecule_definition

    return PostgresFingerprintStore(
        "molecule_fingerprints", settings.ecfp_bits, molecule_definition()
    )


def default_reaction_store() -> PostgresFingerprintStore:
    """The production reaction (DRFP) store — one place pairs table, width, and definition."""
    from mcp_servers.rxnfp.fingerprint import reaction_definition

    return PostgresFingerprintStore(
        "reaction_fingerprints", settings.drfp_bits, reaction_definition()
    )
