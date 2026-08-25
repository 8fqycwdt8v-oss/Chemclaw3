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

**A chunk's identity is its content *and* its boundaries.** `doc_id` says which text a chunk came
from; `chunking_key` says where it was cut. Both are in the chunk row's key (`infra/sql/041`),
because two shares can hold one document and chunk it differently, and keying on the content alone
made them fight over the same rows — the coarser share's write took ordinal 0 and deleted the finer
share's remaining fifteen. Two chunkings of one document coexist; four copies at *one* chunking
still share one set of chunks and one embedding call, which is the property the two-table split
exists for. A cutting no file row claims any more is an orphan, and is swept.

**A hit is cited by path, not by hash.** `doc-9f2a...` is not something a chemist can open, so the
search resolves each hit back to a file path. When several paths hold the same content the smallest
one is cited deterministically — an arbitrary choice, but a stable one, which is what a citation
needs.
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
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.fulltext import TSQUERY_TERMS, reference_terms, reference_tokens
from chemclaw.ingest.documents.binding import DocumentShareError
from chemclaw.ingest.documents.chunk import Chunk


class DocumentIndexError(SubsystemUnavailableError):
    """The document index could not be reached, so the search never ran.

    A `SubsystemUnavailableError` and deliberately **not** a `ChemclawError`, which is this
    repository's *non-retryable bad-data* contract: a statement timeout says nothing about the
    query, and the identical call succeeds once the database is back. Registering it as bad data
    would make an activity give up on a blip it would otherwise ride out — the argument
    `SubsystemUnavailableError` was created for, and the reason `tests/test_publish.py` asserts
    that hierarchy's *absence* from `_BAD_DATA_TYPES`.

    The message stays free of hostnames and driver text; the underlying `psycopg.Error` carries
    those as `__cause__`, for the log and the operator.
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
    # When this run saw the file — the mark half of `prune_stale`'s mark-and-sweep. The Postgres
    # backend stamps its `indexed_at` column server-side with `now()` and ignores this value, so
    # the sweep compares one clock (the database's) rather than the worker's against it; the
    # in-memory backend has only this one.
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

    model_config = {"arbitrary_types_allowed": True}


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
    # Bounded here rather than trusted, because this is where two backends with different scoring
    # meet one `EvidenceChunk` contract that requires [0, 1]. The in-memory reference counted
    # shared tokens and produced 2.0, which a caller only found out about as a validation error
    # three layers up; a cosine and a `ts_rank` happen to be in range and hid it.
    score: float = Field(ge=0.0, le=1.0)


@runtime_checkable
class DocumentIndex(Protocol):
    """Persistence + dense/lexical search over one or more mounted shares."""

    async def fingerprints(
        self, source: str, paths: list[str], chunking_key: str
    ) -> dict[str, str]:
        """The stored `path -> fingerprint` for these paths of `source`, chunked as `chunking_key`.

        What the sync diffs the current filesystem stat against to decide which files must be
        re-read. A path with no entry reads as "changed", exactly like a real mismatch. Scoped to
        the paths one bounded chunk actually crawled, because on a 500k-file share the unscoped
        answer is a dictionary nobody needs and every chunk would rebuild it.

        Scoped to the chunking too, because that is the only gate that can see a chunk-size change:
        the file's `mtime_ns:size` does not move when a setting does, so a row cut under different
        boundaries has to read as "changed" or the document is never re-chunked at all.
        """
        ...

    async def known_documents(self, doc_ids: set[str], key: str, chunking_key: str) -> set[str]:
        """Which of these documents have **at least one** chunk under both configurations.

        Keyed on the embedding configuration, not merely on presence: a document indexed by a
        previous model must be re-embedded even though its content is unchanged, or a copy arriving
        under a new path would inherit a vector nothing else in the corpus is comparable to. And on
        the chunking, because the boundaries decide what each vector describes — a document whose
        content hash is unchanged still needs cutting again when they move.

        "At least one", not "all", and both backends agree on that: measured, a document with one
        of five chunks moved to a new key reports as known, leaving four stale. That is correct
        *here* because this gate answers "must the crawl re-read and re-embed this file", and the
        remaining four are the per-chunk drain's job — `stale_chunks` found exactly those four, and
        `DocumentSyncWorkflow` drains before it crawls. A caller that reordered the two phases, or
        made the drain partial, would inherit a real bug; that is a `docs/planning/BACKLOG.md` row
        with a trigger rather than a stronger predicate here, because per-document completeness
        would re-embed a whole file to fix one chunk.
        """
        ...

    async def upsert(self, files: list[FileRecord], chunks: list[ChunkRecord], key: str) -> None:
        """Insert or replace file rows by path and chunk rows by `(doc_id, chunking_key, ordinal)`.

        `key` is the embedding configuration these vectors were produced by
        (`chemclaw.core.embeddings.embedding_config_key`) and is stored with each chunk, so a later
        run can tell whether it is still comparable to a fresh query. The chunking is on the rows
        themselves, because it is part of which row each one *is*.

        **A cutting nothing claims any more is deleted, in the same write.** After the file rows
        land, every chunk set of the documents just written that no file row names is removed:
        re-chunking a document leaves its previous cutting behind otherwise — rows nothing points
        at, which `reembed_stale` then re-embeds under the current key and makes indistinguishable
        from live ones. Measured: re-cutting one document at 400 → 4000 chars left 19 such rows
        beside its 2 real ones. Scoped to *unclaimed* cuttings rather than to "this document's
        other ordinals", because another share may hold the same document at its own chunk size and
        deleting by content alone destroyed that share's chunks permanently.
        """
        ...

    async def stale_chunks(self, key: str, limit: int, chunkings: set[str]) -> list[StaleChunk]:
        """Up to `limit` chunks cut by one of `chunkings` whose vector was not made by `key`.

        NULL counts as stale — a row written before the key column existed is "unknown", and
        unknown must never read as "current" (the argument `infra/sql/035` makes for its own
        added column).

        `chunkings` is the set of chunkings the *enabled* shares currently use, and it is what
        keeps an upgrade from paying twice. A row cut under any other chunking is already going to
        be re-parsed, re-cut and re-embedded by the crawl, so re-embedding it here is work that is
        then thrown away — measured at 17 embedding calls for a document worth 1 on a run where
        both the model and the chunk size moved, which is exactly what 038 and 040 do together.
        It is not fixable by stamping the chunking during a re-embed: the chunking is part of the
        row's identity (041) and a re-embed does not re-cut anything.
        """
        ...

    async def store_embeddings(self, chunks: list[ChunkRecord], key: str) -> None:
        """Replace the vector and key of existing chunks, leaving content and coordinate alone."""
        ...

    async def stored_document(
        self, source: str, doc_id: str, chunking_key: str
    ) -> StoredDocument | None:
        """This document as stored under one cutting, or `None` when this share does not hold it.

        The read half of what `upsert` writes, and the only way back to a whole protocol: the
        parsed text is discarded once `doc_id` is taken from it, so these rows *are* the document.
        Scoped by `source` and gated on the same file-row eligibility a search uses, so a caller
        cannot read a document out of a share it was never entitled to search.

        Pieces are `chunk.Chunk` rather than `ChunkRecord` — the same shape the cutter produced,
        and deliberately without the vector. A whole-document read wants text; carrying 1,536
        floats a piece for it would be the largest part of the payload and none of the answer.
        """
        ...

    async def touch(self, source: str, paths: list[str]) -> None:
        """Mark these already-current paths as seen by this run, without re-reading them.

        The mark half of the mark-and-sweep `prune_stale` completes. It is one statement per
        crawl chunk rather than a fingerprint dictionary held across a whole drain, which is what
        keeps the sweep affordable on a share far larger than memory.
        """
        ...

    async def prune_stale(self, source: str, before: datetime) -> int:
        """Delete `source` rows not seen since `before`, and any chunk set no file row claims.

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
        """Return up to `top_k` chunks best matching the terms in `query`, best first.

        **The same one boolean rule the note index states** (`chemclaw.core.fulltext`): a chunk
        matching every term outranks one matching some, a chunk matching some is still a hit, and a
        chunk carrying a `-excluded` term is not a hit at all. Both backends, because this is the
        divergence PR #173 fixed for notes and left standing here — the durable statement ANDed the
        terms while the in-memory reference OR'd them, so an ordinary multi-word question about the
        share returned nothing from the database and everything from the tests.
        """
        ...


