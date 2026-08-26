"""The narrow seam a vector database attaches on — Protocols, and nothing that can reach one.

This module imports no client and no third-party package, deliberately, exactly as
`chemclaw.ingest.eln.warehouse.driver` does for the warehouse seam. That is what lets the
composition above it be exercised in CI against a fake, on a machine with no vector database
running and no vendor package installed. A real client lives alone in its own adapter module,
imported only when configuration names it.

**Why this interface is much narrower than `DocumentIndex`.** That Protocol has ten methods and only
three of them are vector work: the rest is relational bookkeeping — the file table, the fingerprint
diff, the mark-and-sweep, the backend's own clock — and one lexical leg that is Postgres full-text
search. An adapter asked to implement all ten against a vector database would be fighting it: there
are no joins to resolve a citation with, and no `now()` to measure a sweep against. So the catalogue
stays in Postgres and only the dense half is pluggable. What a vector database is *for* is the two
operations below.

**A point carries an id, a vector, and one grouping key — and no other metadata.** The id addresses
the point (for a document chunk, `doc_id#chunking_key#ordinal` — the row's whole primary key); the
`group` names the object it is a piece of (the `doc_id`). Everything else a query might filter on —
tags, dates, which share a file came from — stays in the catalogue. Pushing *those* into the
store's payload is the textbook shape and the wrong one here: a tag belongs to a *path* and a chunk
belongs to *content*, so one report filed in two project folders has two tag sets and one set of
chunks. Storing their union would let a tag
filter match a chunk whose other copy carries the tag — a silent wrong answer bought to save a round
trip. A group is different in kind: it is the point's own identity, not an attribute of some other
row, so it cannot go stale against anything.

**Eligibility therefore arrives as a scope of groups, applied before the top-k rather than after
it.** This is the shape `NoteIndex.search_dense(within=...)` already has, and it exists for a
measured reason: filter after the cut and a narrow tag over a wide corpus returns nothing at all,
because the k nearest vectors all belonged to something else. Groups rather than ids because the
catalogue decides eligibility per *document* and a document is many chunks — asking it to enumerate
every chunk of every eligible document would turn a filter into a second full scan.

The residual is stated in `README.md` rather than hidden: a scope is a set, and a broad filter over
a very large corpus builds a big one.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError


def stored_embedding_key(embedding_key: str, provider: str, collection: str) -> str:
    """The `embedding_key` a catalogue row carries when its vector lives in an external store.

    `chemclaw.core.embeddings.embedding_config_key` answers *is this vector still valid* — which
    model, which endpoint, which width. It cannot answer *is this vector reachable*, and the two
    came apart the moment a second backend existed. A deployment that moves a corpus from one store
    to another leaves every catalogue row carrying a key that still matches, so the fingerprint diff
    finds nothing to do, nothing is re-embedded, and the search answers from an empty collection —
    no hits, no error. That is `infra/sql/039`'s defect with the *location* as the thing that moved
    instead of the model.

    So a row written against a store records both. The provider is in the key and not only the
    collection, because both indexes default to a collection name that does not mention the vendor:
    a Qdrant-to-Databricks move with the shipped defaults produces the *same* collection string, and
    namespacing on that alone would have missed the commonest switch there is.

    **What this does not catch, stated rather than implied:** a move that keeps the provider and the
    collection name — repointing `vector_store_url` at a different server, or dropping and
    recreating the collection in place. Both are operator actions on the store itself rather than a
    configuration change this system sees, and the answer to them is `--full`. Putting the URL in
    the key was considered and rejected: a hostname change that renames the same server would then
    re-embed the whole corpus for nothing.
    """
    return f"{embedding_key}@{provider}:{collection}"


class VectorStoreError(SubsystemUnavailableError):
    """The vector store could not be reached, so the search never ran.

    A `SubsystemUnavailableError` and deliberately **not** a `ChemclawError` — this repository's
    non-retryable bad-data contract — for the same reason `DocumentIndexError` is one: a timeout or
    a dropped connection says nothing about the query, and the identical call succeeds once the
    store is back. Registering it as bad data would make an activity give up on a blip it would
    otherwise ride out.

    The message stays free of hostnames and client text; the underlying exception carries those as
    `__cause__`, for the log and the operator.
    """


class VectorStoreConfigError(ChemclawError):
    """The store cannot be built as configured: no client installed, or a bad provider name.

    The opposite case to `VectorStoreError`, and a `ChemclawError` (so a `ValueError`) so
    `chemclaw.durable.publish` marks it non-retryable by class name. A missing client package fails
    identically on every attempt; burning a Temporal retry budget on it only delays the operator
    seeing which package to install.
    """


class VectorPoint(BaseModel):
    """One embedded chunk as the store holds it: an address, a vector, and what it is a piece of.

    No content. What the text says and where it came from is the catalogue's business, and
    duplicating it here would create a second copy of the corpus that can drift from the first —
    the failure `document_chunks` avoids by keying on content in the first place.
    """

    # `doc_id#chunking_key#ordinal` for a document chunk — the row's whole primary key, because
    # two of the three do not identify one. Opaque to the store; the catalogue parses it back.
    id: str = Field(min_length=1)
    vector: list[float]
    # The object this point is a piece of — a `doc_id` for a chunk. What `search`'s scope matches
    # on. Defaults to the id itself, which is right for anything embedded whole (a note is one
    # vector and its own group), so a caller with no grouping never thinks about it.
    group: str = ""

    @property
    def group_key(self) -> str:
        """The group to file this point under: the declared one, or the id when there is none."""
        return self.group or self.id


class VectorMatch(BaseModel):
    """One ranked point: its id and its similarity, best first in a result list."""

    id: str
    # Cosine similarity, bounded like `DocumentHit.score` and for the same reason: two backends with
    # different arithmetic meet one contract, and floating-point rounding puts an identical vector's
    # self-similarity fractionally above 1.0 about half the time.
    score: float = Field(ge=0.0, le=1.0)


@runtime_checkable
class VectorStore(Protocol):
    """Dense similarity search over one named collection of embeddings.

    Three operations, because that is what a vector database is for. Everything else a corpus needs
    — what the chunk says, which file it came from, whether that file still exists — belongs to the
    catalogue that owns the text.
    """

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        """Insert or replace each point by id.

        Idempotent by contract: the sync re-embeds the same chunk whenever its document changes or
        the embedding configuration moves, and neither may accumulate duplicates.
        """
        ...

    async def search(
        self,
        collection: str,
        embedding: list[float],
        top_k: int,
        groups: set[str] | None = None,
    ) -> list[VectorMatch]:
        """Return up to `top_k` points most similar to `embedding`, best first.

        `groups` restricts the search to points in these groups **before** the top-k cut, so a
        caller's eligibility filter keeps full recall rather than competing with ineligible
        neighbours for the k slots. `None` means the whole collection, which is the common case and
        must cost nothing extra. An *empty* set means nothing is eligible and returns nothing — a
        different statement from `None`, and one an adapter must not send as an unfiltered search.

        Non-positive similarity is not a hit and is dropped, mirroring the `> 0` floor both
        Postgres-backed indexes apply: without it a narrow corpus surfaces unrelated documents as
        cited evidence, since a nearest-neighbour search returns the k nearest unconditionally.
        """
        ...

    async def delete(self, collection: str, ids: list[str]) -> None:
        """Remove these points; ids that are not present are not an error.

        Called by the sweep after the catalogue has decided what is gone. Tolerant of absence
        because the catalogue is the record: a point already missing is the state being asked for.
        """
        ...
