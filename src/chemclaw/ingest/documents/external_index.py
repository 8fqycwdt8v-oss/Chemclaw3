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

    `doc_id@chunking_key#ordinal`, which is the chunk's primary key in `document_chunks`
    (`infra/sql/041`). One function, because the write and the read must agree and a second spelling
    of a key is how they stop agreeing.

    **The chunking is in the address, and leaving it out was a bug.** It reached `main` as one:
    this index shipped keyed on `(doc_id, ordinal)` the same day the chunk table gained
    `chunking_key`, and neither change was wrong alone. Together, two cuttings of one document
    collide on a single point — the finer cutting's ordinal 3 overwrites the coarser's, so a
    re-tuned `chunk_chars` silently corrupts the store while the catalogue holds both sets intact.
    """
    return f"{doc_id}@{chunking_key}#{ordinal}"


def parse_point_id(reference: str) -> tuple[str, str, int] | None:
    """Read a point id back into `(doc_id, chunking_key, ordinal)`, or `None` when it is not one.

    `None` rather than an exception: the store is a separate system that may hold points this
    catalogue no longer knows about — a crashed run, a collection shared by mistake, an id written
    before the chunking joined the key — and one unreadable id must degrade to "this hit cannot be
    resolved" rather than fail the search.
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
            id=point_id(chunk.doc_id, chunk.chunking_key, chunk.ordinal),
            vector=chunk.embedding,
            # Eligibility is decided per *cutting* of a document, not per document: `_ELIGIBLE`
            # requires `f.chunking_key = c.chunking_key`, so a share that cuts a document at its own
            # size must never be served another share's cutting of the same text.
            group=group_key(chunk.doc_id, chunk.chunking_key),
        )
        for chunk in chunks
    ]


