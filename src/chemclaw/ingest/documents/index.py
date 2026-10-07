"""Where the chunked share lives: content-addressed documents, path-addressed files.

`InMemoryDocumentIndex` is the Python reference ranking (a test oracle); `PostgresDocumentIndex`
persists to `document_files` / `document_chunks` and ranks in SQL.

Files are keyed by path and chunks by `doc_id` (hash of the parsed text), so copies of one document
across folders share one set of chunks and one embedding call, and a moved file costs nothing. A
chunk's identity also includes its `chunking_key`, so two shares cutting one document differently
coexist; a cutting no file row claims is an orphan and is swept.

A hit is cited by path, not hash: when several paths hold the same content, the smallest is cited,
deterministically.
"""

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import SCHEMA_VECTOR_DIM, settings
from chemclaw.core.embeddings import embedding_config_key
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.fulltext import (
    TSQUERY_TERMS,
    normalize_search_text,
    reference_terms,
    reference_tokens,
)
from chemclaw.ingest.documents.binding import DocumentShareError
from chemclaw.ingest.documents.chunk import Chunk


class DocumentIndexError(SubsystemUnavailableError):
    """The document index could not be reached, so the search never ran.

    A `SubsystemUnavailableError`, not a `ChemclawError` (non-retryable bad data): a timeout says
    nothing about the query, and the call succeeds once the database is back. The message carries no
    hostnames or driver text; the `psycopg.Error` is the `__cause__`.
    """


class FileRecord(BaseModel):
    """One path on the share, and the document its bytes parsed to."""

    path: str = Field(min_length=1)
    source: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    # "mtime_ns:size" — what makes the next crawl able to skip this file without reading it.
    fingerprint: str = Field(min_length=1)
    # The chunking this path's content was cut under (`DocumentShareBinding.chunking_key`). It is
    # what makes a chunk set *claimed*: the sweep keeps exactly the cuttings some file row names.
    chunking_key: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    modified_at: datetime | None = None
    # When this run saw the file — the mark half of `prune_stale`'s mark-and-sweep. Postgres stamps
    # `indexed_at` server-side with `now()` and ignores this, so the sweep compares against one
    # clock; the in-memory backend uses this value.
    indexed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChunkRecord(BaseModel):
    """One retrievable piece of a document, with its embedding and its structural coordinate.

    `(doc_id, chunking_key, ordinal)` is the whole identity — the text it came from, the boundaries
    that cut it, and where it sits in that cutting. The chunking travels on the record rather than
    beside the call because it is part of *which row this is*, not a property of the write.
    """

    doc_id: str = Field(min_length=1)
    chunking_key: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    content: str = Field(min_length=1)
    coordinate: str = ""
    embedding: list[float]


class StaleChunk(BaseModel):
    """A stored chunk whose vector was made by a configuration that is no longer current.

    Carries its `content`, which is why re-embedding never touches the file share: the text was
    kept beside the vector, so a model swap is a database-to-database operation. And its
    `chunking_key`, because that is part of the row's identity and re-embedding has to address the
    row it read — without it a re-embed of one share's cutting would overwrite another's.
    """

    doc_id: str
    chunking_key: str
    ordinal: int
    content: str


class StoredDocument(BaseModel):
    """One document as the tables hold it: the path it is cited as, and its pieces in order.

    The read-model `upsert` writes and nothing read until now. Separate from `DocumentText`
    because they are different things — this is rows, that is reassembled text — and because only
    the caller knows the cutting's overlap, so the join cannot happen down here.
    """

    doc_id: str = Field(min_length=1)
    # `CITATION_SQL`'s rule, the smallest matching path, so a whole-document read cites the same
    # file a chunk hit from that document cites rather than a different copy of it.
    path: str = Field(min_length=1)
    pieces: list[Chunk] = Field(default_factory=list)
    modified_at: datetime | None = None
    # Whether the backend stopped before the document ended; the source for
    # `DocumentText.truncated`.
    truncated: bool = False

    model_config = {"arbitrary_types_allowed": True}


def _within_chars(pieces: list[Chunk], max_chars: int) -> tuple[list[Chunk], bool]:
    """Keep pieces until their cumulative length reaches `max_chars`, plus the one that crosses it.

    The in-memory mirror of the Postgres window, so both backends cut at the same piece. The
    crossing piece is kept so a document ending exactly at the ceiling is not reported truncated.
    """
    kept: list[Chunk] = []
    spent = 0
    for index, piece in enumerate(pieces):
        kept.append(piece)
        spent += len(piece.content)
        if spent >= max_chars:
            return kept, index + 1 < len(pieces)
    return kept, False


