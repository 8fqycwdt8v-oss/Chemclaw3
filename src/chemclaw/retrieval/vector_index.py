"""Derived note index for hybrid retrieval — dense + lexical entry points (plan F10-A2).

The knowledge graph is found today by wikilink traversal + substring match and by structural
fingerprints; neither ranks a note by *semantic* similarity or by weighted *term* match. This
module adds those two entry points over a derived index of the notes — `search_dense` (cosine over
an embedding) and `search_lexical` (Postgres full-text `ts_rank`) — while the git-markdown graph
stays the source of truth (D-004): the index is rebuildable at any time from the notes.

Two backends behind one `NoteIndex` interface, exactly as the fingerprint store does it
(`chemclaw.science.fingerprints.store`): `InMemoryNoteIndex` computes the ranking in Python (the
reference the tests use, no database), `PostgresNoteIndex` persists to `note_index`
(`infra/sql/012`) and ranks
in SQL. Dense ranking is identical across backends (both cosine); the in-memory lexical
rank is a simple token-overlap proxy of Postgres `ts_rank` (same ordering intent, not identical
scores), noted where it is defined.
"""

import asyncio
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.kg.graph import invalidate_cache, load_notes, note_file_fingerprints
from chemclaw.kg.search import search_text

# Lexical tokenizer for the in-memory backend (lowercase alphanumeric runs) — the offline proxy of
# Postgres `to_tsvector`; the durable backend uses real FTS, this only needs the same ordering.
_TOKEN = re.compile(r"[a-z0-9]+")