def group_key(doc_id: str, chunking_key: str) -> str:
    """What a scope narrows on: one cutting of one document.

    The pair the catalogue treats as a unit — `document_files` carries both, and eligibility joins
    them. Keeping the group at `doc_id` alone would let a filtered search match a document through
    its *superseded* cutting's points.
    """
    return f"{doc_id}@{chunking_key}"


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

    async def _forget_vectors(self, keys: list[tuple[str, str, int]]) -> None:
        """Drop the points of chunk rows a re-chunk just superseded.

        Without this the store grows forever: `PostgresDocumentIndex.upsert` deletes the previous
        cutting's rows at the end of its transaction, and the vectors those rows described would
        stay behind — unreachable, since every search resolves its hits through the catalogue, but
        never reclaimed. Re-tuning `chunk_chars` on a large share would leave a second full copy of
        the corpus in the vector database.
        """
        if keys:
            await self._store.delete(
                self._collection,
                [point_id(doc, chunking, ordinal) for doc, chunking, ordinal in keys],
            )

    def _read_key(self) -> str:
        """The stored spelling of the live configuration, for the inherited catalogue statements.

        This class's `search_dense` ranks in the store rather than in the `_dense` statement, so
        nothing reaches it today — which is exactly when a namespaced write and an un-namespaced
        read are cheap to hold in step, rather than a scoped search that silently matches no row.
        """
        return self._stored_key(super()._read_key())

    def _stored_key(self, key: str) -> str:
        """The `embedding_key` a `document_chunks` row carries while its vector is in the store.

        The same rule the note index follows, and it was missing here first — this is the larger
        corpus, so the silent-empty search it prevents is the more expensive one.
        `chemclaw.retrieval.vectors.base.stored_embedding_key` states it and its residual.
        """
        return stored_embedding_key(key, settings.vector_store_provider, self._collection)

    async def known_documents(self, doc_ids: set[str], key: str, chunking_key: str) -> set[str]:
        """Which documents have chunks under this embedding *in this store*.

        `fingerprints` needs no override beside this one: it diffs a file's `mtime_ns:size`, which
        says nothing about vectors. Every method that compares an *embedding* key does.
        """
        return await super().known_documents(doc_ids, self._stored_key(key), chunking_key)

    async def stale_chunks(self, key: str, limit: int, chunkings: set[str]) -> list[StaleChunk]:
        """Chunks whose vector was made by another configuration *or* left in another store."""
        return await super().stale_chunks(self._stored_key(key), limit, chunkings)

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord], key: str) -> None:
        """Send the vectors, then commit the catalogue — in that order, always.

        The ordering `PostgresDocumentIndex.upsert` established, carried across the split. Vectors
        first: a crash after them leaves points the next run overwrites by id, whereas a committed
        file row whose vectors never arrived looks unchanged to every later crawl and would be
        invisible forever.
        """
        if chunks:
            await self._store.upsert(self._collection, _points_for(chunks))
        await super().upsert(files, chunks, self._stored_key(key))

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
                # Orphans across every source, and by the base's own predicate rather than a
                # second copy of it: identical content reachable through a copy on another share
                # must stay indexed, and a chunk set is claimed by its *cutting* as well as its
                # document. Hand-writing this here is how the two stores would come to disagree
                # about what an orphan is — which they briefly did, when this said only
                # `f.doc_id = c.doc_id` and the base had already added the chunking.
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
        eligible = await self._eligible_cuttings(source, filters)
        if not eligible:
            return []
        # The scope is a set of *cuttings*, spelled with the same `group_key` the points were
        # written under — that identity is the whole contract between the two calls, and it broke
        # once already: the points moved to `doc_id@chunking_key` and the scope stayed at `doc_id`,
        # so the intersection was empty and every scoped search returned nothing at all. Eligibility
        # is a property of the file rows a cutting is reachable through, so it is never finer than
        # the cutting, and asking the catalogue to enumerate every chunk would turn a filter into a
        # second scan.
        matches = await self._store.search(self._collection, query_embedding, top_k, eligible)
        if not matches:
            return []
        return await self._resolve(source, matches, filters)

    async def _eligible_cuttings(self, source: str, filters: DocumentFilter) -> set[str]:
        """The `group_key`s of `source` satisfying `filters`. Always a set — never "no restriction".

        **Cuttings, not documents, and the two must be spelled by `group_key` on both sides.** A
        point's group is `doc_id@chunking_key`, because `_ELIGIBLE` decides eligibility per cutting:
        a share that cuts a document at its own size must never be served another share's cutting of
        the same text. Returning bare doc ids here made the scope disjoint from every group in the
        store, so `VectorStore.search` matched nothing and this backend answered *every* dense query
        with `[]` — a total retrieval outage that no test saw, because the failure mode of a scope
        that is too narrow looks exactly like a corpus with no eligible documents.

        **The source is always a restriction, and skipping the scope for an unfiltered query was a
        bug.** Every enabled share writes into one collection
        (`vector_store_document_collection` is a single setting), so a search that sends no scope
        takes the top-k across *all* shares. `_resolve` then drops every hit belonging to another
        source, because `CITATION_SQL` filters on `%(src)s` and yields NULL for it — and the caller
        silently gets fewer than `top_k` hits, or none, with nothing raised. The pgvector index
        never had this: `_ELIGIBLE` carries `f.source = %(src)s` *inside* the ranking statement, so
        its top-k is taken over eligible rows only. Two backends disagreeing about what a search
        means is worse than either being slow. Measured and recorded in
        `D-2026-08-08-a-vector-store-is-not-a-catalogue.md`.

        An empty set means nothing is eligible, and the caller must return no hits rather than
        search unscoped — `VectorStore.search` draws the same distinction between `set()` and
        `None`, and this method now never produces the latter.

        **The stated residual, and it is a real limit.** This enumerates the source's matching
        cuttings and sends them to the store, so an unfiltered query over a million-document share
        builds a million-key filter. That is not a "documented cost" so much as a ceiling on how far
        this composition scales as written; `docs/planning/BACKLOG.md` carries the row and names the
        fix (a source the store can filter on itself, which needs a payload the sync can maintain
        through content dedup — the hard part, and why it is not done here). Correct first.
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

        One statement for the whole page of hits — a keyed lookup over at most `top_k` rows, not a
        scan. The citation is resolved with `CITATION_SQL`, the *same* expression the pgvector index
        uses, so which of several identical copies gets cited does not depend on where the vectors
        happen to live.

        Anything the catalogue cannot resolve is dropped: a point whose chunk was swept, or whose
        citation resolves to no path under these filters, is not evidence a reader could check, and
        the contract is that a hit cites something openable.
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
        # The store ranked them; the catalogue only added text. Re-sorted because a SQL result set
        # has no order of its own, and the tie-break matches every other index here.
        hits.sort(key=lambda hit: (-hit.score, hit.doc_id, hit.ordinal))
        return hits
