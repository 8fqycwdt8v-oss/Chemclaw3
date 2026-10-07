"""The document index with its dense vectors in an external store, and its catalogue in Postgres.

A subclass of `PostgresDocumentIndex` for deployments whose embeddings live in a dedicated vector
database: the file table, fingerprint diff, mark-and-sweep clock and lexical leg are relational work
that stays in Postgres (D-2026-08-08-a-vector-store-is-not-a-catalogue). The `document_chunks`
embedding column is kept for schema parity and left NULL.

Write order carries across the split: vectors first, then the catalogue. A crash in between leaves
orphaned points the next run overwrites by id; the reverse order could leave a committed file row
whose vectors never arrive, invisible forever.
"""

import logging
import time
from datetime import datetime

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.ingest.documents.index import (
    CITATION_SQL,
    CLAIMED_SQL,
    ChunkRecord,
    DocumentFilter,
    DocumentHit,
    FileRecord,
    PostgresDocumentIndex,
    StaleChunk,
)
from chemclaw.retrieval.vectors.base import (
    VectorMatch,
    VectorPoint,
    VectorStore,
    stored_embedding_key,
)

logger = logging.getLogger(__name__)


def point_id(doc_id: str, chunking_key: str, ordinal: int) -> str:
    """The vector store's address for one chunk — the catalogue key, rendered.

    `doc_id@chunking_key#ordinal`, the chunk's primary key in `document_chunks`. One function so
    write and read agree. The chunking is part of the address so two cuttings of one document never
    collide on a point.
    """
    return f"{doc_id}@{chunking_key}#{ordinal}"


def parse_point_id(reference: str) -> tuple[str, str, int] | None:
    """Read a point id back into `(doc_id, chunking_key, ordinal)`, or `None` when it is not one.

    `None` rather than an exception: the store may hold points the catalogue does not know, and one
    unreadable id must not fail the search.
    """
    head, separator, ordinal = reference.rpartition("#")
    if not separator or not head:
        return None
    doc_id, marker, chunking_key = head.partition("@")
    if not marker or not doc_id or not chunking_key:
        return None
    try:
        return doc_id, chunking_key, int(ordinal)
    except ValueError:
        return None


def _points_for(chunks: list[ChunkRecord]) -> list[VectorPoint]:
    """The store's points for these chunks — id, vector, and the document they belong to.

    The single builder, because a `VectorPoint` with no `group` defaults to its own id, which would
    make the chunk invisible to every filtered search.
    """
    return [
        VectorPoint(
            id=point_id(chunk.doc_id, chunk.chunking_key, chunk.ordinal),
            vector=chunk.embedding,
            # Eligibility is per cutting of a document (`_ELIGIBLE` joins on `chunking_key`), so a
            # share is never served another share's cutting of the same text.
            group=group_key(chunk.doc_id, chunk.chunking_key),
        )
        for chunk in chunks
    ]


def group_key(doc_id: str, chunking_key: str) -> str:
    """What a scope narrows on: one cutting of one document.

    Grouping by `doc_id` alone would let a filtered search match through a superseded cutting's
    points.
    """
    return f"{doc_id}@{chunking_key}"


# When each collection last had its drift reported at WARNING, so log volume follows the drift
# rather than the query rate. Keyed by collection, which is configuration and therefore bounded.
_LAST_UNRESOLVED_WARNING: dict[str, float] = {}

# How often one collection's standing drift is re-logged. Only log volume; the counter
# `chemclaw_vector_unresolved_points_total` is unthrottled and is what alerts read.
_UNRESOLVED_WARN_INTERVAL_SECONDS = 300.0


def _report_unresolved(addressed: int, rows: int, hits: int, collection: str) -> None:
    """Say so when the store ranked points the catalogue could not turn into evidence.

    The store and catalogue are not written in one transaction and can drift, which otherwise shows
    only as a source returning zero hits. `addressed - rows` counts points whose chunk row is gone
    (re-sync needed); `rows - hits` counts chunks whose citation resolves to nothing under these
    filters. One counter, since the operator action is the same; the log line carries the split.

    The counter is per query; the WARNING is per collection per interval, because this runs on every
    interactive search and a standing fault must not log once per turn. Per-query detail stays at
    DEBUG.
    """
    if hits >= addressed:
        return
    record_metric(
        lambda m: m.increment("chemclaw_vector_unresolved_points_total", addressed - hits)
    )
    message = (
        "vector store returned %d ranked point(s) from %r; %d resolved to a chunk row and %d to a "
        "citable hit (%.0f%% usable). Points with no chunk row mean the collection has drifted "
        "from `document_chunks` — a sweep or a re-chunk the store was not told about"
    )
    args = (addressed, collection, rows, hits, 100.0 * hits / addressed)
    now = time.monotonic()
    last = _LAST_UNRESOLVED_WARNING.get(collection)
    if last is not None and now - last < _UNRESOLVED_WARN_INTERVAL_SECONDS:
        logger.debug(message, *args)
        return
    _LAST_UNRESOLVED_WARNING[collection] = now
    logger.warning(message, *args)