class NoteRecord(BaseModel):
    """One indexed note: its id, the text that was embedded/tokenized, and its dense embedding.

    `fingerprint` is the stat signature (`chemclaw.kg.graph.note_file_fingerprints`) the note's file
    had when this record was embedded — empty when the caller does not track one (every offline test
    that builds a `NoteRecord` directly). `reindex_notes` is the only writer that fills it in for
    real, and it is what makes an incremental rebuild possible: a note whose fingerprint has not
    moved needs no fresh embedding call.
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

    async def upsert(self, records: list[NoteRecord]) -> None:
        """Insert or replace index rows by note id."""
        ...

    async def fingerprints(self) -> dict[str, str]:
        """The stored `note_id -> fingerprint` for every indexed note (empty fingerprint omitted).

        What `reindex_notes` diffs the current on-disk fingerprints against to decide which notes
        need a fresh embedding call — an unindexed or never-fingerprinted note simply has no entry,
        which reads as "changed" exactly like a real mismatch would.
        """
        ...

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Return up to `top_k` notes most cosine-similar to `query_embedding`, best first.

        `within` restricts hits to the given note ids *before* the top-k cut, so a caller's
        eligibility filter keeps full recall instead of competing with ineligible neighbors for
        the k slots. None means the whole index.
        """
        ...

    async def search_lexical(
        self, query: str, top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Return up to `top_k` notes best matching the terms in `query`, best first.

        `within` scopes the search exactly as in `search_dense`.
        """
        ...


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class InMemoryNoteIndex:
    """Process-local `NoteIndex` for tests and single-run use (the reference ranking).

    Dense search is exact cosine — the same ordering `PostgresNoteIndex` produces with pgvector's
    `<=>` (up to HNSW recall). Lexical search is a token-overlap count, a deterministic proxy of
    Postgres `ts_rank`: the intent (more shared terms rank higher) matches, the exact scores do not.
    """

    def __init__(self) -> None:
        """Start with an empty index, keyed by note id (re-upserting an id replaces it)."""
        self._records: dict[str, NoteRecord] = {}

    async def upsert(self, records: list[NoteRecord]) -> None:
        """Insert or replace each record by note id."""
        for record in records:
            self._records[record.note_id] = record

    async def fingerprints(self) -> dict[str, str]:
        """Stored fingerprints, empty ones omitted (a caller who never set one looks new)."""
        return {r.note_id: r.fingerprint for r in self._records.values() if r.fingerprint}

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank notes by cosine similarity to the query; drop zero-similarity, tie-break by id."""
        hits = [
            IndexHit(note_id=r.note_id, score=_cosine(query_embedding, r.embedding))
            for r in self._records.values()
            if within is None or r.note_id in within
        ]
        hits = [h for h in hits if h.score > 0.0]
        hits.sort(key=lambda h: (-h.score, h.note_id))
        return hits[:top_k]

    async def search_lexical(
        self, query: str, top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank notes by shared-token count with the query; drop non-matches, tie-break by id."""
        query_tokens = set(_TOKEN.findall(query.lower()))
        hits: list[IndexHit] = []
        for record in self._records.values():
            if within is not None and record.note_id not in within:
                continue
            overlap = len(query_tokens & set(_TOKEN.findall(record.text.lower())))
            if overlap:
                hits.append(IndexHit(note_id=record.note_id, score=float(overlap)))
        hits.sort(key=lambda h: (-h.score, h.note_id))
        return hits[:top_k]


def _vector_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal (`[a,b,c]`), cast `::vector(N)` in SQL."""
    return "[" + ",".join(str(component) for component in embedding) + "]"


def _scope_array(within: set[str] | None) -> list[str] | None:
    """A `within` scope as the SQL array parameter: sorted for a stable query, NULL = unscoped."""
    return sorted(within) if within is not None else None


class PostgresNoteIndex:
    """Durable `NoteIndex` backed by Postgres + pgvector over the `note_index` table.

    Dense search is cosine distance (`<=>`) accelerated by the HNSW `vector_cosine_ops` index;
    lexical search is `ts_rank` over the GIN-indexed `tsvector`. The embedding width is
    `settings.embedding_dim`, which must equal the table's `vector(N)` column — a mismatch makes
    Postgres raise on insert (a loud failure, like the fingerprint bit width). One short-lived
    connection per call (KISS, the calc/fingerprint store's choice).
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Bind to the configured DSN and the configured embedding width."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        width = settings.embedding_dim
        self._upsert = (
            "INSERT INTO note_index (note_id, embedding, lexeme, fingerprint, updated_at) "
            f"VALUES (%(id)s, %(emb)s::vector({width}), "
            "to_tsvector('english', %(text)s), %(fp)s, now()) "
            "ON CONFLICT (note_id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, lexeme = EXCLUDED.lexeme, "
            "fingerprint = EXCLUDED.fingerprint, updated_at = now()"
        )
        # The `> 0` floor mirrors the InMemory reference (`score > 0.0`): a zero/near-zero or
        # negatively-correlated note is not a hit. Without it pgvector returns the top-k nearest
        # unconditionally, so a small corpus would surface unrelated notes as cited evidence — a
        # ranking the tests never see. (A zero query vector is short-circuited in `search_dense`
        # before the query, so `<=>` never produces a NaN distance to order by.)
        # The `within` scope lives in the SQL itself (NULL = unrestricted), so a caller's
        # eligibility set bounds the search *before* the LIMIT — the top-k slots are never spent
        # on notes the caller would drop afterwards (the same semantics the InMemory backend has).
        scope = "AND (%(ids)s::text[] IS NULL OR note_id = ANY(%(ids)s::text[])) "
        self._dense = (
            f"SELECT note_id, 1 - (embedding <=> %(q)s::vector({width})) AS score "
            "FROM note_index WHERE embedding IS NOT NULL "
            f"AND 1 - (embedding <=> %(q)s::vector({width})) > 0 "
            f"{scope}"
            # `note_id` secondary sort mirrors the InMemory reference's (-score, note_id) tie-break,
            # so equal-similarity notes order deterministically and identically across backends.
            f"ORDER BY embedding <=> %(q)s::vector({width}), note_id LIMIT %(k)s"
        )
        self._lexical = (
            "SELECT note_id, ts_rank(lexeme, query) AS score "
            "FROM note_index, websearch_to_tsquery('english', %(q)s) AS query "
            f"WHERE lexeme @@ query {scope}ORDER BY score DESC, note_id LIMIT %(k)s"
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout.

        Pooled per process when the process opened a pool (`chemclaw.core.db.pooling`), so a
        request path pays no TCP+auth handshake; a dedicated connect otherwise. Either way a
        down or misconfigured database reports "Postgres unreachable at <host>" rather than a
        raw psycopg traceback, and a hung query is cancelled rather than pinning the enclosing
        activity for its whole budget.
        """
        async with db.bounded(self._dsn) as conn:
            yield conn

    async def upsert(self, records: list[NoteRecord]) -> None:
        """Insert or replace each record (embedding + tsvector + fingerprint) by note id."""
        if not records:
            return
        async with self._connection() as conn:
            for record in records:
                await conn.execute(
                    self._upsert,
                    {
                        "id": record.note_id,
                        "emb": _vector_literal(record.embedding),
                        "text": record.text,
                        "fp": record.fingerprint or None,
                    },
                )
            await conn.commit()

    async def fingerprints(self) -> dict[str, str]:
        """Stored fingerprints for every row that has one.

        NULL (a row written before this column, or by a `--full` rebuild) is left out — either way
        it is "unknown", which reads as changed to `reindex_notes`, never as a stale match.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT note_id, fingerprint FROM note_index WHERE fingerprint IS NOT NULL"
                )
                rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Rank notes by cosine similarity to `query_embedding` (pgvector HNSW), positive only."""
        # A zero query vector (a token-less/symbol-only query under the hash embedder) has cosine 0
        # to everything — no hit, exactly as the InMemory reference returns. Short-circuit so we
        # never hand pgvector a zero vector (whose `<=>` distance is NaN) to order by.
        if not any(query_embedding):
            return []
        params = {"q": _vector_literal(query_embedding), "k": top_k, "ids": _scope_array(within)}
        async with self._connection() as conn:
            async with conn.cursor() as cur:
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
                    self._lexical, {"q": query, "k": top_k, "ids": _scope_array(within)}
                )
                rows = await cur.fetchall()
        return [IndexHit(note_id=r[0], score=float(r[1])) for r in rows]


def default_note_index() -> PostgresNoteIndex:
    """The production note index (Postgres) — one place the retrievers get their backend."""
    return PostgresNoteIndex()


async def reindex_notes(
    index: NoteIndex, notes_dir: str | None = None, *, full: bool = False
) -> int:
    """(Re)build `index` from the notes on disk; return how many notes were (re-)embedded.

    Incremental by default (D-2026-08-02-embed-only-what-changed): a note whose file fingerprint
    (`chemclaw.kg.graph.note_file_fingerprints`, stat-only — no read/parse) matches what `index`
    already has stored is left alone, so a scheduled run against an unchanged corpus embeds nothing.
    Before this, every run — hourly by default (`durable/note_index.py`) — re-embedded every note in
    the knowledge graph regardless of whether anything had changed, one LLM-endpoint call per note
    per hour forever.

    `full=True` re-embeds every note unconditionally (the CLI's `--full`), for recovery from a
    corrupted index or a change to the embedding model/dimension the fingerprint cannot see (that
    staleness — detecting a model change — is the separately tracked backlog item; this is not it).

    Idempotent either way (upsert by id), so it is safe to run on a schedule or after a merge. Notes
    deleted from disk leave a harmless stale row — the retrievers drop any hit whose note no longer
    loads — so a full teardown is never required.

    **Reads past the graph cache deliberately.** This is the one in-process moment that correlates
    with a merge — the PR-gate's merge webhook triggers it — and the note list below is compared
    against a freshly scanned `note_file_fingerprints`. Without the bust the two halves could come
    from different moments: a graph cached before the merge landed, diffed against fingerprints
    read after it, which computes `changed` from a stale set of notes. The cost is one rescan on a
    job that is about to re-embed anyway.
    """
    directory = Path(notes_dir) if notes_dir is not None else settings.knowledge_path
    await asyncio.to_thread(invalidate_cache, directory)
    notes = await asyncio.to_thread(load_notes, directory) if directory.exists() else []
    if not notes:
        return 0
    current_fingerprints = await asyncio.to_thread(note_file_fingerprints, directory)
    stored_fingerprints = {} if full else await index.fingerprints()
    changed = [
        note
        for note in notes
        if current_fingerprints.get(note.id) != stored_fingerprints.get(note.id)
    ]
    if not changed:
        return 0
    texts = [search_text(note) for note in changed]
    # embed_texts may call the endpoint (openai_compatible) — offload so the event loop is free.
    embeddings = await asyncio.to_thread(embed_texts, texts)
    records = [
        NoteRecord(
            note_id=note.id,
            text=text,
            embedding=embedding,
            fingerprint=current_fingerprints.get(note.id, ""),
        )
        for note, text, embedding in zip(changed, texts, embeddings, strict=True)
    ]
    await index.upsert(records)
    return len(records)


def main(argv: list[str] | None = None) -> int:
    """CLI: rebuild the durable note index from the knowledge graph; print the count.

    Incremental by default; `--full` re-embeds every note regardless of its stored fingerprint
    (recovery from a corrupted index, or after an embedding model/dimension change).
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
