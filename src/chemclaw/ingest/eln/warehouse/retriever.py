"""The retrieve half: similarity search run inside the warehouse, over the corpus that stays there.

A warehouse ELN holds an embedding per reaction over a corpus far larger than the curated slice that
is ingested, so the search runs where the vectors live instead of copying them. This is a
`SourceRetriever` rather than a `NoteIndex` search, because note-index hits without a local note
file are dropped. Unlike file-drop ELNs (ingest-only, to avoid double counting), a warehouse ELN
gets a retrieve half because most of its corpus has no other way in; `suppress_ingested` drops the
hits that did become records.
"""

import asyncio
import logging
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.ingest.eln.records import default_record_store
from chemclaw.ingest.eln.warehouse import sql
from chemclaw.ingest.eln.warehouse.binding import (
    BindingError,
    VectorBinding,
    WarehouseBinding,
    load_binding,
)
from chemclaw.ingest.eln.warehouse.connect import open_warehouse
from chemclaw.ingest.eln.warehouse.driver import Warehouse, WarehouseQueryError
from chemclaw.ingest.eln.warehouse.expr import as_text
from chemclaw.kg.note import require_note_slug
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.vectors.base import VectorStore

logger = logging.getLogger(__name__)


class WarehouseVectorRetriever:
    """A `SourceRetriever` running its similarity search in the warehouse. One per data source."""

    def __init__(self, binding: dict[str, Any], name: str) -> None:
        """Validate the binding at startup; `name` is the source chunks are attributed to.

        `binding` is the manifest's `config:` block, shared with the ingest half. `name` is
        required, so two warehouse sources stay distinguishable in citations and source weights.
        """
        self._binding: WarehouseBinding = load_binding(binding)
        if self._binding.vector is None:
            raise WarehouseQueryError(
                "this data source declares a retrieve half, but its binding has no 'vector' section"
            )
        self._vector: VectorBinding = self._binding.vector
        self.name = name
        self._warehouse: Warehouse | None = None
        # Used only by an index-ranked source and resolved lazily, so an unreachable index fails a
        # search rather than pod startup. Not a constructor argument: the registry splats the
        # manifest's `config:` into this signature, and a manifest may not set a store. Tests assign
        # the attribute.
        self._store: VectorStore | None = None

    def _connection(self) -> Warehouse:
        """The warehouse, opened on first use and reused for the life of the process."""
        if self._warehouse is None:
            self._warehouse = open_warehouse(self._binding.connection)
        return self._warehouse

    def _index_store(self) -> "VectorStore":
        """The vector store an index-ranked source ranks in, resolved on first use."""
        if self._store is None:
            from chemclaw.retrieval.vectors.registry import default_vector_store

            self._store = default_vector_store()
        return self._store

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return the warehouse's nearest reactions to `query`, best first.

        An empty list means the warehouse matched nothing; a failure is raised, so the evidence
        sweep records this source as failed rather than reporting an empty corpus. A blank query
        returns `[]` without asking.
        """
        if not query.strip():
            return []
        # `_chunks` queries the record store too, so its failures must also propagate.
        return await self._chunks(await self._search(query, filters))

    async def _search(self, query: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """Run the ranked search, embedding here or in the warehouse as the binding says."""
        if self._vector.index:
            return await self._search_index(query, filters)
        warehouse = self._connection()
        # Checked before the embedding call, which may be a paid round trip, since a driver without
        # vector support fails every query. Not at construction, because resolving the driver
        # imports the vendor client. A `BindingError` is a non-retryable `ChemclawError`.
        dialect = warehouse.vector_dialect
        if dialect is None:
            raise BindingError(
                f"{self.name}: this binding declares a `vector:` block, but its driver offers no "
                "similarity-search dialect. Only a warehouse whose function names this repository "
                "has verified can serve one; rank on a vector index instead, or drop the block"
            )
        # Offloaded because `embed_texts` may make a blocking provider call, and this runs on the
        # event loop serving every stream. Under `server` the warehouse embeds the raw query inside
        # the SQL, so no `chemclaw_embedding_*` metric is recorded; the leg is timed on
        # `chemclaw_evidence_source_seconds`.
        embedded: str | list[float] = (
            query
            if self._vector.embedding == "server"
            else (await asyncio.to_thread(embed_texts, [query]))[0]
        )
        statement, params = sql.vector_statement(
            self._vector,
            warehouse.placeholder,
            dialect,
            embedded,
            filters,
            settings.retrieval_top_k,
            settings.embedding_dim,
        )
        async with warehouse.cursor() as cursor:
            await cursor.execute(statement, params)
            return await cursor.fetchall()

    async def _search_index(self, query: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """Rank in a vector index, then resolve the winning keys to content in the warehouse.

        The store answers which and how similar; the warehouse, which owns the text, answers what it
        says. Used where an in-warehouse similarity scan would be a full scan of a very large
        corpus. Rows come back shaped like the scanned path's, score included.
        """
        embedding = (await asyncio.to_thread(embed_texts, [query]))[0]
        groups = await self._eligible_keys(filters)
        if groups is not None and not groups:
            return []
        matches = await self._index_store().search(
            self._vector.index, embedding, settings.retrieval_top_k, groups
        )
        if not matches:
            return []
        scores = {match.id: match.score for match in matches}
        warehouse = self._connection()
        statement, params = sql.resolve_statement(self._vector, warehouse.placeholder, list(scores))
        async with warehouse.cursor() as cursor:
            await cursor.execute(statement, params)
            resolved = await cursor.fetchall()
        by_key = {str(row.get(self._vector.key, "")): row for row in resolved}
        # The store's order is the ranking; rebuilt from `matches` so ties keep a stable order. Keys
        # the relation no longer holds are dropped.
        rows: list[dict[str, Any]] = []
        for match in matches:
            row = by_key.get(match.id)
            if row is None:
                continue
            rows.append({**row, sql.SCORE_COLUMN: match.score})
        return rows

    async def _eligible_keys(self, filters: dict[str, Any]) -> set[str] | None:
        """The keys a filtered search may match, or `None` for an unrestricted one.

        `None` means the whole index at no extra query; an empty set means nothing is eligible and
        must never become an unfiltered search. Eligibility reaches the index before its top-k,
        since post-filtering a narrow tag would return nothing. Only the query's narrow filters form
        a scope; the binding's broad `where:` is enforced by `resolve_statement`, since enumerating
        it would exceed the scope cap.
        """
        # Truthiness, matching `sql.vector_predicates`: an empty `tag=""` is no filter.
        if not any(filters.get(key) for key in self._vector.filter_columns):
            return None
        cap = settings.vector_store_max_scope_keys
        warehouse = self._connection()
        statement, params = sql.scope_statement(self._vector, warehouse.placeholder, filters, cap)
        async with warehouse.cursor() as cursor:
            await cursor.execute(statement, params)
            rows = await cursor.fetchall()
        if len(rows) > cap:
            # Refused rather than truncated: a cut eligibility set reads as a thin corpus.
            message = (
                f"{self.name}: this query's filters match more than {cap} rows, which is more "
                "eligibility than an index filter can carry. Narrow them, raise "
                "CHEMCLAW_VECTOR_STORE_MAX_SCOPE_KEYS if the index can take it, or move the "
                "restriction into the binding's `where:`, which is enforced without enumerating"
            )
            # Also logged at WARNING because this message is the operator's fix, and callers may log
            # only a generic failure.
            logger.warning("%s", message)
            raise WarehouseQueryError(message)
        return {str(row[self._vector.key]) for row in rows if row.get(self._vector.key)}

    async def _chunks(self, rows: list[dict[str, Any]]) -> list[EvidenceChunk]:
        """Turn ranked rows into evidence, dropping the ones already ingested as records."""
        chunks: list[EvidenceChunk] = []
        suppressed = 0
        keys = [k for row in rows if (k := str(row.get(self._vector.key, "")).strip())]
        ingested = await _ingested_keys(keys) if self._vector.suppress_ingested else set()
        for row in rows:
            key = str(row.get(self._vector.key, "")).strip()
            if not key:
                continue
            if key in ingested:
                suppressed += 1
                continue
            content = self._describe(row)
            if not content:
                continue
            chunks.append(
                EvidenceChunk(
                    content=content,
                    # Not a note id: the citation resolves to the warehouse row itself, like
                    # `vendored:<dataset>:<row>`.
                    source_note_id=f"{self.name}:{key}",
                    retriever=self.name,
                    score=sql.normalise_score(
                        self._vector.metric, float(row.get(sql.SCORE_COLUMN, 0.0) or 0.0)
                    ),
                    source=f"{self.name}:{self._vector.relation}:{key}",
                )
            )
        if suppressed:
            logger.debug("%s: suppressed %d hit(s) already merged as notes", self.name, suppressed)
        return chunks

    def _describe(self, row: dict[str, Any]) -> str:
        """Render the content columns a chemist reads, labelled with the source's own names.

        Labelled because the columns are site-specific and a bare join would be unreadable.
        """
        parts = [
            f"{column}: {as_text(row[column]).strip()}"
            for column in self._vector.content_columns
            if row.get(column) is not None and str(row[column]).strip()
        ]
        return "\n".join(parts)


async def _ingested_keys(keys: list[str]) -> set[str]:
    """Which of these warehouse keys the ELN corpus already holds as reaction records.

    One `known()` query for the whole result set, uncached so a just-ingested reaction is suppressed
    at once. Keys are bound parameters, so a hostile key selects nothing. A key that fails
    `kg.note.require_note_slug` cannot be a record id and is filtered out before the query, both
    because the answer is known and because a value like a NUL byte would make the driver raise and
    discard the whole leg.
    """
    askable = []
    for key in keys:
        try:
            askable.append(require_note_slug(key))
        except ValueError:
            # A binding whose key column is wrong rejects every key, silently disabling suppression
            # so duplicates read as corroboration; logged so that is visible.
            logger.debug("warehouse key %r cannot be a record id; not asked about", key)
            continue
    if not askable:
        if keys:
            logger.warning(
                "none of %d warehouse key(s) can be a record id (e.g. %r), so no hit can be "
                "suppressed as already ingested. This is what a binding whose `entry.key` names "
                "the wrong column looks like: reactions already in the corpus are served twice",
                len(keys),
                keys[0],
            )
        return set()
    return await default_record_store().known(askable)