class ExternalVectorDocumentIndex(PostgresDocumentIndex):
    """A `DocumentIndex` whose catalogue is Postgres and whose vectors are in a `VectorStore`."""

    def __init__(
        self, store: VectorStore, collection: str | None = None, dsn: str | None = None
    ) -> None:
        """Bind to a vector store and the catalogue's DSN.

        Args:
            store: Where the dense vectors live.
            collection: The store's collection name; defaults to the configured one.
            dsn: The catalogue's DSN; defaults to the configured Postgres.
        """
        super().__init__(dsn)
        self._store = store
        self._collection = collection or settings.vector_store_document_collection

    def _require_vector_column(self) -> None:
        """No-op: this index never writes the pgvector column, so its width cannot reject a write.

        Enforcing it would refuse a deployment whose embedding width the unused column was never
        migrated for.
        """

    def _chunk_vector(self, chunk: ChunkRecord) -> str | None:
        """`None` — the embedding goes to the store, and the column stays NULL."""
        return None

    async def _forget_vectors(self, keys: list[tuple[str, str, int]]) -> None:
        """Drop the points of chunk rows a re-chunk just superseded.

        `PostgresDocumentIndex.upsert` deletes the previous cutting's rows; without this their
        vectors would stay in the store unreachable and never reclaimed.
        """
        if keys:
            await self._store.delete(
                self._collection,
                [point_id(doc, chunking, ordinal) for doc, chunking, ordinal in keys],
            )

    def _read_key(self) -> str:
        """The stored spelling of the live configuration, for the inherited catalogue statements.

        Nothing reaches the `_dense` statement here (`search_dense` ranks in the store); kept in
        step with `_stored_key` so a future scoped read cannot silently match no row.
        """
        return self._stored_key(super()._read_key())

    def _stored_key(self, key: str) -> str:
        """The `embedding_key` a `document_chunks` row carries while its vector is in the store.

        The rule `chemclaw.retrieval.vectors.base.stored_embedding_key` states.
        """
        return stored_embedding_key(key, settings.vector_store_provider, self._collection)

    async def known_documents(self, doc_ids: set[str], key: str, chunking_key: str) -> set[str]:
        """Which documents have chunks under this embedding *in this store*.

        `fingerprints` needs no override: it diffs `mtime_ns:size`, not embeddings.
        """
        return await super().known_documents(doc_ids, self._stored_key(key), chunking_key)

    async def stale_chunks(self, key: str, limit: int, chunkings: set[str]) -> list[StaleChunk]:
        """Chunks whose vector was made by another configuration *or* left in another store."""
        return await super().stale_chunks(self._stored_key(key), limit, chunkings)

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord], key: str) -> None:
        """Send the vectors, then commit the catalogue — in that order, always.

        A crash after the vectors leaves points the next run overwrites by id; a committed file row
        whose vectors never arrived would look unchanged to every later crawl.
        """
        if chunks:
            await self._store.upsert(self._collection, _points_for(chunks))
        await super().upsert(files, chunks, self._stored_key(key))

    async def store_embeddings(self, chunks: list[ChunkRecord], key: str) -> None:
        """Replace the vectors in the store, and only the `embedding_key` in the catalogue.

        Staleness (`document_chunks.embedding_key`) stays in Postgres, so `sync.reembed_stale` works
        unchanged.
        """
        if not chunks:
            return
        await self._store.upsert(self._collection, _points_for(chunks))
        async with self._connection() as conn:
            for chunk in chunks:
                await conn.execute(
                    "UPDATE document_chunks SET embedding_key = %(key)s "
                    "WHERE doc_id = %(doc)s AND chunking_key = %(ck)s AND ordinal = %(ord)s",
                    {
                        "key": self._stored_key(key),
                        "doc": chunk.doc_id,
                        "ck": chunk.chunking_key,
                        "ord": chunk.ordinal,
                    },
                )
            await conn.commit()

    async def prune_stale(self, source: str, before: datetime) -> int:
        """Sweep the catalogue, then delete the vectors of whatever chunks that orphaned.

        Overridden because the orphans must be named to remove their points. Catalogue first: it is
        the record, and a point whose chunk is gone is unreachable rather than wrong.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM document_files WHERE source = %s AND indexed_at < %s",
                    (source, before),
                )
                removed = cur.rowcount
                # Orphans across every source, by the base's own predicate: a chunk set is kept
                # while any file row reaches its document and cutting, including a copy on another
                # share.
                await cur.execute(
                    f"DELETE FROM document_chunks c WHERE NOT {CLAIMED_SQL} "
                    "RETURNING c.doc_id, c.chunking_key, c.ordinal"
                )
                orphaned = await cur.fetchall()
            await conn.commit()
        if orphaned:
            await self._store.delete(
                self._collection, [point_id(row[0], row[1], row[2]) for row in orphaned]
            )
        return removed

    async def search_dense(
        self, source: str, query_embedding: list[float], top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Search the store, scoped to what the catalogue says is eligible, then resolve the hits.

        1. **Scope.** The catalogue names the eligible cuttings and that set is passed into the
           search; filtering afterwards would return nothing whenever the k nearest vectors belong
           elsewhere.
        2. **Search**, in the store, over vectors only.
        3. **Resolve**, in the catalogue: content, coordinate and citation path for the returned ids
           — a keyed lookup over `top_k` rows.
        """
        if not any(query_embedding):
            return []
        eligible = await self._eligible_cuttings(source, filters)
        if not eligible:
            return []
        # The scope is spelled with the same `group_key` the points were written under; that
        # identity is the contract between the two calls, and a mismatch silently empties every
        # scoped search.
        matches = await self._store.search(self._collection, query_embedding, top_k, eligible)
        if not matches:
            return []
        return await self._resolve(source, matches, filters)

    async def _eligible_cuttings(self, source: str, filters: DocumentFilter) -> set[str]:
        """The `group_key`s of `source` satisfying `filters`. Always a set — never "no restriction".

        Cuttings, spelled by `group_key` to match the points. The source is always a restriction
        even for an unfiltered query: every share writes into one collection, and an unscoped top-k
        would be dropped by `_resolve`'s source filter, returning fewer hits than the pgvector
        index. An empty set means nothing is eligible and the caller returns no hits.

        Limit: an unfiltered query over a very large share sends one key per cutting to the store;
        `docs/planning/BACKLOG.md` carries the fix.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT doc_id, chunking_key FROM document_files "
                    "WHERE source = %(src)s "
                    "AND (%(tag)s::text IS NULL OR %(tag)s = ANY(tags)) "
                    "AND (%(since)s::timestamptz IS NULL OR modified_at >= %(since)s) "
                    "AND (%(until)s::timestamptz IS NULL OR modified_at <= %(until)s)",
                    {
                        "src": source,
                        "tag": filters.tag or None,
                        "since": filters.since,
                        "until": filters.until,
                    },
                )
                rows = await cur.fetchall()
        # Built by the same function the points were written with, so the two spellings cannot drift
        # apart again without a compile-time-visible change.
        return {group_key(doc_id, chunking) for doc_id, chunking in rows}

    async def _resolve(
        self, source: str, matches: list[VectorMatch], filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Attach content, coordinate and a citation path to each ranked point id.

        One keyed statement over at most `top_k` rows. Uses `CITATION_SQL`, the same expression as
        the pgvector index, so which copy is cited does not depend on the backend. Unresolvable
        points are dropped and reported via `_report_unresolved`.
        """
        addressed: dict[tuple[str, str, int], float] = {}
        for match in matches:
            parsed = parse_point_id(match.id)
            if parsed is None:
                logger.warning(
                    "vector store returned an unreadable point id %r; skipping", match.id
                )
                continue
            addressed[parsed] = match.score
        if not addressed:
            return []
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT c.doc_id, c.chunking_key, c.ordinal, c.content, c.coordinate, "
                    f"{CITATION_SQL}"
                    "FROM document_chunks c "
                    "JOIN unnest(%(docs)s::text[], %(cks)s::text[], %(ords)s::int[]) "
                    "AS wanted(doc_id, chunking_key, ordinal) "
                    "ON wanted.doc_id = c.doc_id AND wanted.chunking_key = c.chunking_key "
                    "AND wanted.ordinal = c.ordinal",
                    {
                        "src": source,
                        "tag": filters.tag or None,
                        "since": filters.since,
                        "until": filters.until,
                        "docs": [doc for doc, _, _ in addressed],
                        "cks": [chunking for _, chunking, _ in addressed],
                        "ords": [ordinal for _, _, ordinal in addressed],
                    },
                )
                rows = await cur.fetchall()
        hits = [
            DocumentHit(
                doc_id=row[0],
                ordinal=row[2],
                content=row[3],
                coordinate=row[4],
                path=row[5],
                score=addressed[(row[0], row[1], row[2])],
            )
            for row in rows
            if row[5]
        ]
        _report_unresolved(len(addressed), len(rows), len(hits), self._collection)
        # The store ranked them; the catalogue only added text. Re-sorted because a SQL result set
        # has no order of its own, and the tie-break matches every other index here.
        hits.sort(key=lambda hit: (-hit.score, hit.doc_id, hit.ordinal))
        return hits
