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
from typing import Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import SCHEMA_VECTOR_DIM, settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.ingest.documents.binding import DocumentShareError


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

        **That argument covers a re-chunk and not the other case the filter also excludes.** A
        share dropped from `CHEMCLAW_DATA_SOURCES` leaves rows no crawl will ever touch again, so
        "the crawl will redo it" is false for them. Skipping them is still right, and for a
        different reason: no search reaches a disabled source either, so a vector nothing can
        return is not worth an embedding call. They go when the share's rows are swept, or the day
        it is re-enabled and re-crawled.
        """
        ...

    async def store_embeddings(self, chunks: list[ChunkRecord], key: str) -> None:
        """Replace the vector and key of existing chunks, leaving content and coordinate alone."""
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
        """Return up to `top_k` chunks most cosine-similar to `query_embedding`, best first.

        Restricted to chunks `source` holds under `filters`, and that restriction is in the
        backend's own query rather than applied to the result, so the k slots are mostly not spent
        on chunks the caller would discard.

        **"Mostly" is the contract: fewer than `top_k` hits does not mean there were no others.**
        The same caveat `NoteIndex.search_dense` carries. On the approximate backend the
        eligibility predicate sits above the inner `LIMIT k`, so it filters the vector index's
        candidate list instead of bounding what the scan considers, and a chunk that would have
        been a hit can be cut before the filter ever sees it. Only `InMemoryDocumentIndex` and
        `search_lexical` are exact.

        **Measured, because the size of this matters and is easy to overstate.** 20,000 chunks,
        20,000 file rows, k=8, 20 queries, pgvector 0.8.0, `hnsw.ef_search=40`,
        `hnsw.iterative_scan=off`, tables `ANALYZE`d, planner's own plan:

            corpus          source 90%        source 9.5%   source 0.5%
            clustered       2/20 short        0/20 short    1/20 short
                            (144/160 rows)    (160/160)     (152/160)
            uniform-random  0/20              0/20          0/20

        So it is real and it is small. **On the same corpora with stale statistics — no `ANALYZE`
        after the load — the same statements went short on 13 of 20 and 20 of 20 queries, returning
        6 rows of a possible 160 at the narrowest.** That is worth knowing on its own: the worst
        behaviour measured here was a planner working from default estimates, not the index being
        approximate, so a corpus loaded in bulk and not analyzed is the case to watch.
        `hnsw.iterative_scan` is the knob that addresses the residual, and it has a
        `docs/planning/BACKLOG.md` row rather than a setting.
        """
        ...

    async def search_lexical(
        self, source: str, query: str, top_k: int, filters: DocumentFilter
    ) -> list[DocumentHit]:
        """Return up to `top_k` chunks best matching the terms in `query`, best first."""
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


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector.

    Clamped to [0, 1] like the Postgres backend does (`_run`), because floating-point rounding puts
    the *identical* vector's self-similarity above 1.0 about half the time — the denominator is two
    square roots and rounds below the numerator. Measured: 996 of 2000 random normalised vectors,
    worst 1.0000000000000002. `DocumentHit.score` is bounded `le=1.0`, so an exact match (a chemist
    pasting a sentence back, or any token-set collision under the `hash` embedder) raised
    `ValidationError` from inside the reference implementation every test validates against.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return min(1.0, max(0.0, dot / norm)) if norm else 0.0


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
        """Resolve each scored chunk to a citation path, drop the unresolvable, take the best k."""
        hits: list[DocumentHit] = []
        for chunk, score in scored:
            if score <= 0.0:
                continue
            path = self._citation(chunk.doc_id, chunk.chunking_key, source, filters)
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


# What makes a chunk row live at all: some file row, on any share, names both its document *and*
# its cutting. One definition, used by the sweep and by the per-write cleanup, because "orphan"
# has to mean the same thing in both or one of them deletes rows the other keeps.
#
# Public for the reason `CITATION_SQL` is: `external_index.py` sweeps the same orphans, and a third
# spelling in that file is not a hypothetical — it had one (`f.doc_id = c.doc_id`, with no chunking
# clause), and it kept a superseded cutting the base class then deleted on the next call.
CLAIMED_SQL = (
    "EXISTS (SELECT 1 FROM document_files f "
    "WHERE f.doc_id = c.doc_id AND f.chunking_key = c.chunking_key)"
)
# Which file rows of this source hold this chunk, under this chunking, within these filters.
#
# **One body, because the two expressions built from it are a contract, not a coincidence.**
# `_ELIGIBLE` decides which chunks compete for the k slots; `CITATION_SQL` decides whether a winner
# is citable. Diverge them and the inner `LIMIT k` fills with rows whose citation resolves to NULL,
# `_run` drops them, and the search returns fewer than k — indistinguishable from the approximate
# shortfall `DocumentIndex.search_dense` documents, and therefore invisible. They were 293
# byte-identical characters written twice.
_FILE_MATCH = (
    "FROM document_files f WHERE f.doc_id = c.doc_id AND f.source = %(src)s "
    "AND f.chunking_key = c.chunking_key "
    "AND (%(tag)s::text IS NULL OR %(tag)s = ANY(f.tags)) "
    "AND (%(since)s::timestamptz IS NULL OR f.modified_at >= %(since)s) "
    "AND (%(until)s::timestamptz IS NULL OR f.modified_at <= %(until)s)"
)
# The file-row predicate both searches share: a chunk is eligible when at least one path in this
# source holds it, was indexed under the same chunking, and satisfies the filters. `EXISTS` rather
# than a join, so a document copied into four folders contributes one row rather than four
# competing for the same top-k slots. The chunking clause is what keeps a share citing its own
# cutting when another share holds the same document at a different chunk size.
_ELIGIBLE = f"EXISTS (SELECT 1 {_FILE_MATCH}) "
# The citation, resolved in the same statement: the smallest matching path. Deterministic, so a
# repeated question cites the same file rather than alternating between copies.
#
# Public because `external_index.py` resolves its hits with the identical rule after searching an
# external store. Two spellings of "which path does this content get cited as" would be two
# citation policies, and they would diverge the first time either was tuned.
CITATION_SQL = f"(SELECT min(f.path) {_FILE_MATCH}) AS path "


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
        #
        # **It names what it deleted.** Deleting a chunk row is only half the deletion when the
        # vectors live in another system, and a subclass cannot delete points a statement never
        # named — which is exactly how this per-write cleanup handed `ExternalVectorDocumentIndex`
        # an obligation it had no way to see (measured: 3 points left behind by a re-chunk that
        # deleted 3 rows). `_forget_vectors` is the other half. Affordable here and deliberately
        # *not* done by the sweep below: this scope is one crawl chunk's documents, that one is
        # every orphan in the table.
        self._drop_unclaimed = (
            f"DELETE FROM document_chunks c WHERE c.doc_id = ANY(%(docs)s) AND NOT {CLAIMED_SQL} "
            "RETURNING c.doc_id, c.chunking_key, c.ordinal"
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
        #
        # **`_ELIGIBLE` sits in the inner `WHERE`, above the `LIMIT k`, which makes it a post filter
        # over the vector index's candidate list rather than a bound on what the scan considers.**
        # The query can therefore return fewer than k. It is measured and quantified in
        # `DocumentIndex.search_dense`'s docstring, which is where a caller reads it — small on an
        # analyzed database, much larger on one whose statistics are stale. A separate property
        # from the tie-break below: two corrections to one statement, with different causes.
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
        self._lexical = (
            "SELECT c.doc_id, c.ordinal, c.content, c.coordinate, "
            f"ts_rank(c.lexeme, query) AS score, {CITATION_SQL}"
            "FROM document_chunks c, websearch_to_tsquery('english', %(q)s) AS query "
            f"WHERE c.lexeme @@ query AND {_ELIGIBLE}"
            "ORDER BY score DESC, c.doc_id, c.ordinal LIMIT %(k)s"
        )

    def _require_vector_column(self) -> None:
        """Refuse a deployment whose `embedding_dim` cannot fit the column this index writes.

        A hook rather than a direct call because it is only true of *this* index. The external-store
        variant writes NULL into that column and reads it never, so the width it was migrated with
        cannot reject anything — and running the check there would refuse a perfectly good 768-wide
        deployment for a column it does not use.
        """
        require_schema_vector_width()

    def _chunk_vector(self, chunk: ChunkRecord) -> str | None:
        """The pgvector literal to store for this chunk, or `None` to leave the column NULL.

        The one place `upsert` decides whether the embedding lands in Postgres at all. The
        external-store variant returns `None` — `NULL::vector(N)` is valid whatever `N` is — so it
        inherits the transaction, its ordering rationale and the file-row write without copying
        twenty lines of it.
        """
        return _vector_literal(chunk.embedding)

    async def _forget_vectors(self, chunks: list[tuple[str, str, int]]) -> None:
        """Delete whatever else addressed these now-deleted chunk rows. Nothing, here.

        The counterpart to `_chunk_vector`: that hook decides where a vector is *written*, this one
        where it is deleted. In this backend the vector is a column of the chunk row, so removing
        the row removed it and there is nothing left to do. The external-store variant deletes the
        points by name — and the reason this is a hook at all is that it could not: every statement
        that deletes chunk rows lives in this class, and a delete that does not say what it removed
        leaves the other system holding vectors nothing will ever address again.

        Called *after* the commit, always, because the catalogue is the record: a point deleted for
        a transaction that then rolled back would leave a chunk row whose vector is gone, which the
        crawl reads as indexed and never repairs.
        """

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
            cur = await conn.execute(self._drop_unclaimed, {"docs": touched})
            orphaned = [(row[0], row[1], row[2]) for row in await cur.fetchall()]
            await conn.commit()
        await self._forget_vectors(orphaned)

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
                # silently un-index a file nobody touched. The same `CLAIMED_SQL` the write
                # path uses.
                #
                # **No `RETURNING` here, unlike `_drop_unclaimed`.** That one is scoped to a crawl
                # chunk's documents; this one is every orphan in the table, and a share removed
                # from `CHEMCLAW_DATA_SOURCES` orphans the whole corpus at once — naming millions
                # of rows to a client that discards them is a memory hazard bought for nothing,
                # since the vector is a column of each row being deleted. The external-store
                # variant overrides this method precisely because *it* has to pay that cost.
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

    async def _run(self, statement: str, params: dict[str, object]) -> list[DocumentHit]:
        """Execute a ranked search and build hits, dropping any whose citation resolved to NULL.

        **The NULL guard cannot fire for either statement in this class**, and that is a property
        of `_FILE_MATCH` rather than an accident: both are built from the same body, `_ELIGIBLE`
        asserts a row satisfying it exists, and `min(path)` over a `NOT NULL` column with a
        non-empty match is never NULL. It stays because `external_index._resolve` composes
        `CITATION_SQL` *without* `_ELIGIBLE` — it filters in the vector store instead — and there a
        swept file row genuinely leaves a chunk with no citable path.

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
