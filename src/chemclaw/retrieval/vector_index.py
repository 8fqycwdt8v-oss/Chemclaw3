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
scores), noted where it is defined. What the two lexical backends *do* share exactly is their
boolean rule — match any term, rank the notes matching every term first, honour a `-term` exclusion
— because a reference implementation that answers a multi-word question differently from the
backend it stands in for cannot be tested against. That rule is built once, in
`chemclaw.core.fulltext`, and the document index (`chemclaw.ingest.documents.index`) runs the same
one.
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from chemclaw.kg.graph import invalidate_cache, load_notes, note_file_fingerprints
from chemclaw.kg.search import search_text

log = logging.getLogger(__name__)


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

    async def upsert(self, records: list[NoteRecord], embedding_key: str) -> None:
        """Insert or replace index rows by note id, recording which configuration embedded them.

        `embedding_key` is `chemclaw.core.embeddings.embedding_config_key()` — a batch-level fact,
        not a per-record one, exactly as the document index takes it (`ingest/documents/index.py`),
        so one upsert can never write two generations of vector under one call.
        """
        ...

    async def retire_absent(self, keep: set[str]) -> int:
        """Delete every indexed note whose id is not in `keep`; return how many went.

        Phrased as "keep exactly these" rather than "delete these" because that is what the caller
        knows: `reindex_notes` has just listed the corpus on disk, and asking it to also enumerate
        what the backend holds would be a second round trip to compute a difference the backend can
        compute itself.

        **An empty `keep` must delete nothing.** A missing or mis-pointed notes directory would
        otherwise wipe the index, and a rebuild costs one embedding call per note.

        Until D-2026-08-25 there was no such method and nothing ever pruned: a note deleted from
        disk left a row behind, which is harmless in Postgres — the retrievers drop a hit whose note
        no longer loads — and is not harmless once the vectors live in a store that bills for them
        and that no other sweep reaches.
        """
        ...

    async def fingerprints(self, embedding_key: str) -> dict[str, str]:
        """The stored `note_id -> fingerprint` for notes embedded under `embedding_key`.

        What `reindex_notes` diffs the current on-disk fingerprints against to decide which notes
        need a fresh embedding call — an unindexed, never-fingerprinted, or differently-embedded
        note simply has no entry, which reads as "changed" exactly like a real mismatch would.

        Scoping the read to the current configuration is what makes a model swap self-healing: the
        file fingerprint answers "did the text change" and cannot answer "did the model change",
        so a key-mismatched row must not be allowed to match on the fingerprint alone.
        """
        ...

    async def search_dense(
        self, query_embedding: list[float], top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Return up to `top_k` notes most cosine-similar to `query_embedding`, best first.

        `within` restricts hits to the given note ids, and `None` means the whole index. It is
        applied in the backend's own query rather than to the result, so the top-k slots are mostly
        not spent on notes the caller would discard.

        **"Mostly" is the contract, not "always", and a caller must not read fewer than `top_k`
        hits as "there were no others".** On a backend whose dense search is approximate — pgvector
        HNSW, which is what production runs — the scope is a filter over the index's candidate
        list rather than a bound on what the scan considers, so a selective `within` can leave
        fewer than k candidates surviving. Only the in-memory reference and the lexical leg are
        exact. `PostgresNoteIndex.__init__` carries the measurement.
        """
        ...

    async def search_lexical(
        self, query: str, top_k: int, within: set[str] | None = None
    ) -> list[IndexHit]:
        """Return up to `top_k` notes best matching the terms in `query`, best first.

        **A note matching *every* term outranks one matching only some, a note matching some is
        still a hit, and a note carrying a `-excluded` term is not a hit at all.** Both backends
        state that one rule (`chemclaw.core.fulltext`) — the durable one used to AND the terms while
        the in-memory reference OR'd them, so a multi-word question returned evidence in the tests
        and nothing in production. It is also the rule `GraphRetriever` already applies to the same
        corpus (D-138), so the graph and the index answer such a question the same way.

        `within` scopes the search exactly as in `search_dense`, and here it really is a bound
        before the `LIMIT` rather than a post-filter: the lexical path is exact.
        """
        ...


# The version of the note-text derivation behind every stored note vector. `search_text` decides
# what a note's embedded text *is*, and `embedding_config_key()` cannot see that decision — it
# keys the model, endpoint and dimension, so a change to the derivation (adding `conditions` and
# `source` to the haystack did exactly this) left every stored vector reading as current while
# embedding a different text than a fresh index would. Folding the version into the note-side key
# makes the old rows invisible to dense reads (the safe failure) and the next reindex re-embed
# them, which is the same self-healing path a model swap already takes. Bump it whenever
# `chemclaw.kg.search.search_text`'s composition changes.
_NOTE_TEXT_VERSION = "ntv2"


def note_embedding_key() -> str:
    """The embedding identity of a stored note vector: model configuration plus text derivation."""
    return f"{embedding_config_key()}|{_NOTE_TEXT_VERSION}"


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
    The *boolean* rule is not a proxy and is not allowed to drift — see `search_lexical`.
    """

    def __init__(self) -> None:
        """Start with an empty index, keyed by note id (re-upserting an id replaces it)."""
        self._records: dict[str, NoteRecord] = {}
        self._embedding_keys: dict[str, str] = {}

    async def upsert(self, records: list[NoteRecord], embedding_key: str) -> None:
        """Insert or replace each record by note id, under the configuration that embedded it."""
        for record in records:
            self._records[record.note_id] = record
            self._embedding_keys[record.note_id] = embedding_key

    async def retire_absent(self, keep: set[str]) -> int:
        """Drop every record whose note id is not in `keep`; an empty `keep` drops nothing."""
        if not keep:
            return 0
        gone = [note_id for note_id in self._records if note_id not in keep]
        for note_id in gone:
            del self._records[note_id]
            self._embedding_keys.pop(note_id, None)
        return len(gone)

    async def fingerprints(self, embedding_key: str) -> dict[str, str]:
        """Fingerprints of rows embedded under `embedding_key`; empty ones omitted.

        A row from a superseded configuration is left out exactly as a fingerprint-less one is —
        both mean "no reusable vector on record", which is what the caller asks this question for.
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

        Scoped to the live embedding configuration, exactly as `fingerprints` is — see
        `PostgresNoteIndex.search_dense` for the defect that scoping both directions closes, and
        why a reference that scoped only the write path could not stand in for the backend.
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

        The same boolean semantics `PostgresNoteIndex` states, which is the whole point of this
        being called a reference: it used to score any note sharing a single token while the durable
        backend ANDed the terms, so a multi-word query that returned hits here returned none there
        and no test could see it. A `-term` exclusion is part of that rule and is honoured here for
        the same reason — a reference that reads `-solvent` as a *request* for solvent is the same
        defect in the mirror. Tokens stand in for Postgres lexemes (no stemming, no stop-word list),
        so the *scores* still differ from `ts_rank` — the ordering intent and the boolean rule are
        what must match.
        """
        wanted, excluded = reference_terms(query)
        if not wanted and not excluded:
            return []
        # (complete, overlap, hit) — `complete` leads the sort for the same reason the durable
        # statement's `lexeme @@ all_terms` does: a note matching every term outranks one matching
        # some, and widening only decides what is returned when nothing matches them all.
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
            "INSERT INTO note_index "
            "(note_id, embedding, lexeme, fingerprint, embedding_key, updated_at) "
            f"VALUES (%(id)s, %(emb)s::vector({width}), "
            "to_tsvector('english', %(text)s), %(fp)s, %(key)s, now()) "
            "ON CONFLICT (note_id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, lexeme = EXCLUDED.lexeme, "
            "fingerprint = EXCLUDED.fingerprint, embedding_key = EXCLUDED.embedding_key, "
            "updated_at = now()"
        )
        # The `> 0` floor mirrors the InMemory reference (`score > 0.0`): a zero/near-zero or
        # negatively-correlated note is not a hit. Without it pgvector returns the top-k nearest
        # unconditionally, so a small corpus would surface unrelated notes as cited evidence — a
        # ranking the tests never see. (A zero query vector is short-circuited in `search_dense`
        # before the query, so `<=>` never produces a NaN distance to order by.)
        # The `within` scope lives in the SQL itself (NULL = unrestricted) rather than being applied
        # to the result, so the top-k slots are mostly not spent on notes the caller would drop.
        #
        # **On the dense path that is a tendency, not the guarantee this comment used to claim.**
        # With the HNSW index actually in use (see the tie-break below), the predicate is a *post*
        # filter over the ef_search candidate list, not a bound on what the index scan considers,
        # so a selective `within` can leave fewer than k candidates surviving. That is why
        # `NoteIndex.search_dense`'s contract says "mostly".
        #
        # **What this comment claimed as the measurement was not reproducible, and the re-measure
        # is the interesting part.** N=20,000, tight clusters, k=8, pgvector 0.8.0,
        # `hnsw.ef_search=40`, `hnsw.iterative_scan=off`, `EXPLAIN ANALYZE` on this very statement:
        #
        #   unscoped, planner or `enable_seqscan=off` -> Index Scan using note_index_embedding_idx,
        #                                                8 of 8
        #   within=0.10, planner                      -> Seq Scan + top-N heapsort, 8 of 8 (exact)
        #   within=0.10, `enable_seqscan=off`         -> Index Scan using note_index_pkey + top-N
        #                                                heapsort, 8 of 8 (exact)
        #
        # 0 of 20 queries returned short in any of the four. The earlier "forcing the index at
        # `within=0.10` returned 5 of 8" rests on `enable_seqscan=off` forcing the *vector* index,
        # and it does not: with a `note_id = ANY(...)` predicate the planner takes the primary key
        # instead, which is exact. So at this scale the scoped note query is exact under **both**
        # plans the planner will produce, and the shortfall above is a hazard the shape permits
        # rather than one this corpus exhibits — the estimated cost of the exact scan grows with N
        # while the HNSW path's startup cost does not, so a large enough corpus flips it.
        #
        # `PostgresDocumentIndex` is where the same shape does bite, mildly: its eligibility is an
        # `EXISTS` over another table, which the planner cannot collapse into a key scan the way it
        # collapses this array, so it stays a filter above an HNSW scan — 2 of 20 queries short at
        # one source size, 1 of 20 at another, on an analyzed database. Its docstring carries the
        # table. The asymmetry is the predicate's shape, not `within` itself.
        #
        # `GraphRetriever` always passes a `within`, so the scoped plan is the only one production
        # takes. Two knobs now trade latency back for recall on exactly this statement —
        # `settings.hnsw_ef_search` and `settings.hnsw_iterative_scan`, applied per query by
        # `db.apply_vector_recall_settings` (shared with the document index, whose dense path is
        # where the residual actually is). Both default to leaving the server alone, because the
        # measured cause of the large shortfalls was stale planner statistics rather than ANN
        # recall (13/20 and 20/20 queries short before `ANALYZE`, 0/20 after) and these address
        # only what is left after it.
        #
        # The lexical statement below carries no such caveat: `ts_rank` over the GIN index is exact,
        # and there `within` really is a bound before the LIMIT. So is the InMemory backend, which
        # is why a two-row unit test cannot see any of this.
        scope = "AND (%(ids)s::text[] IS NULL OR note_id = ANY(%(ids)s::text[])) "
        # **The tie-break sorts the k rows, not the table.** `note_id` as a secondary key mirrors
        # the InMemory reference's `(-score, note_id)`, so equal-similarity notes order
        # deterministically and identically across backends — but written into the *inner* ORDER BY
        # it made the ordering non-derivable from the vector index and the planner abandoned the
        # index entirely. EXPLAIN ANALYZE at N=20,000, median of 5: inner tie-break → Seq Scan +
        # Sort, 243.05 ms; this form → Index Scan + a 10-row quicksort, 3.58 ms, returning the same
        # ids in the same order as the tie-break-free query. What it does *not* pin is which rows
        # win a tie *at the k-th place* — and neither did the old form, because HNSW is approximate:
        # the tie-break exists so two backends agree on the order of the hits they return.
        # **`embedding_key` is a predicate on the read, not only on the rebuild.** The column was
        # added (039, D-2026-08-08) because "a vector is only reusable for the configuration that
        # made it… comparing its queries against the old model's vectors corrupts every similarity,
        # silently, and no error is ever raised" — and it then gated `fingerprints` and nothing
        # else. So an operator repointing `embedding_model` or `llm_base_url` at another model of
        # the same width (which raises nothing at insert) put every row into a state where the
        # rebuild treats it as absent and this statement scores it 0.99 against the *new* model's
        # query. The claimed self-healing is a Temporal schedule, so the window is at least one
        # `note_reindex_schedule_minutes` plus a corpus re-embed, and unbounded when it is off.
        # An empty dense leg is something the sweep already reports honestly (`fanout._sweep`);
        # cross-space garbage cited as evidence is not.
        self._dense = (
            "SELECT note_id, score FROM ("
            f"SELECT note_id, 1 - (embedding <=> %(q)s::vector({width})) AS score "
            "FROM note_index WHERE embedding IS NOT NULL AND embedding_key = %(key)s "
            f"AND 1 - (embedding <=> %(q)s::vector({width})) > 0 "
            f"{scope}"
            f"ORDER BY embedding <=> %(q)s::vector({width}) LIMIT %(k)s"
            ") AS hits ORDER BY score DESC, note_id"
        )
        # **Match any term; rank the notes matching every term above the rest; honour a `-term`
        # exclusion.** One boolean semantics, built once in `chemclaw.core.fulltext` and stated in
        # `InMemoryNoteIndex.search_lexical`, because the two used to disagree: this statement was
        # `websearch_to_tsquery` alone, which ANDs, while the in-memory reference scored any note
        # sharing a single token. Measured on a 15,000-note corpus, four stems ("amide coupling
        # solvent screen"): the AND form matched **0 rows** while the widened form returned the
        # complete matches first — so an ordinary multi-word question retrieved on the dense leg
        # alone in production, the lexical leg contributed nothing, and the rank fusion the hybrid
        # mode rests on ran one-legged, while the unit tests passed on the memory OR. A test that
        # cannot see the semantics of the backend it stands in for is not a reference.
        #
        # This is the rule `GraphRetriever` already states for the same corpus (D-138: every term,
        # widening to any term rather than answering "nothing known", coverage ordering the result),
        # so the two entry points into the graph now answer a multi-word question the same way. It
        # is expressed as one statement rather than a query-then-retry because `ts_rank` over the
        # widened query already ranks a full-coverage note above a partial one, and the explicit
        # `lexeme @@ all_terms` sort key makes that ordering a guarantee instead of a tendency.
        #
        # **The widening is over the parsed query's clauses, not over the query's lexemes.** This
        # comment used to claim the widened form "must OR exactly the stems the AND form would have
        # required" — and that claim was false for negation, which is the one place the two differ:
        # `to_tsvector` does not know `-`, so widening its lexemes turned an exclusion into a
        # positive OR term and `amide coupling -solvent` returned the solvent notes.
        # `chemclaw.core.fulltext.TSQUERY_TERMS` carries the measurement and the injection argument.
        #
        # Measured cost of widening, same corpus, GIN index used in both (`Bitmap Index Scan`):
        # 3.1 ms matching 5,000 rows (AND) against 12.4 ms matching 10,000 (widened). The scan is
        # proportional to how many notes share *any* term, which is the price of not returning
        # nothing.
        self._lexical = (
            "SELECT note_id, ts_rank(lexeme, any_terms) AS score "
            f"FROM note_index, {TSQUERY_TERMS} "
            f"WHERE lexeme @@ any_terms {scope}"
            "ORDER BY (lexeme @@ all_terms) DESC, score DESC, note_id LIMIT %(k)s"
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
        async with db.connection(self._dsn) as conn:
            yield conn

    def _row_vector(self, record: NoteRecord) -> str | None:
        """The value bound into this row's `embedding` column — the vector, here.

        A hook with one line in it, because the subclass whose vectors live elsewhere needs to bind
        `NULL` and everything else about the write is identical. `%(emb)s::vector(N)` renders
        `NULL::vector(N)` for `None`, which is a valid insert, so the statement itself is shared
        rather than rebuilt — a second copy of an `INSERT … ON CONFLICT` is how two writers stop
        agreeing about what a row holds.
        """
        return _vector_literal(record.embedding)

    async def upsert(self, records: list[NoteRecord], embedding_key: str) -> None:
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
                    },
                )
            await conn.commit()

    async def retire_absent(self, keep: set[str]) -> int:
        """Delete rows for notes no longer on disk, returning the ids so a subclass can follow.

        `RETURNING note_id` rather than a count: `ExternalVectorNoteIndex` needs the ids to remove
        the matching points from its store, and asking the table twice would race its own delete.
        """
        return len(await self._retire_absent_ids(keep))

    async def _retire_absent_ids(self, keep: set[str]) -> list[str]:
        """The shared half: delete and report which ids went. Empty `keep` deletes nothing."""
        if not keep:
            return []
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM note_index WHERE NOT (note_id = ANY(%(keep)s)) RETURNING note_id",
                    {"keep": sorted(keep)},
                )
                rows = await cur.fetchall()
            await conn.commit()
        return [row[0] for row in rows]

    def _read_key(self) -> str:
        """The `embedding_key` a *read* must match — the live configuration, as stored.

        A hook rather than a direct `embedding_config_key()` call for `_stored_key`'s reason:
        `ExternalVectorNoteIndex` namespaces the key it writes by store and collection, so the two
        would disagree the moment that subclass reached this statement. It does not today (it
        overrides `search_dense` to rank in the store), which is exactly when a shared spelling is
        cheap to get right and expensive to discover later.
        """
        return note_embedding_key()

    async def fingerprints(self, embedding_key: str) -> dict[str, str]:
        """Stored fingerprints for every row that has one *and* was embedded under this key.

        NULL in either column (a row written before that column existed, or a vector from a
        superseded embedding configuration) is left out — all of those are "unknown", which reads
        as changed to `reindex_notes`, never as a stale match.
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

        **Only rows this deployment's current embedding configuration produced** — see the `_dense`
        statement for what scoring the others silently cost.
        """
        # A zero query vector (a token-less/symbol-only query under the hash embedder) has cosine 0
        # to everything — no hit, exactly as the InMemory reference returns. Short-circuit so we
        # never hand pgvector a zero vector (whose `<=>` distance is NaN) to order by.
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

    Two shapes, chosen by `vector_store_provider`, exactly as `default_document_index()` chooses
    (`ingest/documents/index.py`). `pgvector` (the default) keeps the vectors in the same statement
    that ranks and filters them; any other provider moves **only** the dense half to that store and
    leaves the text, the `tsvector`, the fingerprint and the embedding key in `note_index`, because
    those are relational work a vector database has no `ts_rank` for.
    """
    if settings.vector_store_provider == "pgvector":
        return PostgresNoteIndex()

    from chemclaw.retrieval.external_note_index import ExternalVectorNoteIndex
    from chemclaw.retrieval.vectors.registry import default_vector_store

    return ExternalVectorNoteIndex(default_vector_store())


def _needs_embedding(note_id: str, current: dict[str, str], stored: dict[str, str]) -> bool:
    """Whether `note_id` must be (re-)embedded: its file fingerprint differs from the stored one.

    A note the fingerprint scan does **not** know is always re-embedded rather than compared. The
    two sides are keyed differently by construction — the scan keys on the file's stem (stat-only,
    it never parses), the note list keys on the id inside the frontmatter — so a note whose filename
    disagrees with its id is missing from `current`, was missing from `stored` too, and `None !=
    None` is False: it read as "unchanged" forever and was never indexed at all, with `full=True`
    no help because it takes the same branch. Absent means unknown, and unknown means embed it.

    Said at WARNING because the only way to be here is that mismatch (or a file deleted between the
    two scans, which is transient): the note is indexed, but it costs an embedding on every run
    until the filename is fixed, and `chemclaw.kg.validate` fails the PR that introduces one.
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

    **A model change is detected too, and needs no flag**
    (D-2026-08-08-a-derived-index-must-record-what-derived-it).
    The file fingerprint cannot see one — swapping the embedding model moves no mtime —
    so the index also stores which configuration embedded each row (`note_index.embedding_key`,
    migration 039), and `fingerprints()` only reports rows made by the current one. A row from a
    superseded configuration therefore has no stored fingerprint to match and is re-embedded here,
    which is what makes the scheduled incremental run self-healing rather than a `--full` somebody
    has to remember at the moment they change a setting.

    `full=True` re-embeds every note unconditionally (the CLI's `--full`), for recovery from a
    corrupted index.

    Idempotent either way (upsert by id), so it is safe to run on a schedule or after a merge.

    **Notes deleted from disk are retired here** (D-2026-08-25). They used to be left behind as
    stale rows, on the argument that the retrievers drop a hit whose note no longer loads — true,
    and enough while every vector sat in a Postgres table nobody bills per row. It stops being
    enough once the dense half can live in an external store: nothing else in this system ever
    deletes a note vector, so an unpruned index would grow by one orphan per deleted note forever.
    The prune runs before the "nothing changed" exit below, because a run whose only news is a
    deletion has nothing to embed and must still remove it.

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
        # **A silent 0 here is what a mis-mounted knowledge volume looks like**, and it is also
        # what an empty corpus looks like, and the hourly job reports success for both — forever,
        # while the dense and lexical legs keep serving whatever the index last held. So the two
        # are separated by the one thing that distinguishes them: whether the directory is there.
        # A missing directory is a deployment fault (a volume that did not mount, a
        # `knowledge_path` pointing at nothing); a present but empty one is a fresh corpus, which
        # is a real state and stays at DEBUG.
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
    current_fingerprints = await asyncio.to_thread(note_file_fingerprints, directory)
    # Guarded three times over against wiping the index: `notes` is non-empty by the return above,
    # `retire_absent` itself does nothing for an empty `keep`, and `keep` is the union below rather
    # than the parsed set. A mis-pointed directory costs one embedding call per note to recover
    # from, so it is worth saying no three times.
    #
    # **`keep` is the union of what parsed and what is on disk, and the second half is the fix.**
    # `_parse_notes` skips an unparseable note by design — it names the case itself, "an rsync that
    # lands a renamed note before removing the old one" — so building `keep` from the parsed set
    # alone made a *transient* parse failure a *deletion* from the derived index. Measured: 40 of
    # 100 notes made unparseable retired 40 index rows, and repairing them cost one embedding call
    # each, over an hour in which both index-backed legs answered as though those notes did not
    # exist. `note_file_fingerprints` is stat-only and keyed by the same id, so it holds an entry
    # for a file whose frontmatter is broken — which is exactly the population that must survive.
    # The graph leg already degrades this way (skip, WARNING, counter); the derived legs now do too.
    on_disk = set(current_fingerprints)
    unparsed = on_disk - {note.id for note in notes}
    if unparsed:
        log.warning(
            "note re-index: %d note file(s) on disk did not parse and are kept in the index rather "
            "than retired (%s); they will be re-embedded when they parse again",
            len(unparsed),
            ", ".join(sorted(unparsed)[:5]),
        )
    retired = await index.retire_absent({note.id for note in notes} | on_disk)
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
    # **Bounded per note and embedded in batches**, because neither bound existed and one note
    # without them froze both derived legs indefinitely. See `note_embed_max_chars` for the
    # measurement; the batching is the half that keeps a refusal local — a failing batch raises
    # after the batches before it have already been upserted, so the pass makes partial progress
    # and the next one only retries what is still missing (a note whose row was never written has
    # no stored fingerprint, which reads as "changed").
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
        await index.upsert(records, embedding_key)
        indexed += len(records)
    return indexed


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