def require_schema_vector_width() -> None:
    """Refuse a deployment whose `embedding_dim` cannot fit the column it would write.

    **Why here and not in the config validator.** `note_index`'s equivalent check lives there
    because `vector`/`lexical` are *shipped* source names, so `NOTE_INDEX_SOURCES` can enumerate
    them. A document share's name is chosen by the deployment — `sharedrive` is only the shipped
    example, and a site mounts its own manifest folder under whatever name it likes — so no name
    set can identify one. Answering "is a share enabled?" means importing its retrieve half, and
    `chemclaw.core` may import no sibling (`tests/test_layering.py`).

    So the guard sits on the two constructors instead, which between them cover every path that
    can reach the column: the first query, the first crawl, and
    `validate_datasources --construct`. It fires at first use rather than at process start — a
    stated residual, and still a message naming both numbers instead of a pgvector type error
    surfacing from inside a worker hours after a clean-looking deploy.

    Raises:
        DocumentShareError: `embedding_dim` disagrees with the migrated column width.
    """
    # Inert wherever the vectors do not live in that column. An external store's deployment may
    # legitimately run a 768-wide model, and refusing it over a column nothing writes would be this
    # check inventing a constraint instead of reporting one.
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

    `a_norm` lets a caller scanning many `b`s against one fixed `a` hand in the norm it already
    computed, instead of this recomputing it per comparison (`search_dense`). Omitted, it is
    computed here, so every other caller is unchanged.

    Clamped to [0, 1] like the Postgres backend does (`_run`), because floating-point rounding puts
    the *identical* vector's self-similarity above 1.0 about half the time — the denominator is two
    square roots and rounds below the numerator. Measured: 996 of 2000 random normalised vectors,
    worst 1.0000000000000002. `DocumentHit.score` is bounded `le=1.0`, so an exact match (a chemist
    pasting a sentence back, or any token-set collision under the `hash` embedder) raised
    `ValidationError` from inside the reference implementation every test validates against.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    left = a_norm if a_norm is not None else math.sqrt(sum(x * x for x in a))
    norm = left * math.sqrt(sum(y * y for y in b))
    return min(1.0, max(0.0, dot / norm)) if norm else 0.0


