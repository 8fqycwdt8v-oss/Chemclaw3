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
from pathlib import Path
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
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
from chemclaw.kg.note import note_id_for_reaction, note_relative_path
from chemclaw.retrieval.evidence import EvidenceChunk

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

    def _connection(self) -> Warehouse:
        """The warehouse, opened on first use and reused for the life of the process."""
        if self._warehouse is None:
            self._warehouse = open_warehouse(self._binding.connection)
        return self._warehouse

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
            return self._chunks(await self._search(query, filters))
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
        warehouse = self._connection()
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
            embedded,
            filters,
            settings.retrieval_top_k,
            settings.embedding_dim,
        )
        async with warehouse.cursor() as cursor:
            await cursor.execute(statement, params)
            return await cursor.fetchall()

    def _chunks(self, rows: list[dict[str, Any]]) -> list[EvidenceChunk]:
        """Turn ranked rows into evidence, dropping the ones that already became notes."""
        chunks: list[EvidenceChunk] = []
        suppressed = 0
        for row in rows:
            key = str(row.get(self._vector.key, "")).strip()
            if not key:
                continue
            if self._vector.suppress_ingested and _is_merged_note(key):
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


def _is_merged_note(key: str) -> bool:
    """Whether this reaction already has a merged note in the knowledge graph.

    Asked per hit rather than by listing the directory, because the question is about a handful of
    ids and the answer for each is one filename: the graph *is* the checkout, so this is a `stat`
    per returned row instead of a full `readdir` of a corpus that grows without bound. On the chat
    hot path that difference is the whole cost of the check. The layout it depends on comes from
    `chemclaw.kg.note.note_relative_path` rather than being re-spelled here, so this cannot be the
    one reader that disagrees with the PR-gate about where a note lands.

    Deliberately not cached: a merge lands between two queries, and a stale answer would keep
    surfacing a reaction a reviewer had just signed off on — the exact duplication this prevents.

    The key is a warehouse-controlled string, so the joined path is confined to the graph before it
    is stat'd. `reaction-../../../etc/passwd` used to build a path outside `knowledge_path`; the
    stat is the only operation and nothing is read, but the answer it produces is the one that
    decides whether a hit is *suppressed*, so a key escaping the graph could hide evidence by
    landing on any file that happens to exist. Confinement rather than a slug pattern, because a
    site's own row keys are its business — one containing a slash is unusual, not hostile, and it
    still has no note, which is exactly what this returns for it.
    """
    root = Path(settings.knowledge_path).resolve()
    note = (root / note_relative_path("reaction", note_id_for_reaction(key))).resolve()
    if not note.is_relative_to(root):
        return False
    return note.is_file()
