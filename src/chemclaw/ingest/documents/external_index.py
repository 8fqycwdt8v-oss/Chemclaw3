"""The document index with its dense vectors in an external store, and its catalogue in Postgres.

The other `DocumentIndex` implementation (`PostgresDocumentIndex` is the default), for a deployment
whose embeddings live in a dedicated vector database. It is a subclass rather than a rewrite,
because everything except the dense half is *identical*: the file table, the fingerprint diff, the
mark, the sweep's clock and the lexical leg are relational work that does not move and must not be
duplicated. Five of the ten `DocumentIndex` methods are therefore inherited untouched.

**What moves, and what does not.** The record is
`docs/decisions/D-2026-08-08-a-vector-store-is-not-a-catalogue.md`. The short version: a vector
database stores and searches vectors. It has no joins to resolve a citation with, no clock for a
mark-and-sweep to measure against, and no full-text ranking comparable to `ts_rank`. Asking one to
be the whole index would mean either denormalizing the file table onto every chunk — which is
*wrong*, not merely costly, since tags belong to a path and chunks belong to content — or losing
the properties the corpus is built on.

**The embedding column stays in `document_chunks` and stays NULL.** Not dropped, because the schema
is shared with the default deployment and a migration that removed it would fork the two. Nothing
here writes or reads it, which is why `_require_vector_column` is a no-op: the width it was migrated
with cannot reject a write nobody makes, and enforcing it would refuse a 768-wide deployment over a
column it does not use.

**The write order survives the split, and it is the reason this is safe across two systems.**
`PostgresDocumentIndex.upsert` writes chunks before file rows because a file row whose chunks are
missing looks *unchanged* to the next crawl and would contribute nothing forever, while chunks with
no file row are merely invisible until one lands. That argument is about ordering, not atomicity —
so it holds when the vectors are in another system entirely: send them first, then commit the
catalogue. A crash between the two leaves orphaned vectors, which the next run overwrites by id and
the sweep eventually deletes. A crash the other way round would be the permanent one, and this
ordering makes it unreachable.
"""

import logging
from datetime import datetime

from chemclaw.core.config import settings
from chemclaw.ingest.documents.index import (
    CITATION_SQL,
    ChunkRecord,
    DocumentFilter,
    DocumentHit,
    FileRecord,
    PostgresDocumentIndex,
)
from chemclaw.retrieval.vectors.base import VectorMatch, VectorPoint, VectorStore

logger = logging.getLogger(__name__)


def point_id(doc_id: str, ordinal: int) -> str:
    """The vector store's address for one chunk — the catalogue key, rendered.

    `doc_id#ordinal`, which is the chunk's primary key in `document_chunks`. One function, because
    the write and the read must agree and a second spelling of a key is how they stop agreeing.
    """
    return f"{doc_id}#{ordinal}"


def parse_point_id(reference: str) -> tuple[str, int] | None:
    """Read a point id back into `(doc_id, ordinal)`, or `None` when it is not one.

    `None` rather than an exception: the store is a separate system that may hold points this
    catalogue no longer knows about — a crashed run, a collection shared by mistake — and one
    unreadable id must degrade to "this hit cannot be resolved" rather than fail the search.
    """
    doc_id, separator, ordinal = reference.rpartition("#")
    if not separator or not doc_id:
        return None
    try:
        return doc_id, int(ordinal)
    except ValueError:
        return None


