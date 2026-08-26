"""The note index with its dense vectors in an external store, and its catalogue in Postgres.

The second `NoteIndex` implementation for a deployment whose embeddings live in a dedicated vector
database, and the twin of `ingest/documents/external_index.py` one layer over. A subclass rather
than a rewrite, because everything except the dense half is *identical*: the text, the `tsvector`,
the file fingerprint, the embedding key and the lexical ranking are relational work that does not
move and must not be duplicated.

**Four of the five `NoteIndex` methods are overridden; `search_lexical` is inherited untouched**,
because `ts_rank` over a GIN index has nothing to do with where a vector is kept. Three of the four
are the dense half. The fourth, `fingerprints`, is the one that is not obvious — see below.

**Why this is smaller than the document twin, and does not pretend otherwise.** There, a point is
one chunk of a document and the store's answer has to be rejoined to a catalogue to become a
citation — hence a point-id encoding, a grouping key, a parse-back and a resolve step. A note is
embedded whole: the point id *is* the note id and the hit *is* an `IndexHit`, so all of that
collapses to a rename. `VectorPoint.group` defaults to the id for exactly this case.

**On D-2026-08-08, which declined this.** That decision left `note_index` on pgvector, saying that
"generalizing the seam to a second consumer before the first has run against a live server would be
designing against a guess". The guess it was avoiding was about the *interface*, and that has since
been answered from the other direction: the interface did not move to accommodate this consumer.
`upsert`, `search` and `delete` are used exactly as `ExternalVectorDocumentIndex` uses them, and the
only thing this class needed that did not exist was a way to *retire* a note — which turned out to
be a gap in `NoteIndex` itself rather than in the seam, and is fixed there.

**The write ordering survives the split, and it is the reason this is safe across two systems.**
Vectors go first, then the catalogue row; deletes go the other way round, catalogue first. A crash
between the two leaves an orphaned vector, which the next run overwrites by id and the next prune
removes. A crash the other way round — a catalogue row whose vector never landed — would be
permanent, because the row's fingerprint would then match on every subsequent run and it would never
be re-embedded. That is `external_index.py`'s argument verbatim, and it is about ordering rather
than atomicity, so it holds when the two halves are in different systems.

**`fingerprints` is overridden because a stored key must say *where the vector went*, not only
which model made it.** `embedding_config_key()` is `provider:endpoint:dDIM:model` — it answers "is
this vector still valid", and it cannot answer "is this vector reachable". Those came apart the
moment a second backend existed: a deployment already running an external store for its *documents*
gets its notes moved here by one shared `vector_store_provider`, and every `note_index` row still
carries a matching key while the store holds nothing. `reindex_notes` would compute `changed = []`,
embed nothing, write nothing, and `search_dense` would query an empty collection — zero dense note
hits, no error, until somebody ran `--full` by hand. That is exactly the defect `infra/sql/039` was
written about, one backend over.

So the key this subclass *stores* is namespaced by the collection. On a provider switch the rows
carry the bare key, this index asks for the namespaced one, nothing matches, and every note is
re-embedded into the store it is now supposed to live in. Self-healing by the mechanism 039
established, with no `VectorStore` method added and no effect on the pgvector path.

**The embedding column stays in `note_index` and stays NULL.** Not dropped: the schema is shared
with the default deployment, and a migration that removed it would fork the two. `_row_vector`
returning `None` is what makes the shared `INSERT … ON CONFLICT` write a NULL rather than a vector.
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

    def _stored_key(self, embedding_key: str) -> str:
        """The `embedding_key` written to and read from `note_index`, namespaced by the store.

        One function so the write and the read cannot disagree — a second spelling of "which
        configuration made this row" is how they stop agreeing, which is the argument
        `ingest/documents/external_index.py::point_id` makes about addresses.
        `stored_embedding_key` states the rule and what it does not catch.
        """
        return stored_embedding_key(embedding_key, settings.vector_store_provider, self._collection)

    async def fingerprints(self, embedding_key: str) -> dict[str, str]:
        """Fingerprints of rows this store actually holds vectors for."""
        return await super().fingerprints(self._stored_key(embedding_key))

    async def upsert(self, records: list[NoteRecord], embedding_key: str) -> None:
        """Send the vectors, then commit the catalogue rows. That order is load-bearing."""
        if not records:
            return
        await self._store.upsert(
            self._collection,
            [VectorPoint(id=record.note_id, vector=record.embedding) for record in records],
        )
        await super().upsert(records, self._stored_key(embedding_key))

    async def retire_absent(self, keep: set[str]) -> int:
        """Delete the catalogue rows first, then the points they addressed.

        The reverse of the write order, and for the same reason: a point whose row is already gone
        is invisible and will be deleted by this call or the next one, while a row whose point is
        gone would rank nothing and still claim a fingerprint, so it would never be re-embedded.
        """
        gone = await self._retire_absent_ids(keep)
        if gone:
            await self._store.delete(self._collection, gone)
        return len(gone)

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank in the store, scoped before the cut; the ids that come back are note ids already.

        `within` becomes the store's `groups`, which is the same scope-before-top-k contract
        `NoteIndex.search_dense` already documents — and the reason it is worth sending rather than
        filtering afterwards: filter after the cut and a narrow scope over a wide corpus returns
        nothing at all, because the k nearest vectors all belonged to something else.

        A zero query vector is short-circuited here as well as in the store, because it has cosine 0
        to everything and the round trip would only confirm that.
        """
        if not any(query_embedding):
            return []
        matches = await self._store.search(self._collection, query_embedding, top_k, within)
        return [IndexHit(note_id=match.id, score=match.score) for match in matches]
