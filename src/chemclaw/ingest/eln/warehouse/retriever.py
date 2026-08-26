"""The retrieve half: similarity search run inside the warehouse, over the corpus that stays there.

A warehouse ELN already holds an embedding per reaction, over a corpus far larger than the curated
part that becomes knowledge-graph notes. Two things follow, and together they are why this half
exists at all:

**Search it where it lives.** Copying the vectors into this system's index would mean re-embedding a
corpus that is bigger than what gets ingested, and keeping the copy fresh forever. Pushing the ANN
into the warehouse makes the whole ELN reachable as evidence at the cost of one query.

**`SourceRetriever` is the right altitude, not `NoteIndex`.** The obvious reach is the note index's
`search_dense`, and it is wrong here: `retrieval.retrievers._chunks_from_hits` drops any hit whose
note is not on local disk, which is every warehouse row, so that path would discard the entire
result set. `retrieval.evidence` says the same thing from the other direction — adding a source is a
new retriever behind that interface, never a change to core.

**Why an ELN may carry a retrieve half here, when `eln-json` and `eln-ord` may not.** The rule those
two follow — ingest-only, so a reaction is not surfaced once as a note and again as a raw record —
is about double-counting, not about ELNs. A file-drop ELN ingests everything it sees, so a retriever
over it would be pure duplication. A warehouse ELN ingests a curated slice of something much larger,
and the rest has no other way in. `suppress_ingested` keeps the original rule intact by dropping
exactly the hits that did become notes.
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

        `binding` comes from the manifest's `config:` block, which the registry splats into
        whichever half it builds — so a source declaring both halves would fail
        `make datasource-validate` if they disagreed about their `config` keys. `name` is the extra
        argument the *retrieve* path adds on top of that block (`_build_retrieve_half`), which the
        ingest adapter does not take and does not need: the ELN sync already keys its cursors on the
        manifest name.

        **It is required, and it used to default to `"warehouse"`.** A default is only ever right
        for the first instance — a second warehouse ELN would have been indistinguishable from the
        first in every citation and every source weight, which is the same defect that let two
        mounted shares share one document-index partition.
        """
        self._binding: WarehouseBinding = load_binding(binding)
        if self._binding.vector is None:
            raise WarehouseQueryError(
                "this data source declares a retrieve half, but its binding has no 'vector' section"
            )
        self._vector: VectorBinding = self._binding.vector
        self.name = name
        self._warehouse: Warehouse | None = None
        # Only ever used by an index-ranked source, and resolved lazily: the data-source registry
        # builds retrieve halves in the chat pod at startup, and a store that dialled out from a
        # constructor would make an unreachable index a failure to *boot* rather than to search.
        #
        # **Deliberately not a constructor argument**, though that would be the obvious way to
        # inject one in a test. The registry splats a manifest's whole `config:` block into this
        # signature, so every parameter here is something a manifest can set — and `store:` is not
        # a thing a manifest may say. `tests/test_warehouse_binding.py` pins the signature for
        # exactly this reason; a test that needs a fake assigns the attribute.
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

        **Every failure yields no evidence rather than raising**, so an unreachable warehouse does
        not take down answers the graph and the fingerprint index could have given between them.
        `ingest.sources.vendored_dataset` made the same call for the same reason.

        The justification this used to carry has expired and is corrected rather than deleted:
        `gather_evidence` fanned its retrievers out through a plain `asyncio.gather` with no
        `return_exceptions`, which made answering emptily the only thing preventing one outage from
        failing the whole question. The sweep is now per-source graph branches that each degrade
        alone (`chemclaw.retrieval.fanout`). Handling it here is still the better place — this is
        where the difference between a transient outage and a missing driver is known, and the two
        are logged differently below — but it is no longer load-bearing on its own.

        The cases are logged differently on purpose. A transient failure is a WARNING, because
        the next query may well succeed. A `BindingError` — a driver package the image does not
        carry, a credential variable nobody set — is an ERROR: it will recur on every query until
        someone changes the deployment, and it must not read as a quiet day for this source.
        Anything else is an ERROR with its traceback: it is either the embedding provider's own
        exception type or a defect here, and both need the stack the enumerated cases do not.
        """
        if not query.strip():
            return []
        try:
            # `_chunks` is inside the guard too: it stats the knowledge tree per row
            # (`suppress_ingested`), which is one more way this leg can fail on a bad day.
            return await self._chunks(await self._search(query, filters))
        except BindingError:
            logger.exception(
                "%s: misconfigured, returning no evidence — every query will do this until it is "
                "fixed",
                self.name,
            )
            return []
        except (WarehouseQueryError, ConnectionError, OSError):
            logger.warning("%s: warehouse search failed, returning no evidence", self.name)
            logger.debug("%s: search failure detail", self.name, exc_info=True)
            return []
        except Exception:
            # The backstop the enumerated list above cannot be, and the docstring's promise is only
            # true with it. The embedding provider is reached from inside `_search`, and it raises
            # its *own* client's exception types — an `openai.APIError` is none of the three above,
            # so a rate-limited or briefly unreachable embedding endpoint escaped this retriever
            # and, through a `gather` with no `return_exceptions`, failed the whole turn including
            # the answer the knowledge graph had already produced. Enumerating a vendor's exception
            # tree here would import it; the contract is "this leg yields no evidence, whatever
            # happens", so that is what is written. Loud in the log, invisible to the other legs.
            logger.exception("%s: unexpected search failure, returning no evidence", self.name)
            return []

    async def _search(self, query: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """Run the ranked search, embedding here or in the warehouse as the binding says."""
        if self._vector.index:
            return await self._search_index(query, filters)
        warehouse = self._connection()
        # Before the embedding call, not after it. Resolving the driver is what tells us whether it
        # can serve a similarity search at all, and under `openai_compatible` the embedding below is
        # a paid round trip to the LLM endpoint — spending one to build a statement that cannot be
        # sent is waste on a path that will fail on every query until the deployment changes.
        #
        # Checked here rather than at construction because resolving the driver means importing the
        # vendor client, and a chat pod that builds this half at startup must not pay that import
        # for a warehouse it may never query. A `BindingError` is a `ChemclawError`, so
        # `durable/publish.py` marks it non-retryable and `retrieve` logs it as the recurring
        # misconfiguration it is.
        dialect = warehouse.vector_dialect
        if dialect is None:
            raise BindingError(
                f"{self.name}: this binding declares a `vector:` block, but its driver offers no "
                "similarity-search dialect. Only a warehouse whose function names this repository "
                "has verified can serve one; rank on a vector index instead, or drop the block"
            )
        # Offloaded, not called inline: under the `openai_compatible` provider `embed_texts` reaches
        # the LLM endpoint over a blocking client, and this runs on the one event loop serving every
        # SSE stream — a stall here freezes conversations that have nothing to do with this source.
        # Measured before this: a 1 s provider call cost the loop its whole second (0 heartbeats
        # where a free loop runs ~20). `ingest.documents.retriever` offloads for the same reason.
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

        The same division of labour `ingest/documents/external_index.py` makes, with the warehouse
        standing in for Postgres as the catalogue: the store answers *which and how similar*, and
        the system that owns the text answers *what does it say*. It exists because the in-warehouse
        path evaluates a similarity function per row — right for an ELN, and a full scan of the
        corpus for anything at patent scale.

        Rows come back shaped exactly as the scanned path's do, score column included, so
        `_chunks` cannot tell which path produced them.
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
        # The store's order is the ranking; the resolve query has none. Rebuilt from `matches`
        # rather than sorted by score, because two identical scores would otherwise reorder
        # arbitrarily between calls. A key the relation no longer holds is simply dropped.
        rows: list[dict[str, Any]] = []
        for match in matches:
            row = by_key.get(match.id)
            if row is None:
                continue
            rows.append({**row, sql.SCORE_COLUMN: match.score})
        return rows

    async def _eligible_keys(self, filters: dict[str, Any]) -> set[str] | None:
        """The keys a filtered search may match, or `None` for an unrestricted one.

        `None` and the empty set are different statements and both are load-bearing: `None` means
        the whole index and must cost no extra query, while an empty set means nothing is eligible
        and must never be sent as an unfiltered search.

        Computed here rather than applied to the results because eligibility has to reach the index
        *before* its top-k. Filter afterwards and a narrow tag over a wide corpus returns nothing at
        all, since the k nearest vectors all belonged to something else.

        **What counts as "filtered" is the predicate list, not the query's keys.** It used to be the
        latter, and that silently dropped the binding's own `where:` on every query that carried no
        mapped filter: the same binding restricted the corpus when the model happened to pass a date
        and did not when it did not. `where:` is the binding author's statement about which rows are
        *ever* eligible, so asking `sql` what it would actually emit is the only reading that makes
        the two paths agree — the scanned one has always applied it unconditionally.
        """
        predicates, _ = sql.vector_predicates(self._vector, "?", filters)
        if not predicates:
            return None
        cap = settings.vector_store_max_scope_keys
        warehouse = self._connection()
        statement, params = sql.scope_statement(self._vector, warehouse.placeholder, filters, cap)
        async with warehouse.cursor() as cursor:
            await cursor.execute(statement, params)
            rows = await cursor.fetchall()
        if len(rows) > cap:
            # Refused rather than truncated: a silently cut eligibility set is a wrong answer that
            # reads as a thin corpus, and the operator's lever (a narrower filter, or a higher cap)
            # only exists if they are told.
            message = (
                f"{self.name}: the filter matches more than {cap} rows, which is more eligibility "
                "than an index filter can carry. Narrow the query's filters, or raise "
                "CHEMCLAW_VECTOR_STORE_MAX_SCOPE_KEYS if the index can take it"
            )
            # Logged here as well as raised, because `retrieve` catches this alongside every other
            # warehouse failure and logs a generic "search failed" at WARNING with the real message
            # only at DEBUG. This is the one case where the message *is* the fix, and an operator
            # reading a default-level log would otherwise see a broad filter as an empty corpus.
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
                    # Not a knowledge-graph note id, and not pretending to be one: the citation has
                    # to resolve to something a reader can check, and for a row that was never
                    # proposed as a note that is the row itself. `vendored:<dataset>:<row>` is the
                    # same call made for the same reason.
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

        Labelled rather than concatenated because the columns are site-specific: a bare join of a
        SMILES, a protocol and a project code reads as one run-on sentence, and the labels are the
        only thing saying which is which.
        """
        parts = [
            f"{column}: {as_text(row[column]).strip()}"
            for column in self._vector.content_columns
            if row.get(column) is not None and str(row[column]).strip()
        ]
        return "\n".join(parts)


