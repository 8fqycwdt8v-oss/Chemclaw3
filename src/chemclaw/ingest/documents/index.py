"""Where the chunked share lives: content-addressed documents, path-addressed files.

Two backends behind one `DocumentIndex`, exactly as `retrieval/vector_index.py` does it —
`InMemoryDocumentIndex` computes the ranking in Python (the reference the tests use, no database)
and `PostgresDocumentIndex` persists to `document_files` / `document_chunks` (`infra/sql/037`) and
ranks in SQL.

**Why two tables.** A classical share is full of the same document in four project folders. Keying
the chunks by `doc_id` — the stable hash of the parsed text — and the files by path means those
four copies share one set of chunks and one embedding call. On a TB share that is not an
optimization, it is the difference between an affordable corpus and an unaffordable one. It is also
the rule `chemclaw.cli.backfill_corpus` already follows ("the id is derived from the content, not
the filename"), so a renamed or moved file costs nothing either.

**A hit is cited by path, not by hash.** `doc-9f2a...` is not something a chemist can open, so the
search resolves each hit back to a file path. When several paths hold the same content the smallest
one is cited deterministically — an arbitrary choice, but a stable one, which is what a citation
needs.
"""

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings


class FileRecord(BaseModel):
    """One path on the share, and the document its bytes parsed to."""

    path: str = Field(min_length=1)
    source: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    # "mtime_ns:size" — what makes the next crawl able to skip this file without reading it.
    fingerprint: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    modified_at: datetime | None = None
    # When this run saw the file — the mark half of `prune_stale`'s mark-and-sweep. The Postgres
    # backend stamps its `indexed_at` column server-side with `now()` and ignores this value, so
    # the sweep compares one clock (the database's) rather than the worker's against it; the
    # in-memory backend has only this one.
    indexed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChunkRecord(BaseModel):
    """One retrievable piece of a document, with its embedding and its structural coordinate."""

    doc_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    content: str = Field(min_length=1)
    coordinate: str = ""
    embedding: list[float]


class DocumentFilter(BaseModel):
    """The dimensions a question may narrow a share to. Empty means the whole corpus."""

    # A tag from the binding: a root's own tags, or the project code lifted out of the path.
    tag: str = ""
    # Bounded by the file's modification time — the only date a file share reliably carries.
    since: datetime | None = None
    until: datetime | None = None


class DocumentHit(BaseModel):
    """A ranked chunk, resolved back to a path a reader can actually open."""

    doc_id: str
    ordinal: int
    content: str
    coordinate: str
    path: str
    # Bounded here rather than trusted, because this is where two backends with different scoring
    # meet one `EvidenceChunk` contract that requires [0, 1]. The in-memory reference counted
    # shared tokens and produced 2.0, which a caller only found out about as a validation error
    # three layers up; a cosine and a `ts_rank` happen to be in range and hid it.
    score: float = Field(ge=0.0, le=1.0)


