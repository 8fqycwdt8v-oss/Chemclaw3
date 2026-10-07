"""The note index with its dense vectors in an external store, and its catalogue in Postgres.

The `NoteIndex` for a deployment whose embeddings live in a dedicated vector database; the twin
of `ingest/documents/external_index.py`. A subclass because everything except the dense half (text,
`tsvector`, fingerprint, embedding key, lexical ranking) is identical; `search_lexical` is
inherited. A note is embedded whole, so the point id is the note id.

Write order makes the split safe across two systems: vectors first, then the catalogue row;
deletes the reverse. A crash leaves at worst an orphaned vector, overwritten or pruned later,
never a catalogue row whose vector is missing (which would never be re-embedded).

`fingerprints` is overridden so the stored key also says where the vector went: keys are
namespaced by collection, so switching `vector_store_provider` makes every row mismatch and every
note re-embed into the new store instead of silently searching an empty collection. The
`note_index` embedding column stays (shared schema) and is written NULL via `_row_vector`.
"""

import logging

from chemclaw.core.config import settings
from chemclaw.retrieval.vector_index import IndexHit, NoteRecord, PostgresNoteIndex
from chemclaw.retrieval.vectors.base import (
    VectorPoint,
    VectorStore,
    stored_embedding_key,
)

logger = logging.getLogger(__name__)


class ExternalVectorNoteIndex(PostgresNoteIndex):
    """A `NoteIndex` whose dense half is a `VectorStore` and whose catalogue is `note_index`."""

    def __init__(
        self, store: VectorStore, collection: str | None = None, dsn: str | None = None
    ) -> None:
        """Bind to a store and the collection its note vectors live in."""
        super().__init__(dsn)
        self._store = store
        self._collection = collection or settings.vector_store_note_collection

    def _row_vector(self, record: NoteRecord) -> str | None:
        """`NULL`: the vector went to the store, and nothing here reads this column."""
        return None

    def _read_key(self) -> str:
        """The stored spelling of the live configuration, for the inherited catalogue statements.

        Must match `_stored_key`'s namespacing, or the base's statements would match no row.
        """
        return self._stored_key(super()._read_key())

    def _stored_key(self, embedding_key: str) -> str:
        """The `embedding_key` written to and read from `note_index`, namespaced by the store.

        One function so write and read cannot disagree; `stored_embedding_key` states the rule.
        """
        return stored_embedding_key(embedding_key, settings.vector_store_provider, self._collection)

    async def fingerprints(self, embedding_key: str) -> dict[str, str]:
        """Fingerprints of rows this store actually holds vectors for."""
        return await super().fingerprints(self._stored_key(embedding_key))

    async def upsert(
        self,
        records: list[NoteRecord],
        embedding_key: str,
        *,
        corpus_revision: int | None = None,
    ) -> None:
        """Send the vectors, then commit the catalogue rows. That order is load-bearing."""
        if not records:
            return
        await self._store.upsert(
            self._collection,
            [VectorPoint(id=record.note_id, vector=record.embedding) for record in records],
        )
        await super().upsert(
            records, self._stored_key(embedding_key), corpus_revision=corpus_revision
        )

    async def retire_absent(self, keep: set[str], *, built_before: int | None = None) -> int:
        """Delete the catalogue rows first, then the points they addressed.

        The reverse of the write order: an orphaned point is invisible and deleted later, while a
        row whose point is gone would never be re-embedded.
        """
        gone = await self._retire_absent_ids(keep, built_before=built_before)
        if gone:
            await self._store.delete(self._collection, gone)
        return len(gone)

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank in the store, scoped before the cut; the ids that come back are note ids already.

        `within` becomes the store's `groups`, so the scope applies before top-k (filtering after
        would return nothing for a narrow scope). A zero query vector short-circuits: it has cosine
        0 to everything.
        """
        if not any(query_embedding):
            return []
        matches = await self._store.search(self._collection, query_embedding, top_k, within)
        return [IndexHit(note_id=match.id, score=match.score) for match in matches]