class DocumentText(BaseModel):
    """A whole document, reassembled from its stored chunks, saying what that is and is not.

    **Not the file's bytes.** `chunk_document` strips each piece, drops empty ones, joins blocks
    with a blank line and hoists `[page 3]` out of the body into a coordinate — none of it
    recoverable — so this is the text *as the crawl parsed and indexed it*. That is also the text
    every citation a turn is holding points into, which is the property that matters: a reader
    checking a quotation checks it against what was actually retrieved.

    `truncated` is carried rather than implied. A document over the read ceiling comes back short,
    and a shortened document that does not say so reads as a complete one — the rule
    `FingerprintSearch.verdict` and `EvidenceChunk.conflicts_total` already follow.
    """

    doc_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    # The smallest path holding this document, by `CITATION_SQL`'s rule — so a whole-document read
    # cites the same file a chunk hit from it cites, rather than picking a different copy.
    path: str = Field(min_length=1)
    text: str
    chunks: int = Field(ge=0)
    truncated: bool = False
    coordinates: list[str] = Field(default_factory=list)
    modified_at: datetime | None = None


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
    # Bounded here because this is where backends with different scoring meet the `EvidenceChunk`
    # contract, which requires [0, 1].
    score: float = Field(ge=0.0, le=1.0)


@runtime_checkable
class DocumentIndex(Protocol):
    """Persistence + dense/lexical search over one or more mounted shares."""

    async def fingerprints(
        self, source: str, paths: list[str], chunking_key: str
    ) -> dict[str, str]:
        """The stored `path -> fingerprint` for these paths of `source`, chunked as `chunking_key`.

        The sync diffs the current stat against this to decide what to re-read; a missing path reads
        as changed. Scoped to one crawl chunk's paths, and to the chunking, because a chunk-size
        change does not move `mtime_ns:size` and must still force a re-cut.
        """
        ...

    async def known_documents(self, doc_ids: set[str], key: str, chunking_key: str) -> set[str]:
        """Which of these documents have **at least one** chunk under both configurations.

        Keyed on the embedding and chunking configuration, so a changed model or chunk size forces a
        re-read even for unchanged content. "At least one" rather than "all": remaining stale chunks
        are `stale_chunks`' job, and `DocumentSyncWorkflow` drains before it crawls; per-document
        completeness would re-embed a whole file to fix one chunk.
        """
        ...

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord], key: str) -> None:
        """Insert or replace file rows by path and chunk rows by `(doc_id, chunking_key, ordinal)`.

        `key` is the embedding configuration (`embedding_config_key`) stored with each chunk. After
        the file rows land, every cutting of the written documents that no file row claims is
        deleted in the same write, so a re-chunk leaves nothing behind for `reembed_stale` to adopt.
        Only unclaimed cuttings: another share may hold the same document at its own chunk size.
        """
        ...

    async def stale_chunks(self, key: str, limit: int, chunkings: set[str]) -> list[StaleChunk]:
        """Up to `limit` chunks cut by one of `chunkings` whose vector was not made by `key`.

        NULL counts as stale: unknown must never read as current. `chunkings` is what the enabled
        shares currently use; a row under any other chunking will be re-cut and re-embedded by the
        crawl, so re-embedding it here would be wasted work.
        """
        ...

    async def store_embeddings(self, chunks: list[ChunkRecord], key: str) -> None:
        """Replace the vector and key of existing chunks, leaving content and coordinate alone."""
        ...

    async def stored_document(
        self, source: str, doc_id: str, chunking_key: str, max_chars: int
    ) -> StoredDocument | None:
        """This document as stored under one cutting, or `None` when this share does not hold it.

        `max_chars` bounds the read at the fetch, not after assembly: pieces up to the cumulative
        cap plus the one crossing it, with `truncated` saying whether more existed. These rows are
        the only stored copy of the text. Scoped by `source` and gated on the same eligibility a
        search uses, so a caller cannot read a document from a share it may not search. Pieces carry
        no vector.
        """
        ...

    async def touch(self, source: str, paths: list[str]) -> None:
        """Mark these already-current paths as seen by this run, without re-reading them.

        The mark half of `prune_stale`'s mark-and-sweep, one statement per crawl chunk so the sweep
        scales past memory.
        """
        ...

    async def prune_stale(self, source: str, before: datetime) -> int:
        """Delete `source` rows not seen since `before`, and any chunk set no file row claims.

        Only ever called after a complete crawl with no failed roots: an unmounted share looks empty
        and would otherwise sweep the whole corpus.
        """
        ...

    async def clock(self) -> datetime:
        """This backend's own current time — the reference a later `prune_stale` is measured from.

        The mark uses the backend's clock, so the sweep must too; the worker's clock would make the
        sweep depend on clock skew and could delete freshly marked rows.
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
        """Return up to `top_k` chunks best matching the terms in `query`, best first.

        The boolean rule `chemclaw.core.fulltext` states, in both backends: a chunk matching every
        term outranks one matching some, a partial match is still a hit, and a chunk carrying a
        `-excluded` term is not a hit.
        """
        ...


def require_schema_vector_width() -> None:
    """Refuse a deployment whose `embedding_dim` cannot fit the column it would write.

    Not in the config validator: a share's name is the deployment's choice, so config cannot tell
    whether one is enabled, and `chemclaw.core` may not import this package. The guard sits on the
    constructors instead, covering the first query, the first crawl and `validate_datasources
    --construct`; it fires at first use rather than at startup.

    Raises:
        DocumentShareError: `embedding_dim` disagrees with the migrated column width.
    """
    # Inert wherever the vectors do not live in that column: an external store may run any width.
    if settings.vector_store_provider != "pgvector":
        return
    if settings.embedding_dim != SCHEMA_VECTOR_DIM:
        raise DocumentShareError(
            f"embedding_dim={settings.embedding_dim} disagrees with the document_chunks vector "
            f"column ({SCHEMA_VECTOR_DIM}, infra/sql/037_document_index.sql); pgvector would "
            "reject every write. Change both together, or disable the share source."
        )


def _cosine(a: list[float], b: list[float], *, a_norm: float | None = None) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector.

    `a_norm` lets a caller scanning many `b`s against one `a` pass its norm in once. Clamped to [0,
    1] like the Postgres backend, because rounding can put a vector's self-similarity just above
    1.0, which `DocumentHit.score` rejects.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    left = a_norm if a_norm is not None else math.sqrt(sum(x * x for x in a))
    norm = left * math.sqrt(sum(y * y for y in b))
    return min(1.0, max(0.0, dot / norm)) if norm else 0.0


class InMemoryDocumentIndex:
    """Process-local `DocumentIndex` computing the reference ranking in Python.

    A differential test oracle, not a deployment backend: no configuration returns it
    (D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend). Dense search is exact
    cosine, the ordering pgvector's `<=>` produces up to HNSW recall; lexical search is a
    shared-term fraction, matching `ts_rank`'s intent but not its scores.
    """

    def __init__(self) -> None:
        """Start empty; files keyed by `(source, path)`, chunks by `(doc_id, chunking, ordinal)`."""
        # `(source, path)` mirrors the table's primary key: `Projects/report.pdf` is not an
        # unusual name, so two shares can carry it and a path-only key lets one evict the other.
        self._files: dict[tuple[str, str], FileRecord] = {}
        # `(doc_id, chunking_key, ordinal)` mirrors the chunk table's primary key (041) for the
        # same class of reason: two shares can hold one document and cut it at different sizes.
        self._chunks: dict[tuple[str, str, int], ChunkRecord] = {}
        # The embedding configuration each chunk's vector was made by — the in-memory mirror of
        # `document_chunks.embedding_key`. The chunking is in the key itself.
        self._keys: dict[tuple[str, str, int], str] = {}

    @staticmethod
    def _row(chunk: ChunkRecord) -> tuple[str, str, int]:
        """The identity of one chunk row: its document, its cutting, and its place in it."""
        return (chunk.doc_id, chunk.chunking_key, chunk.ordinal)

    def _claimed(self) -> set[tuple[str, str]]:
        """Every `(doc_id, chunking_key)` some file row names — the live chunk sets.

        The in-memory mirror of `CLAIMED_SQL`.
        """
        return {(f.doc_id, f.chunking_key) for f in self._files.values()}

    async def fingerprints(
        self, source: str, paths: list[str], chunking_key: str
    ) -> dict[str, str]:
        """Stored fingerprints for these paths of one source, cut under this chunking."""
        wanted = set(paths)
        return {
            f.path: f.fingerprint
            for f in self._files.values()
            if f.source == source and f.path in wanted and f.chunking_key == chunking_key
        }

    async def known_documents(self, doc_ids: set[str], key: str, chunking_key: str) -> set[str]:
        """Which of these documents have chunks under the current embedding *and* chunking."""
        current = {
            doc_id
            for (doc_id, chunking, _), stored in self._keys.items()
            if stored == key and chunking == chunking_key
        }
        return doc_ids & current

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord], key: str) -> None:
        """Replace each file by path and each chunk by its row, then drop unclaimed cuttings."""
        for chunk in chunks:
            self._chunks[self._row(chunk)] = chunk
            self._keys[self._row(chunk)] = key
        for file in files:
            self._files[(file.source, file.path)] = file
        # After the file rows, so "claimed" reflects this write. Scoped to the touched documents;
        # other shares' cuttings of the same document stay claimed.
        touched = {file.doc_id for file in files} | {chunk.doc_id for chunk in chunks}
        claimed = self._claimed()
        for row in [k for k in self._chunks if k[0] in touched and (k[0], k[1]) not in claimed]:
            del self._chunks[row]
            self._keys.pop(row, None)

    async def stored_document(
        self, source: str, doc_id: str, chunking_key: str, max_chars: int
    ) -> StoredDocument | None:
        """This document as stored, when some path on `source` still holds it under this cutting."""
        paths = sorted(
            f.path
            for f in self._files.values()
            if f.doc_id == doc_id and f.chunking_key == chunking_key and f.source == source
        )
        if not paths:
            return None
        rows = sorted(
            (
                chunk
                for (cdoc, ckey, _), chunk in self._chunks.items()
                if cdoc == doc_id and ckey == chunking_key
            ),
            key=lambda c: c.ordinal,
        )
        kept, truncated = _within_chars(
            [Chunk(ordinal=r.ordinal, content=r.content, coordinate=r.coordinate) for r in rows],
            max_chars,
        )
        return StoredDocument(
            doc_id=doc_id,
            # `min` mirrors `CITATION_SQL`, so the reference backend cites what Postgres cites.
            path=paths[0],
            pieces=kept,
            # `max` across every matching file row, mirroring `_MODIFIED_BY_DOC`: a document copied
            # into several folders is as recent as its most recently touched copy.
            modified_at=max(
                (
                    f.modified_at
                    for f in self._files.values()
                    if f.doc_id == doc_id
                    and f.chunking_key == chunking_key
                    and f.source == source
                    and f.modified_at is not None
                ),
                default=None,
            ),
            truncated=truncated,
        )

    async def stale_chunks(self, key: str, limit: int, chunkings: set[str]) -> list[StaleChunk]:
        """Chunks of a live cutting whose vector was made by a different configuration."""
        stale = [
            StaleChunk(
                doc_id=chunk.doc_id,
                chunking_key=chunk.chunking_key,
                ordinal=chunk.ordinal,
                content=chunk.content,
            )
            for row, chunk in sorted(self._chunks.items())
            if self._keys.get(row) != key and chunk.chunking_key in chunkings
        ]
        return stale[:limit]

    async def store_embeddings(self, chunks: list[ChunkRecord], key: str) -> None:
        """Replace the vector and key of chunks already stored, leaving the rest of the row."""
        for chunk in chunks:
            existing = self._chunks.get(self._row(chunk))
            if existing is None:
                continue
            self._chunks[self._row(chunk)] = existing.model_copy(
                update={"embedding": chunk.embedding}
            )
            self._keys[self._row(chunk)] = key

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
        """Drop this source's rows unseen since `before`, then any chunk set no file row claims."""
        stale = [
            key
            for key, file in self._files.items()
            if key[0] == source and file.indexed_at < before
        ]
        for key in stale:
            del self._files[key]
        # Orphans across every source, not just this one's documents: identical content reachable
        # through a copy on another share must stay indexed (the SQL `NOT EXISTS` says the same).
        claimed = self._claimed()
        for row in [key for key in self._chunks if (key[0], key[1]) not in claimed]:
            del self._chunks[row]
            self._keys.pop(row, None)
        return len(stale)

    def _citation(
        self, doc_id: str, chunking_key: str, source: str, filters: DocumentFilter
    ) -> str:
        """The smallest path in `source` holding this cutting of this document, or `""`.

        Matches the chunking too, so a share cites its own cutting, never another share's.
        """
        candidates = sorted(
            f.path
            for f in self._files.values()
            if f.doc_id == doc_id
            and f.chunking_key == chunking_key
            and f.source == source
            and _matches(f, filters)
        )
        return candidates[0] if candidates else ""

    def _rank(
        self, source: str, filters: DocumentFilter, scored: list[tuple[ChunkRecord, float]], k: int
    ) -> list[DocumentHit]:
        """Resolve each scored chunk to a citation path, drop the unresolvable, take the best k.

        Citations are resolved once per document cutting rather than per chunk, and before the cut
        to k, so a dropped chunk's slot goes to the next best hit.
        """
        hits: list[DocumentHit] = []
        resolved: dict[tuple[str, str], str] = {}
        for chunk, score in scored:
            if score <= 0.0:
                continue
            cutting = (chunk.doc_id, chunk.chunking_key)
            if cutting not in resolved:
                resolved[cutting] = self._citation(
                    chunk.doc_id, chunk.chunking_key, source, filters
                )
            path = resolved[cutting]
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
        """Rank chunks by cosine similarity to the query; drop zero similarity.

        The query norm is computed once for the scan. Scoped to the live embedding configuration, as
        the Postgres backend is.
        """
        query_norm = math.sqrt(sum(x * x for x in query_embedding))
        current = embedding_config_key()
        scored = [
            (c, _cosine(query_embedding, c.embedding, a_norm=query_norm))
            for c in self._chunks.values()
            if self._keys.get(self._row(c)) == current
        ]
        return self._rank(source, filters, scored, top_k)

    async def search_lexical(
        self, source: str, query: str, top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Rank chunks by how much of the query they carry; drop non-matches and exclusions.

        The score is the fraction of wanted terms present, keeping it in [0, 1] and putting complete
        matches first. A query that only excludes terms scores every surviving chunk 1.0, as the
        durable backend returns those rows too.
        """
        wanted, excluded = reference_terms(query)
        if not wanted and not excluded:
            return []
        scored = [
            (chunk, self._coverage(chunk, wanted, excluded)) for chunk in self._chunks.values()
        ]
        return self._rank(source, filters, scored, top_k)

    @staticmethod
    def _coverage(chunk: ChunkRecord, wanted: set[str], excluded: set[str]) -> float:
        """How much of the query this chunk answers, in [0, 1]; 0.0 when it is not a hit at all."""
        tokens = reference_tokens(chunk.content)
        if excluded & tokens:
            return 0.0
        return len(wanted & tokens) / len(wanted) if wanted else 1.0


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


# What makes a chunk row live: some file row, on any share, names both its document and its cutting.
# One definition for the sweep and the per-write cleanup; public because `external_index.py` must
# delete exactly the same rows and their vectors.
CLAIMED_SQL = (
    "EXISTS (SELECT 1 FROM document_files f "
    "WHERE f.doc_id = c.doc_id AND f.chunking_key = c.chunking_key)"
)
# The file-row predicate eligibility and citation share: some path in this source holds the chunk,
# under the same chunking, and satisfies the filters. `EXISTS` rather than a join so a document
# copied into several folders is one candidate, not several. Shared so a chunk is never searchable
# while citing a path that fails the filters.
_FILE_MATCH = (
    "FROM document_files f WHERE f.doc_id = c.doc_id AND f.source = %(src)s "
    "AND f.chunking_key = c.chunking_key "
    "AND (%(tag)s::text IS NULL OR %(tag)s = ANY(f.tags)) "
    "AND (%(since)s::timestamptz IS NULL OR f.modified_at >= %(since)s) "
    "AND (%(until)s::timestamptz IS NULL OR f.modified_at <= %(until)s)"
)
_ELIGIBLE = f"EXISTS (SELECT 1 {_FILE_MATCH}) "
# The citation, resolved in the same statement: the smallest matching path, so a repeated question
# cites the same file. Public because `external_index.py` resolves its hits with this identical
# rule.
CITATION_SQL = f"(SELECT min(f.path) {_FILE_MATCH}) AS path "
# The same two facts for a whole-document read, keyed on bound parameters rather than the chunk row,
# so they are evaluated once for the result instead of per row inside the window (see `_document`).
_FILE_MATCH_BY_DOC = _FILE_MATCH.replace("f.doc_id = c.doc_id", "f.doc_id = %(doc)s").replace(
    "f.chunking_key = c.chunking_key", "f.chunking_key = %(chunking)s"
)
_CITATION_BY_DOC = f"SELECT min(f.path) {_FILE_MATCH_BY_DOC}"
_MODIFIED_BY_DOC = f"SELECT max(f.modified_at) {_FILE_MATCH_BY_DOC}"
# The same file rows' modification time, for a whole-document read: `max`, because a document
# copied into several folders is as recent as the most recently touched copy of it.
_MODIFIED_SQL = f"SELECT max(f.modified_at) {_FILE_MATCH}"


class PostgresDocumentIndex:
    """Durable `DocumentIndex` over `document_files` + `document_chunks` (`infra/sql/037`).

    Dense search is cosine distance (`<=>`) over the HNSW index; lexical search is `ts_rank` over
    the GIN-indexed `tsvector`. `settings.embedding_dim` must equal the `vector(N)` column;
    `require_schema_vector_width` refuses a mismatch up front.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Bind to the configured DSN and the configured embedding width."""
        self._require_vector_column()
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        width = settings.embedding_dim
        self._upsert_file = (
            "INSERT INTO document_files "
            "(path, source, doc_id, fingerprint, tags, modified_at, indexed_at, chunking_key) "
            "VALUES (%(path)s, %(src)s, %(doc)s, %(fp)s, %(tags)s, %(mtime)s, now(), %(chunking)s) "
            "ON CONFLICT (source, path) DO UPDATE SET "
            "doc_id = EXCLUDED.doc_id, "
            "fingerprint = EXCLUDED.fingerprint, tags = EXCLUDED.tags, "
            "modified_at = EXCLUDED.modified_at, indexed_at = now(), "
            "chunking_key = EXCLUDED.chunking_key"
        )
        self._upsert_chunk = (
            "INSERT INTO document_chunks "
            "(doc_id, ordinal, content, coordinate, embedding, lexeme, embedding_key, "
            "chunking_key) "
            f"VALUES (%(doc)s, %(ord)s, %(content)s, %(coord)s, %(emb)s::vector({width}), "
            "to_tsvector('english', %(search)s), %(key)s, %(chunking)s) "
            "ON CONFLICT (doc_id, chunking_key, ordinal) DO UPDATE SET "
            "content = EXCLUDED.content, coordinate = EXCLUDED.coordinate, "
            "embedding = EXCLUDED.embedding, lexeme = EXCLUDED.lexeme, "
            "embedding_key = EXCLUDED.embedding_key"
        )
        # The previous cutting of a document this write re-chunked, deleted at the end of the same
        # transaction (after the file rows, so `CLAIMED_SQL` sees them). Scoped to the written
        # documents: a primary-key range, not a table scan.
        self._drop_unclaimed = (
            f"DELETE FROM document_chunks c WHERE c.doc_id = ANY(%(docs)s) AND NOT {CLAIMED_SQL}"
        )
        # Re-embedding touches only the vector and its key; content and tsvector are unchanged.
        # Addressed by the full primary key so one share's re-embed cannot overwrite another share's
        # row.
        self._store_embedding = (
            f"UPDATE document_chunks SET embedding = %(emb)s::vector({width}), "
            "embedding_key = %(key)s "
            "WHERE doc_id = %(doc)s AND chunking_key = %(chunking)s AND ordinal = %(ord)s"
        )
        # The whole document in order, gated on the same eligibility a search uses (tag and time
        # filters bound NULL; source and chunking still apply), so a document is readable only from
        # the share that indexed it.
        # Bounded in SQL: the window sums content length in `ordinal` order and keeps every piece
        # whose preceding total is under the cap — the same set `_within_chars` keeps — and
        # `remaining` says whether more existed.
        # The citation and modification time are invariant across the result, so they are resolved
        # once in the outer `SELECT`; inside the window they would run per eligible row.
        self._document = (
            f"SELECT ordinal, content, coordinate, remaining, ({_CITATION_BY_DOC}) AS path, "
            f"({_MODIFIED_BY_DOC}) AS modified_at FROM ("
            "SELECT c.ordinal, c.content, c.coordinate, "
            "sum(length(c.content)) OVER (ORDER BY c.ordinal) - length(c.content) AS before, "
            "count(*) OVER () AS remaining "
            f"FROM document_chunks c "
            f"WHERE c.doc_id = %(doc)s AND c.chunking_key = %(chunking)s AND {_ELIGIBLE}"
            ") AS windowed WHERE before < %(cap)s ORDER BY ordinal"
        )
        # `IS DISTINCT FROM`, not `<>`: NULL is every row written before the key column existed,
        # and `<>` would silently pass over exactly those.
        self._stale = (
            "SELECT doc_id, chunking_key, ordinal, content FROM document_chunks "
            "WHERE embedding_key IS DISTINCT FROM %(key)s "
            "AND chunking_key = ANY(%(chunkings)s) "
            "ORDER BY doc_id, chunking_key, ordinal LIMIT %(k)s"
        )
        # The `> 0` floor mirrors the in-memory reference: a zero or negatively correlated chunk is
        # not a hit, and pgvector would otherwise return the top k unconditionally.
        # The `(doc_id, ordinal)` tie-break sorts the k returned rows in an outer query; put in the
        # inner `ORDER BY` it would stop the planner using the HNSW index. HNSW is approximate, so
        # this pins the order of returned hits, not which rows win a tie at place k.
        # `embedding_key` is a read predicate so a different model of the same width never ranks old
        # vectors against a new query.
        self._dense = (
            "SELECT doc_id, ordinal, content, coordinate, score, path FROM ("
            "SELECT c.doc_id, c.ordinal, c.content, c.coordinate, "
            f"1 - (c.embedding <=> %(q)s::vector({width})) AS score, {CITATION_SQL}"
            "FROM document_chunks c "
            "WHERE c.embedding IS NOT NULL AND c.embedding_key = %(key)s "
            f"AND 1 - (c.embedding <=> %(q)s::vector({width})) > 0 AND {_ELIGIBLE}"
            f"ORDER BY c.embedding <=> %(q)s::vector({width}) LIMIT %(k)s"
            ") AS hits ORDER BY score DESC, doc_id, ordinal"
        )
        # The boolean rule the note index runs: `chemclaw.core.fulltext.TSQUERY_TERMS` builds both
        # query forms, `any_terms` deciding which chunks match and `all_terms` ranking complete
        # matches first, matching `InMemoryDocumentIndex`.
        self._lexical = (
            "SELECT c.doc_id, c.ordinal, c.content, c.coordinate, "
            f"ts_rank(c.lexeme, any_terms) AS score, {CITATION_SQL}"
            f"FROM document_chunks c, {TSQUERY_TERMS} "
            f"WHERE c.lexeme @@ any_terms AND {_ELIGIBLE}"
            "ORDER BY (c.lexeme @@ all_terms) DESC, score DESC, c.doc_id, c.ordinal LIMIT %(k)s"
        )

    def _require_vector_column(self) -> None:
        """Refuse a deployment whose `embedding_dim` cannot fit the column this index writes.

        A hook because the external-store subclass never writes that column.
        """
        require_schema_vector_width()

    async def _forget_vectors(self, keys: list[tuple[str, str, int]]) -> None:
        """Told which chunk rows a re-chunk just superseded, so a subclass can drop their vectors.

        A no-op here, since the vectors are in the deleted rows. Called after the commit, so a
        subclass never removes vectors for a rolled-back transaction.
        """

    def _chunk_vector(self, chunk: ChunkRecord) -> str | None:
        """The pgvector literal to store for this chunk, or `None` to leave the column NULL.

        The external-store subclass returns `None` (`NULL::vector(N)` is valid at any `N`) and
        inherits the rest of `upsert`.
        """
        return _vector_literal(chunk.embedding)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout (pooled where opened)."""
        async with db.connection(self._dsn) as conn:
            yield conn

    async def fingerprints(
        self, source: str, paths: list[str], chunking_key: str
    ) -> dict[str, str]:
        """The stat signature each of these paths was last read at, for the ones on record.

        Scoped to the crawl chunk's paths and to the chunking, so a file whose boundaries are
        superseded reads as changed; a NULL chunking matches no key.
        """
        if not paths:
            return {}
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT path, fingerprint FROM document_files "
                    "WHERE source = %s AND path = ANY(%s) AND chunking_key = %s",
                    (source, sorted(paths), chunking_key),
                )
                rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}

    async def known_documents(self, doc_ids: set[str], key: str, chunking_key: str) -> set[str]:
        """Which of these documents have current-configuration chunks — asked before embedding."""
        if not doc_ids:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT doc_id FROM document_chunks "
                    "WHERE doc_id = ANY(%s) AND embedding_key = %s AND chunking_key = %s",
                    (sorted(doc_ids), key, chunking_key),
                )
                rows = await cur.fetchall()
        return {row[0] for row in rows}

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord], key: str) -> None:
        """Write the chunks first, then the file rows, then sweep unclaimed cuttings — one txn.

        Order matters on a crash: a file row whose chunks are missing would match its fingerprint
        and be skipped forever, while chunks without a file row are only invisible until it lands.
        The cleanup is last so "claimed" includes the file rows this write moved.
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
                        # The document's own text, unchanged: what a reader gets back and what
                        # `reembed_stale` re-embeds.
                        "content": chunk.content,
                        # Normalised exactly as the query is (`chemclaw.core.fulltext`). Bound
                        # separately from `content` because normalisation detaches signs from
                        # numbers, right for a lexeme and wrong for stored prose.
                        "search": normalize_search_text(chunk.content),
                        "coord": chunk.coordinate,
                        "emb": self._chunk_vector(chunk),
                        "key": key,
                        "chunking": chunk.chunking_key,
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
                        "chunking": file.chunking_key,
                    },
                )
            touched = sorted({file.doc_id for file in files} | {c.doc_id for c in chunks})
            async with conn.cursor() as cur:
                await cur.execute(
                    f"{self._drop_unclaimed} RETURNING c.doc_id, c.chunking_key, c.ordinal",
                    {"docs": touched},
                )
                superseded = await cur.fetchall()
            await conn.commit()
        await self._forget_vectors([(r[0], r[1], r[2]) for r in superseded])

    async def stale_chunks(self, key: str, limit: int, chunkings: set[str]) -> list[StaleChunk]:
        """Up to `limit` chunks of a live cutting whose vector is not the current configuration."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._stale, {"key": key, "k": limit, "chunkings": sorted(chunkings)}
                )
                rows = await cur.fetchall()
        return [
            StaleChunk(doc_id=r[0], chunking_key=r[1], ordinal=r[2], content=r[3]) for r in rows
        ]

    async def store_embeddings(self, chunks: list[ChunkRecord], key: str) -> None:
        """Replace each chunk's vector and key in one transaction."""
        if not chunks:
            return
        async with self._connection() as conn:
            for chunk in chunks:
                await conn.execute(
                    self._store_embedding,
                    {
                        "emb": _vector_literal(chunk.embedding),
                        "key": key,
                        "doc": chunk.doc_id,
                        "chunking": chunk.chunking_key,
                        "ord": chunk.ordinal,
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
        """Delete this source's rows unseen since `before`, then any chunk set nothing claims."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM document_files WHERE source = %s AND indexed_at < %s",
                    (source, before),
                )
                removed = cur.rowcount
                # Orphans, not "chunks of the deleted documents": the same content may still be
                # reachable through a copy elsewhere.
                await cur.execute(f"DELETE FROM document_chunks c WHERE NOT {CLAIMED_SQL}")
            await conn.commit()
        return removed

    def _read_key(self) -> str:
        """The `embedding_key` a read must match — the live configuration, as stored.

        A hook because `ExternalVectorDocumentIndex` namespaces the keys it writes, and reads must
        use the same spelling.
        """
        return embedding_config_key()

    def _params(self, source: str, top_k: int, filters: DocumentFilter) -> dict[str, object]:
        """The filter parameters both statements bind (NULL meaning unrestricted)."""
        return {
            "src": source,
            "k": top_k,
            "tag": filters.tag or None,
            "since": filters.since,
            "until": filters.until,
        }

    async def _run(
        self, statement: str, params: dict[str, object], *, vector_recall: bool = False
    ) -> list[DocumentHit]:
        """Execute a ranked search and build hits, dropping any whose citation resolved to NULL.

        `vector_recall` applies the configured pgvector recall parameters to this statement's
        transaction (`db.apply_vector_recall_settings`); on for the dense leg, off for the exact
        lexical one.

        Args:
            statement: The ranked search to run.
            params: Its bound parameters.
            vector_recall: Whether this statement takes an HNSW scan worth parametrizing.

        Raises:
            DocumentIndexError: The backend could not answer. Wraps any `psycopg.Error` so the
            caller sees a subsystem type that `retrieval.fanout._sweep` degrades; the message
            carries no driver text because `api/middleware` relays it to the client verbatim, and
            the detail stays on `__cause__`.
        """
        try:
            async with self._connection() as conn:
                async with conn.cursor() as cur:
                    if vector_recall:
                        await db.apply_vector_recall_settings(cur)
                    await cur.execute(statement, params)
                    rows = await cur.fetchall()
        except psycopg.Error as exc:
            raise DocumentIndexError(
                "the document index did not answer, so the search never ran"
            ) from exc
        return [
            DocumentHit(
                doc_id=row[0],
                ordinal=row[1],
                content=row[2],
                coordinate=row[3],
                # Clamped: `ts_rank` sums per-term weights and is only usually below 1; clipping
                # changes no order.
                score=min(1.0, max(0.0, float(row[4]))),
                path=row[5],
            )
            for row in rows
            if row[5]
        ]

    async def stored_document(
        self, source: str, doc_id: str, chunking_key: str, max_chars: int
    ) -> StoredDocument | None:
        """This document as stored, when some path on `source` still holds it under this cutting."""
        params: dict[str, Any] = {
            "doc": doc_id,
            "chunking": chunking_key,
            "src": source,
            "cap": max_chars,
            "tag": None,
            "since": None,
            "until": None,
        }
        try:
            async with self._connection() as conn, conn.cursor() as cur:
                await cur.execute(self._document, params)
                rows = await cur.fetchall()
        except psycopg.Error as exc:
            raise DocumentIndexError(
                "the document index did not answer, so the document was not read"
            ) from exc
        # Every row carries the same resolved citation; a document no live file row claims returns
        # none at all, which is the `None` this method promises rather than an empty document.
        if not rows or not rows[0][4]:
            return None
        return StoredDocument(
            doc_id=doc_id,
            path=rows[0][4],
            pieces=[Chunk(ordinal=r[0], content=r[1], coordinate=r[2]) for r in rows],
            modified_at=rows[0][5],
            # `remaining` counts every eligible piece; fewer rows came back than that means the
            # window stopped early.
            truncated=len(rows) < rows[0][3],
        )

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
        params["key"] = self._read_key()
        return await self._run(self._dense, params, vector_recall=True)

    async def search_lexical(
        self, source: str, query: str, top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Rank chunks by full-text `ts_rank` against the terms in `query`."""
        params = self._params(source, top_k, filters)
        params["q"] = normalize_search_text(query)
        return await self._run(self._lexical, params)


def default_document_index() -> DocumentIndex:
    """The production document index — one place the retriever and the sync get their backend.

    `vector_store_provider == "pgvector"` (the default) keeps vectors in the same statement that
    resolves the citation; any other provider composes the Postgres catalogue with an external
    store. The external branch is imported lazily so a default deployment never needs its client
    package.
    """
    if settings.vector_store_provider == "pgvector":
        return PostgresDocumentIndex()
    from chemclaw.ingest.documents.external_index import ExternalVectorDocumentIndex
    from chemclaw.retrieval.vectors.registry import default_vector_store

    return ExternalVectorDocumentIndex(default_vector_store())
