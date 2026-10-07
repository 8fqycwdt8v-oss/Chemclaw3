"""The narrow seam a vector database attaches on — Protocols, and nothing that can reach one.

Imports no client, so the composition above it runs in CI against a fake; a real client lives in its
own adapter module, imported only when configuration names it.

Only the dense half is pluggable: the catalogue (file table, fingerprints, sweep, lexical leg) stays
in Postgres, which has the joins and the clock a vector database lacks. A point carries an id, a
vector and one grouping key (the `doc_id` for a chunk) and no other metadata: tags belong to a path
while chunks belong to content, so filtering on a payload tag could match a chunk whose *other* copy
carries it. Eligibility therefore arrives as a scope of groups, applied before the top-k so a narrow
filter keeps full recall.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError


def stored_embedding_key(embedding_key: str, provider: str, collection: str) -> str:
    """The `embedding_key` a catalogue row carries when its vector lives in an external store.

    `embedding_config_key` says whether a vector is still *valid*; this also records where it is
    *reachable*, so moving a corpus to another store re-embeds instead of silently searching an
    empty collection. The provider is included because both indexes default to a vendor-neutral
    collection name. Repointing the URL or recreating a collection in place is not caught; the
    answer to that is `--full` (keying on the URL would re-embed on every hostname change).
    """
    return f"{embedding_key}@{provider}:{collection}"


class VectorStoreError(SubsystemUnavailableError):
    """The vector store could not be reached, so the search never ran.

    Retryable (`SubsystemUnavailableError`, not `ChemclawError`): a timeout says nothing about the
    query. The message carries no hostnames or client text; those are on `__cause__`.
    """


class VectorStoreConfigError(ChemclawError):
    """The store cannot be built as configured: no client installed, or a bad provider name.

    A `ChemclawError` so it is non-retryable: a missing package fails identically on every attempt.
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
    # The object this point is a piece of (a `doc_id` for a chunk); what `search`'s scope matches.
    # Defaults to the id itself, right for anything embedded whole.
    group: str = ""

    @property
    def group_key(self) -> str:
        """The group to file this point under: the declared one, or the id when there is none."""
        return self.group or self.id


class VectorMatch(BaseModel):
    """One ranked point: its id and its similarity, best first in a result list."""

    id: str
    # Cosine similarity, bounded like `DocumentHit.score`; adapters clamp rounding above 1.0.
    score: float = Field(ge=0.0, le=1.0)


@runtime_checkable
class VectorStore(Protocol):
    """Dense similarity search over one named collection of embeddings.

    Everything else a corpus needs belongs to the catalogue that owns the text.
    """

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        """Insert or replace each point by id.

        Idempotent by contract: re-embedding the same chunk must not accumulate duplicates.
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

        `groups` restricts the search **before** the top-k cut. `None` means the whole collection;
        an *empty* set means nothing is eligible and returns nothing — an adapter must not send it
        as an unfiltered search. Non-positive similarity is not a hit and is dropped.
        """
        ...

    async def delete(self, collection: str, ids: list[str]) -> None:
        """Remove these points; ids that are not present are not an error.

        The catalogue is the record, so a point already missing is the state being asked for.
        """
        ...
