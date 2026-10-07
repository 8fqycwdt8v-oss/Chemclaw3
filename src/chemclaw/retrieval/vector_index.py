"""Derived note index for hybrid retrieval — dense and lexical entry points.

`search_dense` (cosine over an embedding) and `search_lexical` (Postgres full-text `ts_rank`) over a
derived index of the notes; the git-markdown graph stays the source of truth and the index is
rebuildable from it at any time.

Two backends behind one `NoteIndex` interface: `InMemoryNoteIndex` ranks in Python (the test
reference), `PostgresNoteIndex` persists to `note_index` and ranks in SQL. Dense ranking is
identical across both; the in-memory lexical score is a token-overlap proxy of `ts_rank`, but the
boolean rule (match any term, complete matches first, honour `-term`) is shared exactly via
`chemclaw.core.fulltext`.
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts, embedding_config_key
from chemclaw.core.fulltext import (
    TSQUERY_TERMS,
    normalize_search_text,
    reference_terms,
    reference_tokens,
)
from chemclaw.kg.graph import (
    corpus_revision,
    invalidate_cache,
    load_notes,
    note_file_fingerprints,
    scan_notes_dir,
)
from chemclaw.kg.note import NoteError, read_note
from chemclaw.kg.search import search_text

log = logging.getLogger(__name__)


class NoteRecord(BaseModel):
    """One indexed note: its id, the text that was embedded/tokenized, and its dense embedding.

    `fingerprint` is the content digest (`chemclaw.kg.graph.note_file_fingerprints`) the note's
    file had when this record was embedded — empty when the caller does not track one (every
    offline test that builds a `NoteRecord` directly). `reindex_notes` is the only writer that
    fills it in for real, and it is what makes an incremental rebuild possible: a note whose
    fingerprint has not moved needs no fresh embedding call.
    """

    note_id: str = Field(min_length=1)
    text: str
    embedding: list[float]
    fingerprint: str = ""


class IndexHit(BaseModel):
    """A retrieval hit: a note id and its score (cosine similarity, or lexical rank)."""

    note_id: str
    score: float


@runtime_checkable
class NoteIndex(Protocol):
    """Persistence + dense/lexical search over the note corpus. Backends implement this."""

    async def upsert(
        self,
        records: list[NoteRecord],
        embedding_key: str,
        *,
        corpus_revision: int | None = None,
    ) -> None:
        """Insert or replace index rows by note id, recording which configuration embedded them.

        `embedding_key` (`embedding_config_key()`) is batch-level, so one upsert never writes two
        generations of vector. `corpus_revision` is the commit count of the checkout the notes came
        from; it is stored because `retire_absent`'s `built_before` is compared against it and a
        later pass on another pod cannot recover it.
        """
        ...

    async def retire_absent(self, keep: set[str], *, built_before: int | None = None) -> int:
        """Delete every indexed note whose id is not in `keep`; return how many went.

        "Keep exactly these" because the caller has just listed the corpus on disk and the backend
        can compute the difference itself. Rows built from a corpus revision newer than
        `built_before` are left alone: the index is shared but each pod's checkout is not, so
        absence on one pod's disk cannot distinguish a deleted note from one not yet fetched. `None`
        retires everything absent.

        **An empty `keep` must delete nothing**: a mis-pointed notes directory would otherwise wipe
        the index, and a rebuild costs one embedding call per note.
        """
        ...

    async def fingerprints(self, embedding_key: str) -> dict[str, str]:
        """The stored `note_id -> fingerprint` for notes embedded under `embedding_key`.

        `reindex_notes` diffs on-disk fingerprints against this; a missing entry reads as "changed".
        Scoping to the current configuration makes a model swap self-healing, since a file
        fingerprint cannot see a model change.
        """
        ...

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Return up to `top_k` notes most cosine-similar to `query_embedding`, best first.

        `within` restricts hits to the given note ids (`None` = whole index), applied in the
        backend's own query. On an approximate backend (pgvector HNSW) it filters the candidate list
        rather than bounding the scan, so fewer than `top_k` hits does not mean there were no
        others.
        """
        ...

    async def search_lexical(
        self, query: str, top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Return up to `top_k` notes best matching the terms in `query`, best first.

        A note matching every term outranks one matching only some, a partial match is still a hit,
        and a note carrying a `-excluded` term is not a hit — one rule for both backends
        (`chemclaw.core.fulltext`), matching `GraphRetriever`. `within` is an exact bound before the
        `LIMIT` here.
        """
        ...


# Version of the note-text derivation behind every stored row, folded into the note-side key:
# `embedding_config_key()` cannot see a change to what text is embedded. Bump it whenever the
# text a fresh index would store differs from what an existing row holds — the composition in
# `kg.search.search_text` *or* the normalisation `upsert` applies. A bump hides old rows from dense
# reads and empties their fingerprints, so the next reindex rewrites them (lexical reads do not
# filter on it).
_NOTE_TEXT_VERSION = "ntv3"


def note_embedding_key() -> str:
    """The embedding identity of a stored note vector: model configuration plus text derivation."""
    return f"{embedding_config_key()}|{_NOTE_TEXT_VERSION}"


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class InMemoryNoteIndex:
    """Process-local `NoteIndex` computing the reference ranking in Python.

    A differential test oracle, not a deployment backend: no configuration returns it. Dense search
    is exact cosine (the same ordering as pgvector up to HNSW recall); lexical search is a
    token-overlap proxy of `ts_rank`, but the boolean rule must not drift — see `search_lexical`.
    """

    def __init__(self) -> None:
        """Start with an empty index, keyed by note id (re-upserting an id replaces it)."""
        self._records: dict[str, NoteRecord] = {}
        self._embedding_keys: dict[str, str] = {}
        self._corpus_revisions: dict[str, int | None] = {}

    async def upsert(
        self,
        records: list[NoteRecord],
        embedding_key: str,
        *,
        corpus_revision: int | None = None,
    ) -> None:
        """Insert or replace each record by note id, under the configuration that embedded it."""
        for record in records:
            self._records[record.note_id] = record
            self._embedding_keys[record.note_id] = embedding_key
            self._corpus_revisions[record.note_id] = corpus_revision

    async def retire_absent(self, keep: set[str], *, built_before: int | None = None) -> int:
        """Drop every record whose note id is not in `keep`; an empty `keep` drops nothing.

        A record built from a corpus revision newer than `built_before` is kept.
        """
        if not keep:
            return 0
        gone = [
            note_id
            for note_id in self._records
            if note_id not in keep and not self._is_newer_than(note_id, built_before)
        ]
        for note_id in gone:
            del self._records[note_id]
            self._embedding_keys.pop(note_id, None)
            self._corpus_revisions.pop(note_id, None)
        return len(gone)

    def _is_newer_than(self, note_id: str, built_before: int | None) -> bool:
        """Was this row built from a corpus revision the caller has not reached?

        Unknown on either side is "no constraint", as the Postgres predicate reads a NULL.
        """
        if built_before is None:
            return False
        stored = self._corpus_revisions.get(note_id)
        return stored is not None and stored > built_before

    async def fingerprints(self, embedding_key: str) -> dict[str, str]:
        """Fingerprints of rows embedded under `embedding_key`; empty ones omitted.

        A superseded-configuration row is omitted too: both mean "no reusable vector on record".
        """
        return {
            r.note_id: r.fingerprint
            for r in self._records.values()
            if r.fingerprint and self._embedding_keys.get(r.note_id) == embedding_key
        }

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank notes by cosine similarity to the query; drop zero-similarity, tie-break by id.

        Scoped to the live embedding configuration, as the Postgres backend is.
        """
        current = note_embedding_key()
        hits = [
            IndexHit(note_id=r.note_id, score=_cosine(query_embedding, r.embedding))
            for r in self._records.values()
            if (within is None or r.note_id in within)
            and self._embedding_keys.get(r.note_id) == current
        ]
        hits = [h for h in hits if h.score > 0.0]
        hits.sort(key=lambda h: (-h.score, h.note_id))
        return hits[:top_k]

    async def search_lexical(
        self, query: str, top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank notes sharing any wanted token, those sharing every one first; tie-break by id.

        The same boolean semantics as `PostgresNoteIndex`, including `-term` exclusion. Tokens stand
        in for lexemes (no stemming or stop-words), so scores differ from `ts_rank`; ordering intent
        and the boolean rule must match.
        """
        wanted, excluded = reference_terms(query)
        if not wanted and not excluded:
            return []
        # (complete, overlap, hit): `complete` leads the sort, as `lexeme @@ all_terms` does in SQL.
        scored: list[tuple[bool, int, IndexHit]] = []
        for record in self._records.values():
            if within is not None and record.note_id not in within:
                continue
            tokens = reference_tokens(record.text)
            if excluded & tokens:
                continue
            overlap = len(wanted & tokens)
            if wanted and not overlap:
                continue
            hit = IndexHit(note_id=record.note_id, score=float(overlap))
            scored.append((overlap == len(wanted), overlap, hit))
        scored.sort(key=lambda entry: (not entry[0], -entry[1], entry[2].note_id))
        return [entry[2] for entry in scored[:top_k]]


def _vector_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal (`[a,b,c]`), cast `::vector(N)` in SQL."""
    return "[" + ",".join(str(component) for component in embedding) + "]"


def _scope_array(within: set[str] | None) -> list[str] | None:
    """A `within` scope as the SQL array parameter: sorted for a stable query, NULL = unscoped."""
    return sorted(within) if within is not None else None


class PostgresNoteIndex:
    """Durable `NoteIndex` backed by Postgres + pgvector over the `note_index` table.

    Dense search is cosine distance (`<=>`) over the HNSW `vector_cosine_ops` index; lexical search
    is `ts_rank` over the GIN-indexed `tsvector`. `settings.embedding_dim` must equal the table's
    `vector(N)` width, or inserts raise.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Bind to the configured DSN and the configured embedding width."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        width = settings.embedding_dim
        self._upsert = (
            "INSERT INTO note_index "
            "(note_id, embedding, lexeme, fingerprint, embedding_key, updated_at, "
            "corpus_commit_count) "
            f"VALUES (%(id)s, %(emb)s::vector({width}), "
            "to_tsvector('english', %(text)s), %(fp)s, %(key)s, now(), %(corpus)s) "
            "ON CONFLICT (note_id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, lexeme = EXCLUDED.lexeme, "
            "fingerprint = EXCLUDED.fingerprint, embedding_key = EXCLUDED.embedding_key, "
            "updated_at = now(), corpus_commit_count = EXCLUDED.corpus_commit_count"
        )
        # The `> 0` floor mirrors the in-memory reference: without it pgvector returns the top-k
        # nearest unconditionally and a small corpus would cite unrelated notes. The `within` scope
        # is
        # in the SQL (NULL = unrestricted); with HNSW in use it is a post-filter over the candidate
        # list,
        # so a selective scope can return fewer than k (hence "mostly" in `NoteIndex.search_dense`).
        # `settings.hnsw_ef_search` / `hnsw_iterative_scan` trade latency for recall, applied per
        # query
        # by `db.apply_vector_recall_settings`; both default to leaving the server alone.
        scope = "AND (%(ids)s::text[] IS NULL OR note_id = ANY(%(ids)s::text[])) "
        # The `note_id` tie-break sorts the k rows in the outer query, not the table: in the inner
        # ORDER BY it stops the planner using the vector index. It makes equal-similarity order
        # match
        # the in-memory reference. `embedding_key` is a read predicate too: rows from another model
        # of
        # the same width would otherwise be scored against the new model's query.
        self._dense = (
            "SELECT note_id, score FROM ("
            f"SELECT note_id, 1 - (embedding <=> %(q)s::vector({width})) AS score "
            "FROM note_index WHERE embedding IS NOT NULL AND embedding_key = %(key)s "
            f"AND 1 - (embedding <=> %(q)s::vector({width})) > 0 "
            f"{scope}"
            f"ORDER BY embedding <=> %(q)s::vector({width}) LIMIT %(k)s"
            ") AS hits ORDER BY score DESC, note_id"
        )
        # Match any term; rank notes matching every term first; honour a `-term` exclusion — the
        # rule
        # `chemclaw.core.fulltext` builds once and `InMemoryNoteIndex.search_lexical` states. The
        # widening is over the parsed query's clauses, not its lexemes, so negation survives. One
        # statement rather than query-then-retry: the `lexeme @@ all_terms` sort key guarantees
        # complete
        # matches lead.
        self._lexical = (
            "SELECT note_id, ts_rank(lexeme, any_terms) AS score "
            f"FROM note_index, {TSQUERY_TERMS} "
            f"WHERE lexeme @@ any_terms {scope}"
            "ORDER BY (lexeme @@ all_terms) DESC, score DESC, note_id LIMIT %(k)s"
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout.

        Pooled when the process opened a pool, a dedicated connect otherwise. An unreachable
        database reports "Postgres unreachable at <host>", and a hung query is cancelled.
        """
        async with db.connection(self._dsn) as conn:
            yield conn

    def _row_vector(self, record: NoteRecord) -> str | None:
        """The value bound into this row's `embedding` column — the vector, here.

        A hook so a subclass storing vectors elsewhere can bind `NULL` while sharing the one `INSERT
        … ON CONFLICT` statement.
        """
        return _vector_literal(record.embedding)

    async def upsert(
        self,
        records: list[NoteRecord],
        embedding_key: str,
        *,
        corpus_revision: int | None = None,
    ) -> None:
        """Insert or replace each record (embedding + tsvector + fingerprint + key) by note id."""
        if not records:
            return
        async with self._connection() as conn:
            for record in records:
                await conn.execute(
                    self._upsert,
                    {
                        "id": record.note_id,
                        "emb": self._row_vector(record),
                        # Normalised on the way in, exactly as the query is on the way out:
                        # `chemclaw.core.fulltext` owns that rule and both sides must apply it.
                        "text": normalize_search_text(record.text),
                        "fp": record.fingerprint or None,
                        "key": embedding_key,
                        "corpus": corpus_revision,
                    },
                )
            await conn.commit()

    async def retire_absent(self, keep: set[str], *, built_before: int | None = None) -> int:
        """Delete rows for notes no longer on disk, returning the ids so a subclass can follow.

        Ids rather than a count: `ExternalVectorNoteIndex` removes the matching points from its
        store.
        """
        return len(await self._retire_absent_ids(keep, built_before=built_before))

    async def _retire_absent_ids(
        self, keep: set[str], *, built_before: int | None = None
    ) -> list[str]:
        """The shared half: delete and report which ids went. Empty `keep` deletes nothing.

        A NULL `built_before` on either side prunes; only a row that states a newer corpus than the
        caller holds is protected. The `::int` casts let Postgres type the parameter even when it is
        NULL, and the `IS NULL` arms are spelled out because three-valued logic would make a folded
        `NOT (... AND ...)` protect NULL rows.
        """
        if not keep:
            return []
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM note_index WHERE NOT (note_id = ANY(%(keep)s)) "
                    "AND (corpus_commit_count IS NULL "
                    "OR %(before)s::int IS NULL "
                    "OR corpus_commit_count <= %(before)s::int) "
                    "RETURNING note_id",
                    {"keep": sorted(keep), "before": built_before},
                )
                rows = await cur.fetchall()
            await conn.commit()
        return [row[0] for row in rows]

    def _read_key(self) -> str:
        """The `embedding_key` a *read* must match — the live configuration, as stored.

        A hook because `ExternalVectorNoteIndex` namespaces the key it writes by store and
        collection.
        """
        return note_embedding_key()

    async def fingerprints(self, embedding_key: str) -> dict[str, str]:
        """Stored fingerprints for every row that has one *and* was embedded under this key.

        NULL in either column reads as unknown, i.e. changed, never as a stale match.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT note_id, fingerprint FROM note_index "
                    "WHERE fingerprint IS NOT NULL AND embedding_key = %(key)s",
                    {"key": embedding_key},
                )
                rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank notes by cosine similarity to `query_embedding` (pgvector HNSW), positive only.

        Only rows the current embedding configuration produced are scored.
        """
        # A zero query vector has cosine 0 to everything (no hit, as in the reference);
        # short-circuit
        # so pgvector never orders by a NaN distance.
        if not any(query_embedding):
            return []
        params = {
            "q": _vector_literal(query_embedding),
            "k": top_k,
            "ids": _scope_array(within),
            "key": self._read_key(),
        }
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                # Same transaction as the search below, which is the only place they mean anything:
                # they parametrize the HNSW index scan this statement takes.
                await db.apply_vector_recall_settings(cur)
                await cur.execute(self._dense, params)
                rows = await cur.fetchall()
        return [IndexHit(note_id=r[0], score=float(r[1])) for r in rows]

    async def search_lexical(
        self, query: str, top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank notes by full-text `ts_rank` against the terms in `query`."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._lexical,
                    {
                        "q": normalize_search_text(query),
                        "k": top_k,
                        "ids": _scope_array(within),
                    },
                )
                rows = await cur.fetchall()
        return [IndexHit(note_id=r[0], score=float(r[1])) for r in rows]


def default_note_index() -> NoteIndex:
    """The production note index — one place the retrievers get their backend.

    Chosen by `vector_store_provider`: `pgvector` (default) keeps vectors in `note_index`; any other
    provider moves only the dense half to that store and keeps text, `tsvector`, fingerprint and
    embedding key in `note_index`.
    """
    if settings.vector_store_provider == "pgvector":
        return PostgresNoteIndex()

    from chemclaw.retrieval.external_note_index import ExternalVectorNoteIndex
    from chemclaw.retrieval.vectors.registry import default_vector_store

    return ExternalVectorNoteIndex(default_vector_store())


def _needs_embedding(note_id: str, current: dict[str, str], stored: dict[str, str]) -> bool:
    """Whether `note_id` must be (re-)embedded: its file fingerprint differs from the stored one.

    A note the fingerprint scan does not know (filename disagrees with its frontmatter id) is always
    re-embedded: absent means unknown, and unknown means embed. Logged at WARNING because it costs
    an embedding on every run until the filename is fixed.
    """
    fingerprint = current.get(note_id)
    if fingerprint is None:
        log.warning(
            "note %r has no file fingerprint (its filename does not match its id); "
            "re-embedding it on every run until the file is renamed to %r",
            note_id,
            f"{note_id}.md",
        )
        return True
    return fingerprint != stored.get(note_id)


def _notes_that_failed_to_parse(directory: Path, stems: set[str]) -> list[str]:
    """Which of `stems` are notes that failed to parse — not files that were never notes.

    A Markdown file with no frontmatter (e.g. `knowledge/README.md`) is not a note and must not
    raise the alarm. `read_note` is the one definition of both "is a note" and "failed"
    (`NoteError`), so each stem is re-read; the set is normally empty. First path per stem wins,
    matching `note_file_fingerprints` and `_parse_notes`.
    """
    if not stems:
        return []
    unparsed: list[str] = []
    seen: set[str] = set()
    for path, _ in scan_notes_dir(directory):
        if path.stem not in stems or path.stem in seen:
            continue
        seen.add(path.stem)
        try:
            read_note(path)
        except NoteError:
            unparsed.append(path.stem)
    return unparsed


async def reindex_notes(
    index: NoteIndex, notes_dir: str | None = None, *, full: bool = False
) -> int:
    """(Re)build `index` from the notes on disk; return how many notes were (re-)embedded.

    Incremental: a note whose content hash (`note_file_fingerprints`) matches the stored one under
    the current embedding configuration is skipped, so an unchanged corpus embeds nothing and a
    model change re-embeds everything without a flag. The fingerprint is content, not mtime, because
    the index is shared between pods with separate checkouts. `full=True` re-embeds unconditionally.
    Idempotent (upsert by id).

    Notes deleted from disk are retired before the "nothing changed" exit, since an external vector
    store has no other pruner. Both the graph cache and the per-file parse cache are bypassed
    (`reparse=True`), so the parsed bodies and the fingerprints come from the same moment; otherwise
    an old body could be stored under a new digest and never heal.
    """
    directory = Path(notes_dir) if notes_dir is not None else settings.knowledge_path
    await asyncio.to_thread(partial(invalidate_cache, directory, reparse=True))
    # Hash before parsing: a note rewritten between the two passes then pairs a new body with an
    # old digest, which the next pass reads as changed and heals; the reverse order would stick.
    current_fingerprints = (
        await asyncio.to_thread(note_file_fingerprints, directory) if directory.exists() else {}
    )
    notes = await asyncio.to_thread(load_notes, directory) if directory.exists() else []
    if not notes:
        # A missing directory is a deployment fault (unmounted volume, wrong `knowledge_path`) and
        # is
        # reported loudly; a present but empty one is a fresh corpus and stays at DEBUG.
        if not directory.exists():
            log.warning(
                "note re-index found no notes: %s does not exist. Nothing is re-embedded and the "
                "index keeps whatever it last held, so retrieval will answer from a frozen corpus "
                "until the path or the mount is fixed",
                directory,
            )
        else:
            log.debug("note re-index found no notes under %s; nothing to do", directory)
        return 0
    # Guarded against wiping the index: `notes` is non-empty here, `retire_absent` ignores an empty
    # `keep`, and `keep` is the union of what parsed and what is on disk. The on-disk half (which
    # includes unparseable and unreadable files) stops a transient parse failure from deleting rows.
    on_disk = set(current_fingerprints)
    unparsed = await asyncio.to_thread(
        _notes_that_failed_to_parse, directory, on_disk - {note.id for note in notes}
    )
    if unparsed:
        log.warning(
            "note re-index: %d note file(s) on disk did not parse and are kept in the index rather "
            "than retired (%s); they will be re-embedded when they parse again",
            len(unparsed),
            ", ".join(sorted(unparsed)[:5]),
        )
    # A prune is a claim about the corpus, and pods hold different checkouts: rows built from a
    # newer `corpus_revision` than this pass holds are left alone. `None` (no git) is no constraint.
    revision = await asyncio.to_thread(corpus_revision, directory)
    retired = await index.retire_absent(
        {note.id for note in notes} | on_disk, built_before=revision
    )
    if retired:
        log.info("retired %d note(s) no longer on disk", retired)
    embedding_key = note_embedding_key()
    stored_fingerprints = {} if full else await index.fingerprints(embedding_key)
    changed = [
        note
        for note in notes
        if _needs_embedding(note.id, current_fingerprints, stored_fingerprints)
    ]
    if not changed:
        return 0
    # Bounded per note and embedded in batches: a failing batch raises after earlier batches are
    # upserted, so the pass makes partial progress and the next one retries only what is missing.
    indexed = 0
    for start in range(0, len(changed), settings.note_embed_batch_size):
        batch = changed[start : start + settings.note_embed_batch_size]
        texts = [search_text(note)[: settings.note_embed_max_chars] for note in batch]
        # embed_texts may call the endpoint (openai_compatible) — offload so the event loop is free.
        embeddings = await asyncio.to_thread(embed_texts, texts, cache=False)
        records = [
            NoteRecord(
                note_id=note.id,
                text=text,
                embedding=embedding,
                fingerprint=current_fingerprints.get(note.id, ""),
            )
            for note, text, embedding in zip(batch, texts, embeddings, strict=True)
        ]
        await index.upsert(records, embedding_key, corpus_revision=revision)
        indexed += len(records)
    return indexed


def main(argv: list[str] | None = None) -> int:
    """CLI: rebuild the durable note index from the knowledge graph; print the count.

    `--full` re-embeds every note regardless of its stored fingerprint.
    """
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument(
        "--full", action="store_true", help="re-embed every note, ignoring stored fingerprints"
    )
    args = parser.parse_args(argv)
    count = asyncio.run(reindex_notes(default_note_index(), full=args.full))
    print(f"indexed {count} note(s) into note_index" + (" (full rebuild)" if args.full else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