def _points_for(chunks: list[ChunkRecord]) -> list[VectorPoint]:
    """The store's points for these chunks — id, vector, and the document they belong to.

    **One builder, because the group is not optional and defaults to something plausible.** A
    `VectorPoint` with no `group` falls back to grouping by its own id, which is right for anything
    embedded whole and silently wrong here: a re-embedded chunk written that way would be filed
    under `doc-abc#3` instead of `doc-abc`, and would then be invisible to every *filtered* search
    while still answering unfiltered ones. Two call sites built these points and only one passed the
    group, so this existed as a bug for the length of one edit — it is a function now so there is
    nowhere for the second caller to differ.
    """
    return [
        VectorPoint(
            id=point_id(chunk.doc_id, chunk.ordinal),
            vector=chunk.embedding,
            # Eligibility is decided per document, so the document is what a scope narrows on.
            group=chunk.doc_id,
        )
        for chunk in chunks
    ]


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

        See the module docstring. The check exists to turn a pgvector dimension error inside a
        worker into a startup message naming both numbers; with the vectors elsewhere there is no
        such error to pre-empt, and running it anyway would refuse a deployment whose embedding
        model is a perfectly good width the column was never migrated for.
        """

    def _chunk_vector(self, chunk: ChunkRecord) -> str | None:
        """`None` — the embedding goes to the store, and the column stays NULL."""
        return None

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord], key: str) -> None:
        """Send the vectors, then commit the catalogue — in that order, always.

        The ordering `PostgresDocumentIndex.upsert` established, carried across the split. Vectors
        first: a crash after them leaves points the next run overwrites by id, whereas a committed
        file row whose vectors never arrived looks unchanged to every later crawl and would be
        invisible forever.
        """
        if chunks:
            await self._store.upsert(self._collection, _points_for(chunks))
        await super().upsert(files, chunks, key)

    async def store_embeddings(self, chunks: list[ChunkRecord], key: str) -> None:
        """Replace the vectors in the store, and only the `embedding_key` in the catalogue.

        The re-embedding drain (`sync.reembed_stale`) still works unchanged, because what marks a
        vector stale — `document_chunks.embedding_key` — never left Postgres. Only the vector it
        describes lives elsewhere.
        """
        if not chunks:
            return
        await self._store.upsert(self._collection, _points_for(chunks))
        async with self._connection() as conn:
            for chunk in chunks:
                await conn.execute(
                    "UPDATE document_chunks SET embedding_key = %(key)s "
                    "WHERE doc_id = %(doc)s AND ordinal = %(ord)s",
                    {"key": key, "doc": chunk.doc_id, "ord": chunk.ordinal},
                )
            await conn.commit()

    async def prune_stale(self, source: str, before: datetime) -> int:
        """Sweep the catalogue, then delete the vectors of whatever chunks that orphaned.

        Overridden rather than inherited because the base deletes orphan chunks and discards which
        ones; here they have to be named, so their points can be removed from the other system. The
        catalogue is committed first and the store second — the catalogue is the record, and a point
        whose chunk is gone is unreachable (every search resolves its hits through the catalogue)
        rather than wrong.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM document_files WHERE source = %s AND indexed_at < %s",
                    (source, before),
                )
                removed = cur.rowcount
                # Orphans across every source, exactly as the base does it: identical content
                # reachable through a copy on another share must stay indexed.
                await cur.execute(
                    "DELETE FROM document_chunks c WHERE NOT EXISTS "
                    "(SELECT 1 FROM document_files f WHERE f.doc_id = c.doc_id) "
                    "RETURNING c.doc_id, c.ordinal"
                )
                orphaned = await cur.fetchall()
            await conn.commit()
        if orphaned:
            await self._store.delete(
                self._collection, [point_id(row[0], row[1]) for row in orphaned]
            )
        return removed

    async def search_dense(
        self, source: str, query_embedding: list[float], top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Search the store, scoped to what the catalogue says is eligible, then resolve the hits.

        Three steps, and the middle one is where the design lives:

        1. **Scope.** When the query carries a filter, the catalogue names the documents that
           satisfy it, and that set is handed *into* the search. Filtering the results afterwards
           would return nothing whenever the k nearest vectors all belong to another tag — the
           recall defect `BACKLOG.md` already records against pgvector's post-filtering, which this
           store is partly attached to avoid. An unfiltered query passes no scope and pays nothing,
           which is the common case.
        2. **Search**, in the store, over vectors only.
        3. **Resolve**, in the catalogue: the content, the coordinate and the citation path for the
           ids that came back. A small keyed lookup over `top_k` rows, not a scan.
        """
        if not any(query_embedding):
            return []
        eligible = await self._eligible_documents(source, filters)
        if eligible is not None and not eligible:
            return []
        # The scope is a set of *documents*, which is exactly what `VectorStore.search` narrows on:
        # a point's group is its `doc_id`. Eligibility is a property of the file rows a document is
        # reachable through, so it can never be finer than the document, and asking the catalogue to
        # enumerate every chunk of every eligible document would turn a filter into a second scan.
        matches = await self._store.search(self._collection, query_embedding, top_k, eligible)
        if not matches:
            return []
        return await self._resolve(source, matches, filters)

    async def _eligible_documents(self, source: str, filters: DocumentFilter) -> set[str] | None:
        """The doc ids of `source` satisfying `filters`, or `None` when nothing is filtered.

        `None` is not "no documents" — it is "no restriction", and the distinction is what keeps an
        ordinary question from paying for a scope query at all.

        **The stated residual.** A filter this broad over a corpus this large builds a big set, and
        it is built in memory here and sent to the store. It is bounded by the point of a filter —
        a tag exists to narrow — but it is not bounded by the code, and a deployment whose commonest
        query is a filter matching most of a million-document corpus will feel it. Recorded in
        `docs/planning/BACKLOG.md` rather than pre-optimized, because the fix (a scope the store can
        express itself) trades away the correctness the two-table design buys.
        """
        if not (filters.tag or filters.since or filters.until):
            return None
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT doc_id FROM document_files WHERE source = %(src)s "
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
        return {row[0] for row in rows}

    async def _resolve(
        self, source: str, matches: list[VectorMatch], filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Attach content, coordinate and a citation path to each ranked point id.

        One statement for the whole page of hits — a keyed lookup over at most `top_k` rows, not a
        scan. The citation is resolved with `CITATION_SQL`, the *same* expression the pgvector index
        uses, so which of several identical copies gets cited does not depend on where the vectors
        happen to live.

        Anything the catalogue cannot resolve is dropped: a point whose chunk was swept, or whose
        citation resolves to no path under these filters, is not evidence a reader could check, and
        the contract is that a hit cites something openable.
        """
        addressed: dict[tuple[str, int], float] = {}
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
                    "SELECT c.doc_id, c.ordinal, c.content, c.coordinate, "
                    f"{CITATION_SQL}"
                    "FROM document_chunks c "
                    "JOIN unnest(%(docs)s::text[], %(ords)s::int[]) AS wanted(doc_id, ordinal) "
                    "ON wanted.doc_id = c.doc_id AND wanted.ordinal = c.ordinal",
                    {
                        "src": source,
                        "tag": filters.tag or None,
                        "since": filters.since,
                        "until": filters.until,
                        "docs": [doc for doc, _ in addressed],
                        "ords": [ordinal for _, ordinal in addressed],
                    },
                )
                rows = await cur.fetchall()
        hits = [
            DocumentHit(
                doc_id=row[0],
                ordinal=row[1],
                content=row[2],
                coordinate=row[3],
                path=row[4],
                score=addressed[(row[0], row[1])],
            )
            for row in rows
            if row[4]
        ]
        # The store ranked them; the catalogue only added text. Re-sorted because a SQL result set
        # has no order of its own, and the tie-break matches every other index here.
        hits.sort(key=lambda hit: (-hit.score, hit.doc_id, hit.ordinal))
        return hits
