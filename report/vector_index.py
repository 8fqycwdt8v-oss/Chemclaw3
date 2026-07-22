"""Derived note index for hybrid retrieval — dense + lexical entry points (plan F10-A2).

The knowledge graph is found today by wikilink traversal + substring match and by structural
fingerprints; neither ranks a note by *semantic* similarity or by weighted *term* match. This
module adds those two entry points over a derived index of the notes — `search_dense` (cosine over
an embedding) and `search_lexical` (Postgres full-text `ts_rank`) — while the git-markdown graph
stays the source of truth (D-004): the index is rebuildable at any time from the notes.

Two backends behind one `NoteIndex` interface, exactly as the fingerprint store does it
(`mcp_servers.fpstore`): `InMemoryNoteIndex` computes the ranking in Python (the reference the
tests use, no database), `PostgresNoteIndex` persists to `note_index` (`infra/sql/010`) and ranks
in SQL. Dense ranking is identical across backends (both cosine); the in-memory lexical
rank is a simple token-overlap proxy of Postgres `ts_rank` (same ordering intent, not identical
scores), noted where it is defined.
"""

import asyncio
import math
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from agents.embedding_provider import embed_texts
from chemclaw import db
from chemclaw.config import settings
from kg.graph import load_notes
from kg.note import Note

# Lexical tokenizer for the in-memory backend (lowercase alphanumeric runs) — the offline proxy of
# Postgres `to_tsvector`; the durable backend uses real FTS, this only needs the same ordering.
_TOKEN = re.compile(r"[a-z0-9]+")


class NoteRecord(BaseModel):
    """One indexed note: its id, the text that was embedded/tokenized, and its dense embedding."""

    note_id: str = Field(min_length=1)
    text: str
    embedding: list[float]


class IndexHit(BaseModel):
    """A retrieval hit: a note id and its score (cosine similarity, or lexical rank)."""

    note_id: str
    score: float


def note_text(note: Note) -> str:
    """The text indexed for a note: its id, tags, and body (the graph retriever's haystack).

    One definition so the dense embedding, the lexical tsvector, and the substring graph search all
    see the same text and cannot drift in what "the note's content" means.
    """
    return f"{note.id} {' '.join(note.tags)} {note.body}".strip()


@runtime_checkable
class NoteIndex(Protocol):
    """Persistence + dense/lexical search over the note corpus. Backends implement this."""

    async def upsert(self, records: list[NoteRecord]) -> None:
        """Insert or replace index rows by note id."""
        ...

    async def search_dense(self, query_embedding: list[float], top_k: int) -> list[IndexHit]:
        """Return up to `top_k` notes most cosine-similar to `query_embedding`, best first."""
        ...

    async def search_lexical(self, query: str, top_k: int) -> list[IndexHit]:
        """Return up to `top_k` notes best matching the terms in `query`, best first."""
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

    async def search_dense(self, query_embedding: list[float], top_k: int) -> list[IndexHit]:
        """Rank notes by cosine similarity to the query; drop zero-similarity, tie-break by id."""
        hits = [
            IndexHit(note_id=r.note_id, score=_cosine(query_embedding, r.embedding))
            for r in self._records.values()
        ]
        hits = [h for h in hits if h.score > 0.0]
        hits.sort(key=lambda h: (-h.score, h.note_id))
        return hits[:top_k]

    async def search_lexical(self, query: str, top_k: int) -> list[IndexHit]:
        """Rank notes by shared-token count with the query; drop non-matches, tie-break by id."""
        query_tokens = set(_TOKEN.findall(query.lower()))
        hits: list[IndexHit] = []
        for record in self._records.values():
            overlap = len(query_tokens & set(_TOKEN.findall(record.text.lower())))
            if overlap:
                hits.append(IndexHit(note_id=record.note_id, score=float(overlap)))
        hits.sort(key=lambda h: (-h.score, h.note_id))
        return hits[:top_k]