class InMemoryDocumentIndex:
    """Process-local `DocumentIndex` for tests and single-run use (the reference ranking).

    Dense search is exact cosine — the ordering `PostgresDocumentIndex` produces with pgvector's
    `<=>` (up to HNSW recall). Lexical search is a shared-token count, a deterministic proxy of
    `ts_rank`: the intent (more shared terms rank higher) matches, the exact scores do not.
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

        The in-memory mirror of `CLAIMED_SQL`. A chunk set outside it belongs to no path on any
        share: a superseded chunk size, or a document whose last file row was swept.
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
        # Written *after* the file rows, so "claimed" is read against what this write just said.
        # Scoped to the documents it touched: a re-chunk supersedes its own previous cutting, and
        # every other share's cutting of the same document is still claimed and survives.
        touched = {file.doc_id for file in files} | {chunk.doc_id for chunk in chunks}
        claimed = self._claimed()
        for row in [k for k in self._chunks if k[0] in touched and (k[0], k[1]) not in claimed]:
            del self._chunks[row]
            self._keys.pop(row, None)

    async def stored_document(
        self, source: str, doc_id: str, chunking_key: str
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
        return StoredDocument(
            doc_id=doc_id,
            # `min` mirrors `CITATION_SQL`, so the reference backend cites what Postgres cites.
            path=paths[0],
            pieces=[
                Chunk(ordinal=r.ordinal, content=r.content, coordinate=r.coordinate) for r in rows
            ],
            modified_at=next(
                (f.modified_at for f in self._files.values() if f.path == paths[0]), None
            ),
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

        The chunking is part of the match, not only the document: a share that cuts a document at
        its own size must cite its own chunks, never another share's cutting of the same text.
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

        The citation is resolved once per *document cutting*, not once per chunk: `_citation` scans
        and sorts every known file, and a document contributes many chunks that all resolve to the
        same path — so this was O(chunks × files · log files) where it is O(cuttings × files ·
        log files). The resolution has to happen before the sort rather than after `[:k]`, because a
        chunk with no citable path is *dropped* and the next best hit takes its place.
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

        The query's norm is computed once here rather than inside `_cosine` per chunk — that is a
        1,536-element pure-Python pass repeated for every chunk in the index, for a value that
        cannot change during the scan.
        """
        query_norm = math.sqrt(sum(x * x for x in query_embedding))
        scored = [
            (c, _cosine(query_embedding, c.embedding, a_norm=query_norm))
            for c in self._chunks.values()
        ]
        return self._rank(source, filters, scored, top_k)

    async def search_lexical(
        self, source: str, query: str, top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Rank chunks by how much of the query they carry; drop non-matches and exclusions.

        The *fraction* of the query's wanted terms this chunk carries, not the raw count: a score is
        contractually in [0, 1], and a count is only a ranking within one query length. The fraction
        is also what makes "a complete match first" fall out of the ordering rather than needing its
        own sort key — a chunk holding every term scores 1.0.

        A query that only excludes (`-solvent`) has no wanted terms to be a fraction of, so every
        surviving chunk carries all zero of them: a complete match, scored 1.0. Anything less would
        be dropped by `_rank`'s zero floor and the durable backend — which does return those rows —
        would be answering a different question again.
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


# What makes a chunk row live at all: some file row, on any share, names both its document *and*
# its cutting. One definition, used by the sweep and by the per-write cleanup, because "orphan"
# has to mean the same thing in both or one of them deletes rows the other keeps. Public because
# `external_index.py`'s sweep must delete exactly the same rows, and then remove their vectors from
# the other system — two spellings of "orphan" across two stores is how they come to disagree.
CLAIMED_SQL = (
    "EXISTS (SELECT 1 FROM document_files f "
    "WHERE f.doc_id = c.doc_id AND f.chunking_key = c.chunking_key)"
)
# The file-row predicate both searches share: a chunk is eligible when at least one path in this
# source holds it, was indexed under the same chunking, and satisfies the filters. `EXISTS` rather
# than a join, so a document copied into four folders contributes one row rather than four
# competing for the same top-k slots. The chunking clause is what keeps a share citing its own
# cutting when another share holds the same document at a different chunk size.
#
# Written once and shared by both, rather than spelled twice: eligibility and citation must select
# over the *same* file rows or a chunk becomes searchable while citing a path that no longer
# satisfies the filters. Two copies of a five-clause predicate is a divergence waiting for whichever
# of them gets a sixth clause first.
_FILE_MATCH = (
    "FROM document_files f WHERE f.doc_id = c.doc_id AND f.source = %(src)s "
    "AND f.chunking_key = c.chunking_key "
    "AND (%(tag)s::text IS NULL OR %(tag)s = ANY(f.tags)) "
    "AND (%(since)s::timestamptz IS NULL OR f.modified_at >= %(since)s) "
    "AND (%(until)s::timestamptz IS NULL OR f.modified_at <= %(until)s)"
)
_ELIGIBLE = f"EXISTS (SELECT 1 {_FILE_MATCH}) "
# The citation, resolved in the same statement: the smallest matching path. Deterministic, so a
# repeated question cites the same file rather than alternating between copies.
#
# Public because `external_index.py` resolves its hits with the identical rule after searching an
# external store. Two spellings of "which path does this content get cited as" would be two
# citation policies, and they would diverge the first time either was tuned.
CITATION_SQL = f"(SELECT min(f.path) {_FILE_MATCH}) AS path "
# The same file rows' modification time, for a whole-document read: `max`, because a document
# copied into several folders is as recent as the most recently touched copy of it.
_MODIFIED_SQL = f"SELECT max(f.modified_at) {_FILE_MATCH}"


class PostgresDocumentIndex:
    """Durable `DocumentIndex` over `document_files` + `document_chunks` (`infra/sql/037`).

    Dense search is cosine distance (`<=>`) accelerated by the HNSW `vector_cosine_ops` index;
    lexical search is `ts_rank` over the GIN-indexed `tsvector`. The embedding width is
    `settings.embedding_dim`, which must equal the table's `vector(N)` column —
    `require_schema_vector_width` refuses a mismatch here rather than letting pgvector reject
    every write later.
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
            "to_tsvector('english', %(content)s), %(key)s, %(chunking)s) "
            "ON CONFLICT (doc_id, chunking_key, ordinal) DO UPDATE SET "
            "content = EXCLUDED.content, coordinate = EXCLUDED.coordinate, "
            "embedding = EXCLUDED.embedding, lexeme = EXCLUDED.lexeme, "
            "embedding_key = EXCLUDED.embedding_key"
        )
        # The previous cutting of a document this write re-chunked. Run once at the end of the same
        # transaction — *after* the file rows, so `CLAIMED_SQL` reads what this write just said —
        # so a re-chunk cannot leave rows behind that nothing points at and `reembed_stale` would
        # then adopt as current. Scoped to the documents written, so it is a primary-key range
        # rather than the table scan the sweep does.
        self._drop_unclaimed = (
            f"DELETE FROM document_chunks c WHERE c.doc_id = ANY(%(docs)s) AND NOT {CLAIMED_SQL}"
        )
        # Re-embedding touches the vector and its key and nothing else: the content and coordinate
        # came from the document and did not change, and rewriting the tsvector would be work for
        # an identical result. Addressed by the whole primary key, so re-embedding one share's
        # cutting cannot overwrite another share's row for the same text.
        self._store_embedding = (
            f"UPDATE document_chunks SET embedding = %(emb)s::vector({width}), "
            "embedding_key = %(key)s "
            "WHERE doc_id = %(doc)s AND chunking_key = %(chunking)s AND ordinal = %(ord)s"
        )
        # The whole document, in document order, gated on the same file-row eligibility a search
        # uses — `%(tag)s`/`%(since)s`/`%(until)s` are bound NULL here because a whole-document read
        # is addressed by id rather than filtered, but the *source* and *chunking* clauses of
        # `_ELIGIBLE` still apply: a document is readable from the share that indexed it, under the
        # cutting that share uses, and from nowhere else.
        self._document = (
            f"SELECT c.ordinal, c.content, c.coordinate, {CITATION_SQL}, "
            f"({_MODIFIED_SQL}) AS modified_at FROM document_chunks c "
            f"WHERE c.doc_id = %(doc)s AND c.chunking_key = %(chunking)s AND {_ELIGIBLE}"
            "ORDER BY c.ordinal"
        )
        # `IS DISTINCT FROM`, not `<>`: NULL is every row written before the key column existed,
        # and `<>` would silently pass over exactly those.
        self._stale = (
            "SELECT doc_id, chunking_key, ordinal, content FROM document_chunks "
            "WHERE embedding_key IS DISTINCT FROM %(key)s "
            "AND chunking_key = ANY(%(chunkings)s) "
            "ORDER BY doc_id, chunking_key, ordinal LIMIT %(k)s"
        )
        # The `> 0` floor mirrors the in-memory reference: a zero or negatively-correlated chunk is
        # not a hit. Without it pgvector returns the top-k nearest unconditionally, so a narrow
        # corpus would surface unrelated documents as cited evidence.
        # **The tie-break sorts the k rows, not the table** — the same correction `note_index`
        # needed, and it matters more here: this is the table designed to hold millions of chunks
        # from a 500k-file share, where `note_index` holds thousands. `(doc_id, ordinal)` as a
        # secondary key mirrors the in-memory reference's ordering so the two backends agree, but
        # written into the *inner* `ORDER BY` it makes the ordering underivable from the vector
        # index and the planner abandons `document_chunks_embedding_idx` for a Seq Scan + Sort.
        # As an outer sort over the k rows the inner query already returned, the HNSW index is used
        # and ten rows are quicksorted. Measured on a synthetic 20,000-chunk corpus (one file row
        # each, migrations applied), median of 5: `Limit → Sort → Seq Scan` **228.25 ms** →
        # `Sort → Limit → Index Scan` **2.47 ms**, returning the same ids in the same order on that
        # corpus. "The same ids" is a measurement, not a guarantee: HNSW is approximate, so what the
        # tie-break pins is that the two backends agree on the order of the hits they *do* return,
        # never which rows win a tie at the k-th place — and the inner form did not pin that either.
        self._dense = (
            "SELECT doc_id, ordinal, content, coordinate, score, path FROM ("
            "SELECT c.doc_id, c.ordinal, c.content, c.coordinate, "
            f"1 - (c.embedding <=> %(q)s::vector({width})) AS score, {CITATION_SQL}"
            "FROM document_chunks c WHERE c.embedding IS NOT NULL "
            f"AND 1 - (c.embedding <=> %(q)s::vector({width})) > 0 AND {_ELIGIBLE}"
            f"ORDER BY c.embedding <=> %(q)s::vector({width}) LIMIT %(k)s"
            ") AS hits ORDER BY score DESC, doc_id, ordinal"
        )
        # **The same one boolean rule the note index runs** — `chemclaw.core.fulltext.TSQUERY_TERMS`
        # builds both forms of the query, `any_terms` deciding which chunks match and `all_terms`
        # putting the complete matches on top. This statement was `websearch_to_tsquery` alone,
        # which ANDs, while `InMemoryDocumentIndex` — the reference every share test stands on —
        # scored any chunk sharing a token. That is the identical divergence PR #173 fixed for
        # notes, left in place on the backend that carries the mounted share's evidence, so the
        # one-legged RRF fusion it measured for notes was still happening here. Measured on a
        # four-document corpus, live PostgreSQL 16 / pgvector 0.8.0, "amide coupling solvent
        # screen": this backend returned **0 rows** where the reference returned all four.
        self._lexical = (
            "SELECT c.doc_id, c.ordinal, c.content, c.coordinate, "
            f"ts_rank(c.lexeme, any_terms) AS score, {CITATION_SQL}"
            f"FROM document_chunks c, {TSQUERY_TERMS} "
            f"WHERE c.lexeme @@ any_terms AND {_ELIGIBLE}"
            "ORDER BY (c.lexeme @@ all_terms) DESC, score DESC, c.doc_id, c.ordinal LIMIT %(k)s"
        )

    def _require_vector_column(self) -> None:
        """Refuse a deployment whose `embedding_dim` cannot fit the column this index writes.

        A hook rather than a direct call because it is only true of *this* index. The external-store
        variant writes NULL into that column and reads it never, so the width it was migrated with
        cannot reject anything — and running the check there would refuse a perfectly good 768-wide
        deployment for a column it does not use.
        """
        require_schema_vector_width()

    async def _forget_vectors(self, keys: list[tuple[str, str, int]]) -> None:
        """Told which chunk rows a re-chunk just superseded, so a subclass can drop their vectors.

        A no-op here, because this index's vectors are *in* the rows that were deleted. The hook
        exists for `ExternalVectorDocumentIndex`, whose vectors live in another system and would
        otherwise accumulate forever: every re-chunk deletes the catalogue rows and left the points
        behind, unreachable but never reclaimed. Called after the commit, so a subclass never
        removes vectors for a transaction that then rolled back.
        """

    def _chunk_vector(self, chunk: ChunkRecord) -> str | None:
        """The pgvector literal to store for this chunk, or `None` to leave the column NULL.

        The one place `upsert` decides whether the embedding lands in Postgres at all. The
        external-store variant returns `None` — `NULL::vector(N)` is valid whatever `N` is — so it
        inherits the transaction, its ordering rationale and the file-row write without copying
        twenty lines of it.
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

        Scoped to the crawl chunk's own paths rather than to the whole source: the unscoped query
        on a 500k-file share returns a dictionary the caller has no use for and would rebuild on
        every chunk of the drain. And to the chunking those rows were cut under, so a file whose
        chunk boundaries are superseded reads as changed and is re-read (NULL — every row written
        before migration 040 — matches no key, which is why the first sync after it re-parses).
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

        Order matters on a crash: a file row whose chunks are missing would be skipped by the next
        crawl (its fingerprint matches) and would contribute nothing forever. Chunks with no file
        row are merely invisible until the file row lands. The cleanup comes last for a different
        reason — it asks which cuttings are still claimed, and the answer must include the file
        rows this very write moved to a new chunking.
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
                # reachable through a copy elsewhere on the share, and deleting by `doc_id` would
                # silently un-index a file nobody touched — the write path's own predicate.
                await cur.execute(f"DELETE FROM document_chunks c WHERE NOT {CLAIMED_SQL}")
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

    async def _run(
        self, statement: str, params: dict[str, object], *, vector_recall: bool = False
    ) -> list[DocumentHit]:
        """Execute a ranked search and build hits, dropping any whose citation resolved to NULL.

        `vector_recall` puts the configured pgvector recall parameters on this statement's own
        transaction (`db.apply_vector_recall_settings`). Off for the lexical leg, whose `ts_rank`
        over a GIN index is exact and has no such parameter, and on for the dense one — which is
        the path those knobs were named for and, until now, the one path that never read them.
        `settings.hnsw_ef_search`'s own documentation cites a residual on *this* index, so a knob
        wired only into the note index left its stated reason untouched. What it governs here is
        real and measured (the eligibility `EXISTS` stays a semi join *above* the HNSW scan); the
        residual itself did not reproduce on a 20,000-chunk corpus. Both measurements are in
        `db.apply_vector_recall_settings`.

        Args:
            statement: The ranked search to run.
            params: Its bound parameters.
            vector_recall: Whether this statement takes an HNSW scan worth parametrizing.

        Raises:
            DocumentIndexError: The backend could not answer. Wrapped rather than left as
                `psycopg.Error`, which descends from `Exception` and not from `OSError`, so the
                retriever's "never raises" handler did not catch it: a statement timeout on a large
                share propagated out through `gather_evidence`'s `asyncio.gather` and failed the
                whole turn, taking the knowledge graph's answer with it. `db.connection` converts
                only *connect-time* failures to `ConnectionError`; anything `execute` raises came
                straight through. This is the wrapper type `WarehouseQueryError` gives the retriever
                that copied this pattern.

                **The message carries none of the driver's text**, which is the contract
                `SubsystemUnavailableError` states and this raiser did not keep: it was
                `f"document search failed: {exc}"` around a `psycopg.Error`, whose string is
                "connection to server at "…", port 5432 failed: …". `api/middleware`'s handler
                relays a `SubsystemUnavailableError`'s message to the HTTP client verbatim,
                precisely *because* the contract says there is nothing in it to leak. Nothing
                reaches that handler from here today — both retrievers swallow this type — so this
                was a contract one raiser did not keep rather than a live leak, and the fix is the
                one line that makes the promise true wherever the type travels next. The detail is
                not lost: it is the `__cause__`, which both handlers that see this type log with
                `exc_info` — the retriever at DEBUG, `api/middleware` at WARNING.
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
                # Clamped: cosine similarity is already in range, but `ts_rank` sums per-term
                # weights and is only *usually* below 1. A score is a ranking within this source,
                # so clipping the rare outlier changes no order and keeps the DTO's contract.
                score=min(1.0, max(0.0, float(row[4]))),
                path=row[5],
            )
            for row in rows
            if row[5]
        ]

    async def stored_document(
        self, source: str, doc_id: str, chunking_key: str
    ) -> StoredDocument | None:
        """This document as stored, when some path on `source` still holds it under this cutting."""
        params: dict[str, Any] = {
            "doc": doc_id,
            "chunking": chunking_key,
            "src": source,
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
        if not rows or not rows[0][3]:
            return None
        return StoredDocument(
            doc_id=doc_id,
            path=rows[0][3],
            pieces=[Chunk(ordinal=r[0], content=r[1], coordinate=r[2]) for r in rows],
            modified_at=rows[0][4],
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
        return await self._run(self._dense, params, vector_recall=True)

    async def search_lexical(
        self, source: str, query: str, top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Rank chunks by full-text `ts_rank` against the terms in `query`."""
        params = self._params(source, top_k, filters)
        params["q"] = query
        return await self._run(self._lexical, params)


def default_document_index() -> DocumentIndex:
    """The production document index — one place the retriever and the sync get their backend.

    Two shapes, chosen by `vector_store_provider`. `pgvector` (the default) keeps the vectors in the
    same statement that resolves the citation, which is the fastest arrangement and the one every
    existing deployment runs. Any other provider composes the same Postgres catalogue with an
    external vector store — see `external_index.py` for what moves and what deliberately does not.

    The external branch is imported inside it, so a default deployment never loads the adapter and
    never needs the client package it would ask for.
    """
    if settings.vector_store_provider == "pgvector":
        return PostgresDocumentIndex()
    from chemclaw.ingest.documents.external_index import ExternalVectorDocumentIndex
    from chemclaw.retrieval.vectors.registry import default_vector_store

    return ExternalVectorDocumentIndex(default_vector_store())