@runtime_checkable
class DocumentIndex(Protocol):
    """Persistence + dense/lexical search over one or more mounted shares."""

    async def fingerprints(self, source: str, paths: list[str]) -> dict[str, str]:
        """The stored `path -> fingerprint` for these paths of `source`.

        What the sync diffs the current filesystem stat against to decide which files must be
        re-read. A path with no entry reads as "changed", exactly like a real mismatch. Scoped to
        the paths one bounded chunk actually crawled, because on a 500k-file share the unscoped
        answer is a dictionary nobody needs and every chunk would rebuild it.
        """
        ...

    async def known_documents(self, doc_ids: set[str]) -> set[str]:
        """Which of these document ids already have chunks — the copies that need no embedding."""
        ...

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord]) -> None:
        """Insert or replace file rows by path and chunk rows by `(doc_id, ordinal)`."""
        ...

    async def touch(self, source: str, paths: list[str]) -> None:
        """Mark these already-current paths as seen by this run, without re-reading them.

        The mark half of the mark-and-sweep `prune_stale` completes. It is one statement per
        crawl chunk rather than a fingerprint dictionary held across a whole drain, which is what
        keeps the sweep affordable on a share far larger than memory.
        """
        ...

    async def prune_stale(self, source: str, before: datetime) -> int:
        """Delete `source` rows not seen since `before`, and any chunk left with no file.

        The sweep half. **Only ever called after a complete crawl with no failed roots** — see the
        prune-safety rule in `sync.py`, because an unmounted share presents as an empty one and
        would otherwise sweep the entire corpus.
        """
        ...

    async def clock(self) -> datetime:
        """This backend's own current time — the reference a later `prune_stale` is measured from.

        The mark is written with the backend's clock (`now()` in Postgres) and the sweep compares
        against it, so both sides must come from the same clock. Taking the run's start time from
        the worker instead would make the sweep depend on worker-versus-database skew: a database
        running a minute behind would leave freshly-marked rows looking older than the run that
        marked them, and the sweep would delete files nobody touched.
        """
        ...

    async def search_dense(
        self, source: str, query_embedding: list[float], top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Return up to `top_k` chunks most cosine-similar to `query_embedding`, best first."""
        ...

    async def search_lexical(
        self, source: str, query: str, top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Return up to `top_k` chunks best matching the terms in `query`, best first."""
        ...


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens — the offline proxy of Postgres `to_tsvector`."""
    return set("".join(c if c.isalnum() else " " for c in text.lower()).split())


class InMemoryDocumentIndex:
    """Process-local `DocumentIndex` for tests and single-run use (the reference ranking).

    Dense search is exact cosine — the ordering `PostgresDocumentIndex` produces with pgvector's
    `<=>` (up to HNSW recall). Lexical search is a shared-token count, a deterministic proxy of
    `ts_rank`: the intent (more shared terms rank higher) matches, the exact scores do not.
    """

    def __init__(self) -> None:
        """Start empty; files keyed by `(source, path)`, chunks by `(doc_id, ordinal)`."""
        # `(source, path)` mirrors the table's primary key: `Projects/report.pdf` is not an
        # unusual name, so two shares can carry it and a path-only key lets one evict the other.
        self._files: dict[tuple[str, str], FileRecord] = {}
        self._chunks: dict[tuple[str, int], ChunkRecord] = {}

    async def fingerprints(self, source: str, paths: list[str]) -> dict[str, str]:
        """Stored fingerprints for these paths of one source."""
        wanted = set(paths)
        return {
            f.path: f.fingerprint
            for f in self._files.values()
            if f.source == source and f.path in wanted
        }

    async def known_documents(self, doc_ids: set[str]) -> set[str]:
        """Which of these documents already have at least one chunk stored."""
        stored = {doc_id for doc_id, _ in self._chunks}
        return doc_ids & stored

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord]) -> None:
        """Replace each file by path and each chunk by `(doc_id, ordinal)`."""
        for file in files:
            self._files[(file.source, file.path)] = file
        for chunk in chunks:
            self._chunks[(chunk.doc_id, chunk.ordinal)] = chunk

    async def touch(self, source: str, paths: list[str]) -> None:
        """Restamp these paths as seen now, so the sweep does not take them."""
        now = datetime.now(UTC)
        for path in paths:
            file = self._files.get((source, path))
            if file is not None:
                self._files[(source, path)] = file.model_copy(update={"indexed_at": now})

    async def clock(self) -> datetime:
        """The process clock — the same one `touch` stamps with."""
        return datetime.now(UTC)

    async def prune_stale(self, source: str, before: datetime) -> int:
        """Drop this source's rows unseen since `before`, then any chunk left with no file."""
        stale = [
            key
            for key, file in self._files.items()
            if key[0] == source and file.indexed_at < before
        ]
        for key in stale:
            del self._files[key]
        # Orphans across every source, not just this one's documents: identical content reachable
        # through a copy on another share must stay indexed (the SQL `NOT EXISTS` says the same).
        live = {f.doc_id for f in self._files.values()}
        for chunk_key in [key for key in self._chunks if key[0] not in live]:
            del self._chunks[chunk_key]
        return len(stale)

    def _citation(self, doc_id: str, source: str, filters: DocumentFilter) -> str:
        """The smallest path in `source` holding this document and matching `filters`, or `""`."""
        candidates = sorted(
            f.path
            for f in self._files.values()
            if f.doc_id == doc_id and f.source == source and _matches(f, filters)
        )
        return candidates[0] if candidates else ""

    def _rank(
        self, source: str, filters: DocumentFilter, scored: list[tuple[ChunkRecord, float]], k: int
    ) -> list[DocumentHit]:
        """Resolve each scored chunk to a citation path, drop the unresolvable, take the best k."""
        hits: list[DocumentHit] = []
        for chunk, score in scored:
            if score <= 0.0:
                continue
            path = self._citation(chunk.doc_id, source, filters)
            if not path:
                continue
            hits.append(
                DocumentHit(
                    doc_id=chunk.doc_id,
                    ordinal=chunk.ordinal,
                    content=chunk.content,
                    coordinate=chunk.coordinate,
                    path=path,
                    score=score,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.doc_id, hit.ordinal))
        return hits[:k]

    async def search_dense(
        self, source: str, query_embedding: list[float], top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Rank chunks by cosine similarity to the query; drop zero similarity."""
        scored = [(c, _cosine(query_embedding, c.embedding)) for c in self._chunks.values()]
        return self._rank(source, filters, scored, top_k)

    async def search_lexical(
        self, source: str, query: str, top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Rank chunks by shared-token count with the query; drop non-matches."""
        wanted = _tokens(query)
        if not wanted:
            return []
        # The *fraction* of the query's terms this chunk carries, not the raw count: a score is
        # contractually in [0, 1], and a count is only a ranking within one query length.
        scored = [
            (chunk, len(wanted & _tokens(chunk.content)) / len(wanted))
            for chunk in self._chunks.values()
        ]
        return self._rank(source, filters, scored, top_k)


def _matches(file: FileRecord, filters: DocumentFilter) -> bool:
    """Whether one file row satisfies the query's filters (the in-memory mirror of the SQL)."""
    if filters.tag and filters.tag not in file.tags:
        return False
    if filters.since is not None and (file.modified_at is None or file.modified_at < filters.since):
        return False
    if filters.until is not None and (file.modified_at is None or file.modified_at > filters.until):
        return False
    return True


def _vector_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal (`[a,b,c]`), cast `::vector(N)` in SQL."""
    return "[" + ",".join(str(component) for component in embedding) + "]"


# The file-row predicate both searches share: a chunk is eligible when at least one path in this
# source holds it and satisfies the filters. `EXISTS` rather than a join, so a document copied into
# four folders contributes one row rather than four competing for the same top-k slots.
_ELIGIBLE = (
    "EXISTS (SELECT 1 FROM document_files f WHERE f.doc_id = c.doc_id AND f.source = %(src)s "
    "AND (%(tag)s::text IS NULL OR %(tag)s = ANY(f.tags)) "
    "AND (%(since)s::timestamptz IS NULL OR f.modified_at >= %(since)s) "
    "AND (%(until)s::timestamptz IS NULL OR f.modified_at <= %(until)s)) "
)
# The citation, resolved in the same statement: the smallest matching path. Deterministic, so a
# repeated question cites the same file rather than alternating between copies.
_CITATION = (
    "(SELECT min(f.path) FROM document_files f WHERE f.doc_id = c.doc_id AND f.source = %(src)s "
    "AND (%(tag)s::text IS NULL OR %(tag)s = ANY(f.tags)) "
    "AND (%(since)s::timestamptz IS NULL OR f.modified_at >= %(since)s) "
    "AND (%(until)s::timestamptz IS NULL OR f.modified_at <= %(until)s)) AS path "
)


class PostgresDocumentIndex:
    """Durable `DocumentIndex` over `document_files` + `document_chunks` (`infra/sql/037`).

    Dense search is cosine distance (`<=>`) accelerated by the HNSW `vector_cosine_ops` index;
    lexical search is `ts_rank` over the GIN-indexed `tsvector`. The embedding width is
    `settings.embedding_dim`, which must equal the table's `vector(N)` column — a mismatch makes
    Postgres raise on insert, which is the loud failure we want.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Bind to the configured DSN and the configured embedding width."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        width = settings.embedding_dim
        self._upsert_file = (
            "INSERT INTO document_files "
            "(path, source, doc_id, fingerprint, tags, modified_at, indexed_at) "
            "VALUES (%(path)s, %(src)s, %(doc)s, %(fp)s, %(tags)s, %(mtime)s, now()) "
            "ON CONFLICT (source, path) DO UPDATE SET "
            "doc_id = EXCLUDED.doc_id, "
            "fingerprint = EXCLUDED.fingerprint, tags = EXCLUDED.tags, "
            "modified_at = EXCLUDED.modified_at, indexed_at = now()"
        )
        self._upsert_chunk = (
            "INSERT INTO document_chunks (doc_id, ordinal, content, coordinate, embedding, lexeme) "
            f"VALUES (%(doc)s, %(ord)s, %(content)s, %(coord)s, %(emb)s::vector({width}), "
            "to_tsvector('english', %(content)s)) "
            "ON CONFLICT (doc_id, ordinal) DO UPDATE SET "
            "content = EXCLUDED.content, coordinate = EXCLUDED.coordinate, "
            "embedding = EXCLUDED.embedding, lexeme = EXCLUDED.lexeme"
        )
        # The `> 0` floor mirrors the in-memory reference: a zero or negatively-correlated chunk is
        # not a hit. Without it pgvector returns the top-k nearest unconditionally, so a narrow
        # corpus would surface unrelated documents as cited evidence.
        self._dense = (
            "SELECT c.doc_id, c.ordinal, c.content, c.coordinate, "
            f"1 - (c.embedding <=> %(q)s::vector({width})) AS score, {_CITATION}"
            "FROM document_chunks c WHERE c.embedding IS NOT NULL "
            f"AND 1 - (c.embedding <=> %(q)s::vector({width})) > 0 AND {_ELIGIBLE}"
            f"ORDER BY c.embedding <=> %(q)s::vector({width}), c.doc_id, c.ordinal LIMIT %(k)s"
        )
        self._lexical = (
            "SELECT c.doc_id, c.ordinal, c.content, c.coordinate, "
            f"ts_rank(c.lexeme, query) AS score, {_CITATION}"
            "FROM document_chunks c, websearch_to_tsquery('english', %(q)s) AS query "
            f"WHERE c.lexeme @@ query AND {_ELIGIBLE}"
            "ORDER BY score DESC, c.doc_id, c.ordinal LIMIT %(k)s"
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout (pooled where opened)."""
        async with db.connection(
            self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        ) as conn:
            yield conn

    async def fingerprints(self, source: str, paths: list[str]) -> dict[str, str]:
        """The stat signature each of these paths was last read at, for the ones on record.

        Scoped to the crawl chunk's own paths rather than to the whole source: the unscoped query
        on a 500k-file share returns a dictionary the caller has no use for and would rebuild on
        every chunk of the drain.
        """
        if not paths:
            return {}
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT path, fingerprint FROM document_files "
                    "WHERE source = %s AND path = ANY(%s)",
                    (source, sorted(paths)),
                )
                rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}

    async def known_documents(self, doc_ids: set[str]) -> set[str]:
        """Which of these documents already have chunks — asked before paying to embed one."""
        if not doc_ids:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT doc_id FROM document_chunks WHERE doc_id = ANY(%s)",
                    (sorted(doc_ids),),
                )
                rows = await cur.fetchall()
        return {row[0] for row in rows}

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord]) -> None:
        """Write the chunks first, then the file rows, in one transaction.

        Order matters on a crash: a file row whose chunks are missing would be skipped by the next
        crawl (its fingerprint matches) and would contribute nothing forever. Chunks with no file
        row are merely invisible until the file row lands.
        """
        if not files and not chunks:
            return
        async with self._connection() as conn:
            for chunk in chunks:
                await conn.execute(
                    self._upsert_chunk,
                    {
                        "doc": chunk.doc_id,
                        "ord": chunk.ordinal,
                        "content": chunk.content,
                        "coord": chunk.coordinate,
                        "emb": _vector_literal(chunk.embedding),
                    },
                )
            for file in files:
                await conn.execute(
                    self._upsert_file,
                    {
                        "path": file.path,
                        "src": file.source,
                        "doc": file.doc_id,
                        "fp": file.fingerprint,
                        "tags": list(file.tags),
                        "mtime": file.modified_at,
                    },
                )
            await conn.commit()

    async def touch(self, source: str, paths: list[str]) -> None:
        """Restamp these unchanged paths as seen now — one statement, however many paths."""
        if not paths:
            return
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE document_files SET indexed_at = now() WHERE source = %s AND path = ANY(%s)",
                (source, sorted(paths)),
            )
            await conn.commit()

    async def clock(self) -> datetime:
        """The database's `now()` — the clock `indexed_at` carries, so the one to compare it to."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT now()")
                row = await cur.fetchone()
        if row is None:  # pragma: no cover - `SELECT now()` always returns a row
            raise RuntimeError("database returned no clock reading")
        moment: datetime = row[0]
        return moment

    async def prune_stale(self, source: str, before: datetime) -> int:
        """Delete this source's rows unseen since `before`, then any chunk no file points at."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM document_files WHERE source = %s AND indexed_at < %s",
                    (source, before),
                )
                removed = cur.rowcount
                # Orphans, not "chunks of the deleted documents": the same content may still be
                # reachable through a copy elsewhere on the share, and deleting by `doc_id` would
                # silently un-index a file nobody touched.
                await cur.execute(
                    "DELETE FROM document_chunks c WHERE NOT EXISTS "
                    "(SELECT 1 FROM document_files f WHERE f.doc_id = c.doc_id)"
                )
            await conn.commit()
        return removed

    def _params(self, source: str, top_k: int, filters: DocumentFilter) -> dict[str, object]:
        """The filter parameters both statements bind (NULL meaning unrestricted)."""
        return {
            "src": source,
            "k": top_k,
            "tag": filters.tag or None,
            "since": filters.since,
            "until": filters.until,
        }

    async def _run(self, statement: str, params: dict[str, object]) -> list[DocumentHit]:
        """Execute a ranked search and build hits, dropping any whose citation resolved to NULL."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(statement, params)
                rows = await cur.fetchall()
        return [
            DocumentHit(
                doc_id=row[0],
                ordinal=row[1],
                content=row[2],
                coordinate=row[3],
                # Clamped: cosine similarity is already in range, but `ts_rank` sums per-term
                # weights and is only *usually* below 1. A score is a ranking within this source,
                # so clipping the rare outlier changes no order and keeps the DTO's contract.
                score=min(1.0, max(0.0, float(row[4]))),
                path=row[5],
            )
            for row in rows
            if row[5]
        ]

    async def search_dense(
        self, source: str, query_embedding: list[float], top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Rank chunks by cosine similarity to `query_embedding` (pgvector HNSW), positive only."""
        # A zero query vector has cosine 0 to everything, and `<=>` would produce a NaN distance to
        # order by — short-circuit exactly as the note index does.
        if not any(query_embedding):
            return []
        params = self._params(source, top_k, filters)
        params["q"] = _vector_literal(query_embedding)
        return await self._run(self._dense, params)

    async def search_lexical(
        self, source: str, query: str, top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Rank chunks by full-text `ts_rank` against the terms in `query`."""
        params = self._params(source, top_k, filters)
        params["q"] = query
        return await self._run(self._lexical, params)


def default_document_index() -> PostgresDocumentIndex:
    """The production document index — one place the retriever and the sync get their backend."""
    return PostgresDocumentIndex()