async def _ingested_keys(keys: list[str]) -> set[str]:
    """Which of these warehouse keys the ELN corpus already holds as reaction records.

    **This asked the filesystem until D-2026-08-25 made that answer permanently `False`.** It
    stat'd `knowledge/reaction/reaction-<key>.md`, which is exactly the file ingestion stopped
    writing when a transcription became a row — so `suppress_ingested` (default `True`) became a
    no-op, a curated warehouse reaction reached the agent twice, and the duplicate read as
    corroboration rather than as one source counted once. Nothing failed; the check simply stopped
    checking, which is why the suite did not notice.

    One query for the whole result set rather than a lookup per row. The stat-per-row shape it
    replaces was argued as cheaper than a `readdir` of an unbounded corpus, and that argument holds
    for stats; it does not survive the move to a store, where per-row would be a round trip per hit
    on the chat hot path. `known()` takes the ids as one parameter and the corpus size never enters.

    Deliberately not cached: an ingest lands between two queries, and a stale answer would keep
    surfacing a reaction the corpus had just absorbed — the exact duplication this prevents.

    The path-confinement this function used to need is gone with the path. A key is now a bound
    parameter in a SQL predicate, so a warehouse row spelling `reaction-../../../etc/passwd` selects
    no row rather than stat'ing whatever that resolves to.

    **A key that could not be a record id is filtered out before the query, not asked about.** That
    is exactness rather than caution: a record id validates through `kg.note.require_note_slug`, so
    a key the rule rejects has no record by construction and the store would answer `False` for it
    anyway — while *asking* can be worse than useless. Postgres text cannot hold a NUL, so one
    warehouse row with a NUL byte in its key would raise out of the driver, through `retrieve()`'s
    backstop, and return `[]` for the **entire leg** — discarding every legitimate hit beside it.
    That is the "hide evidence" outcome this check exists to prevent, reached from the other side;
    it is the same defect the old path form had with `resolve()`, and it survives the move to a
    store unless the filter does.
    """
    askable = []
    for key in keys:
        try:
            askable.append(require_note_slug(key))
        except ValueError:
            continue  # cannot be a record id, so it has no record — no query needed
    if not askable:
        return set()
    return await default_record_store().known(askable)