def _vector_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal (`[a,b,c]`), cast `::vector(N)` in SQL."""
    return "[" + ",".join(str(component) for component in embedding) + "]"


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
            "INSERT INTO note_index (note_id, embedding, lexeme, updated_at) "
            f"VALUES (%(id)s, %(emb)s::vector({width}), "
            "to_tsvector('english', %(text)s), now()) "
            "ON CONFLICT (note_id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, lexeme = EXCLUDED.lexeme, updated_at = now()"
        )
        # The `> 0` floor mirrors the InMemory reference (`score > 0.0`): a zero/near-zero or
        # negatively-correlated note is not a hit. Without it pgvector returns the top-k nearest
        # unconditionally, so a small corpus would surface unrelated notes as cited evidence — a
        # ranking the tests never see. (A zero query vector is short-circuited in `search_dense`
        # before the query, so `<=>` never produces a NaN distance to order by.)
        self._dense = (
            f"SELECT note_id, 1 - (embedding <=> %(q)s::vector({width})) AS score "
            "FROM note_index WHERE embedding IS NOT NULL "
            f"AND 1 - (embedding <=> %(q)s::vector({width})) > 0 "
            # `note_id` secondary sort mirrors the InMemory reference's (-score, note_id) tie-break,
            # so equal-similarity notes order deterministically and identically across backends.
            f"ORDER BY embedding <=> %(q)s::vector({width}), note_id LIMIT %(k)s"
        )
        self._lexical = (
            "SELECT note_id, ts_rank(lexeme, query) AS score "
            "FROM note_index, websearch_to_tsquery('english', %(q)s) AS query "
            "WHERE lexeme @@ query ORDER BY score DESC, note_id LIMIT %(k)s"
        )

    async def _connect(self) -> psycopg.AsyncConnection[TupleRow]:
        """Open a fail-fast connection with the configured per-statement timeout (DRY via db)."""
        return await db.connect(
            self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        )

    async def upsert(self, records: list[NoteRecord]) -> None:
        """Insert or replace each record (embedding + tsvector) by note id."""
        if not records:
            return
        async with await self._connect() as conn:
            for record in records:
                await conn.execute(
                    self._upsert,
                    {
                        "id": record.note_id,
                        "emb": _vector_literal(record.embedding),
                        "text": record.text,
                    },
                )
            await conn.commit()

    async def search_dense(self, query_embedding: list[float], top_k: int) -> list[IndexHit]:
        """Rank notes by cosine similarity to `query_embedding` (pgvector HNSW), positive only."""
        # A zero query vector (a token-less/symbol-only query under the hash embedder) has cosine 0
        # to everything — no hit, exactly as the InMemory reference returns. Short-circuit so we
        # never hand pgvector a zero vector (whose `<=>` distance is NaN) to order by.
        if not any(query_embedding):
            return []
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._dense, {"q": _vector_literal(query_embedding), "k": top_k})
                rows = await cur.fetchall()
        return [IndexHit(note_id=r[0], score=float(r[1])) for r in rows]

    async def search_lexical(self, query: str, top_k: int) -> list[IndexHit]:
        """Rank notes by full-text `ts_rank` against the terms in `query`."""
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._lexical, {"q": query, "k": top_k})
                rows = await cur.fetchall()
        return [IndexHit(note_id=r[0], score=float(r[1])) for r in rows]


def default_note_index() -> PostgresNoteIndex:
    """The production note index (Postgres) — one place the retrievers get their backend."""
    return PostgresNoteIndex()


async def reindex_notes(index: NoteIndex, notes_dir: str | None = None) -> int:
    """(Re)build `index` from the notes on disk; return how many notes were indexed.

    Embeds each note's `note_text` and upserts it. Idempotent (upsert by id), so it is safe to run
    on a schedule or after a merge. Notes deleted from disk leave a harmless stale row — the
    retrievers drop any hit whose note no longer loads — so a full teardown is never required.
    """
    directory = Path(notes_dir if notes_dir is not None else settings.knowledge_dir)
    notes = await asyncio.to_thread(load_notes, directory) if directory.exists() else []
    if not notes:
        return 0
    texts = [note_text(note) for note in notes]
    # embed_texts may call the endpoint (openai_compatible) — offload so the event loop is free.
    embeddings = await asyncio.to_thread(embed_texts, texts)
    records = [
        NoteRecord(note_id=note.id, text=text, embedding=embedding)
        for note, text, embedding in zip(notes, texts, embeddings, strict=True)
    ]
    await index.upsert(records)
    return len(records)


def main() -> int:
    """CLI: rebuild the durable note index from the knowledge graph; print the count."""
    count = asyncio.run(reindex_notes(default_note_index()))
    print(f"indexed {count} note(s) into note_index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
