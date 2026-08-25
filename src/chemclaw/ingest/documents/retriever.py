"""The retrieve half: answer questions from the indexed share, for callers entitled to see it.

**Imports nothing that can open a document.** `chemclaw.ingest.sources.registry` builds every
active retrieve half in the *chat* pod, so a module-scope `pypdf`/`python-docx`/`openpyxl` import
here would put the whole document-parsing stack in the process that serves conversations. That is
the defect D-118 measured for `calc` and fixed with the `specs.py`/`results.py` split; the same
split runs through this package, and `tests/test_datasource_isolation.py` holds it.

**The entitlement gate lives here, and it is the whole security model.** Getting onto this share is
an AD-group decision; once on it, everyone sees everything. So the honest enforcement is not
per-file ACLs — it is: *a caller who is not in the share's group gets nothing from this source at
all.* The check is against the turn's roles, which carry Entra app roles and, when
`entra_group_claims_as_roles` is set, each group claim namespaced with `GROUP_ROLE_PREFIX` — so an
AD group reaches this whether the tenant assigns it to an app role or emits it as a `groups` claim.
A group-gated binding names `group:<claim value>`; the bare object-id is not what lands in the role
set and would match nothing.

A gated share **refuses when there is no identity to check**, which is the `require_actor`
reject-if-absent rule (`agent/authz.py`) applied to a corpus instead of a tool. An *ungated* share
(`required_roles: []`) has nothing to verify and so needs no actor — that distinction is deliberate:
demanding an identity in order to check an empty requirement would block the report workflow for no
security benefit at all.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, time
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.core.identity_context import get_current_actor, get_current_roles
from chemclaw.ingest.documents.binding import (
    DocumentShareBinding,
    DocumentShareError,
    load_binding,
)
from chemclaw.ingest.documents.index import (
    DocumentFilter,
    DocumentHit,
    DocumentIndex,
    DocumentIndexError,
    DocumentText,
    default_document_index,
    require_schema_vector_width,
)
from chemclaw.ingest.documents.reassemble import join_chunks
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion

logger = logging.getLogger(__name__)


def _as_datetime(value: Any, *, end_of_day: bool) -> datetime | None:
    """Widen a `gather_evidence` date filter to a UTC datetime, or `None` if it is not a date.

    `gather_evidence` windows on whole days; a file's modification time is a timestamp. Widening
    `until` to the end of its day is what stops "until yesterday" from excluding everything that
    was touched after midnight — a filter that silently drops a day is worse than no filter.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        moment = time.max if end_of_day else time.min
        return datetime.combine(value, moment, tzinfo=UTC)
    return None


class ShareDocumentRetriever:
    """A `SourceRetriever` over one mounted share's indexed documents. One per data source."""

    def __init__(
        self, binding: dict[str, Any], name: str, index: DocumentIndex | None = None
    ) -> None:
        """Validate the binding at startup; `name` is the id every chunk is cited under.

        **`name` is required, and it used to default to `"sharedrive"`.** That default was the
        whole of a data-loss bug: the registry builds a half from `**manifest.config`, which
        carries no name, so *every* share took the literal — two mounted shares both answered
        `sharedrive`, `share_sources()` collapsed them to one entry, and the survivor's sweep
        deleted the other's rows. `chemclaw.ingest.sources.registry._build_retrieve_half` now
        stamps the manifest's name over whatever is passed here, so the production path cannot
        disagree with the folder; requiring it as well means a *direct* construction cannot
        silently claim to be a share it is not.

        Args:
            binding: The share's declared layout, from the manifest's `config:` block.
            name: The data-source name this share is indexed and cited under.
            index: The backend, injected by tests; production resolves the Postgres one lazily so
                importing this module opens no connection.
        """
        self._binding: DocumentShareBinding = load_binding(binding)
        # Refused at construction, not at the first write. The registry builds this half wherever
        # the source is enabled, so a deployment whose `embedding_dim` cannot fit the migrated
        # column learns it from a message naming both numbers rather than from pgvector rejecting
        # every chunk inside a worker, hours after a deploy that looked clean.
        require_schema_vector_width()
        self.name = name
        self._index = index

    def share_binding(self) -> DocumentShareBinding:
        """The share this retriever answers from — how the sync job finds what to crawl.

        The manifest declares one `binding:` and this half owns it, so the indexer reads the
        layout through the source it is indexing rather than reaching into the manifest itself.
        It is also the marker `durable.document_sync` matches on: a retrieve half with this method
        is a share that needs crawling, which keeps `CHEMCLAW_DATA_SOURCES` the only enable switch
        (D-018 — no second list of "and these ones are also document shares").
        """
        return self._binding

    def _backend(self) -> DocumentIndex:
        """The index, resolved on first use so construction stays free of I/O."""
        if self._index is None:
            self._index = default_document_index()
        return self._index

    def _entitled(self) -> bool:
        """Whether this turn's caller may see this share at all."""
        required = self._binding.required_role_set
        if not required:
            return True
        if get_current_actor() is None:
            logger.debug(
                "%s: no authenticated actor on this turn; returning no evidence", self.name
            )
            return False
        return bool(get_current_roles() & required)

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return the share's best-matching document chunks for `query`, best first.

        **Never raises**, and it stays that way now that the sweep would survive it anyway. A
        database the share index lives in being briefly unreachable must not take down answers the
        knowledge graph could have given on its own — the call `WarehouseVectorRetriever` and
        `VendoredDatasetRetriever` both made too.

        The *reason* this docstring used to give has expired and is corrected rather than deleted:
        `gather_evidence` fanned its retrievers out through a plain `asyncio.gather` with no
        `return_exceptions`, so one raising leg failed the whole question. It now sweeps them as
        graph branches that each degrade on their own (`chemclaw.retrieval.fanout`), so a raise
        here would cost this source and not the sweep. Answering emptily is still right — a source
        that knows why it found nothing can log it, where a caught exception one layer up cannot —
        but it is no longer the only thing standing between one outage and a dead turn.
        """
        if not query.strip() or not self._entitled():
            return []
        # `note_type` names a knowledge-graph note type. A report on a file share has no such type,
        # so a caller who asked for one is asking for something this source cannot honestly answer
        # — returning documents anyway would be ignoring the filter.
        if filters.get("type"):
            return []
        try:
            hits = await self._search(query, filters)
        # Ordered: `DocumentIndexError` is the narrower type and must be tested first. A backend
        # that timed out is transient and worth a warning; a binding that is wrong is permanent and
        # worth an error naming it as such.
        except (DocumentIndexError, ConnectionError, OSError, RuntimeError):
            logger.warning("%s: document search failed, returning no evidence", self.name)
            logger.debug("%s: search failure detail", self.name, exc_info=True)
            return []
        except DocumentShareError:
            logger.exception(
                "%s: misconfigured, returning no evidence — every query will do this until it is "
                "fixed",
                self.name,
            )
            return []
        except Exception:
            # What makes "never raises" above true rather than aspirational. `_search` embeds the
            # query through the provider seam, which raises its own client's exception types — an
            # `openai.APIError` is in neither list above, so a rate-limited embedding endpoint
            # escaped this leg and failed the whole turn. Naming that vendor's tree here would
            # import it; the contract is the promise in the docstring, so it is written as one.
            logger.exception("%s: unexpected search failure, returning no evidence", self.name)
            return []
        return hits

    async def read_document(self, doc_id: str) -> DocumentText | None:
        """Return one whole document of this share, or `None` when it cannot be read.

        **Why this exists.** A protocol is atomic — an SOP is one procedure and half of one is
        misleading rather than merely shorter — but the share stores documents cut into
        `chunk_chars` pieces, and `sync._read_and_parse` discards the parsed text once `doc_id` is
        taken from it. So the pieces are the document, and until now nothing read them back: a chunk
        hit cited `sharedrive:doc-9f2a…#3` and there was no way to ask for the other pieces of it.
        This is the reader for the address the retriever has been emitting all along.

        **Entitled exactly as `retrieve` is, and that is load-bearing rather than symmetric.** A
        whole-document read is a strictly larger disclosure than a ranked excerpt, so the share's
        one security decision — you are in the AD group or you see nothing — has to be asked here
        too, including the reject-if-absent rule for a gated share with no actor on the turn.

        **Never raises**, for `retrieve`'s reasons and one more: the caller is a turn assembling
        several protocols, and one unreadable document must cost that document rather than the
        answer. `None` means "could not be read"; a `DocumentText` with `truncated` set means "read,
        and there was more" — two different facts, kept apart, because a shortened document that
        does not say so reads as a complete one.
        """
        if not doc_id.strip() or not self._entitled():
            return None
        try:
            stored = await self._backend().stored_document(
                self.name, doc_id, self._binding.chunking_key
            )
        except Exception:
            logger.exception("%s: could not read document %s", self.name, doc_id)
            return None
        if stored is None or not stored.pieces:
            return None
        text = join_chunks([p.content for p in stored.pieces], self._binding.chunk_overlap_chars)
        ceiling = settings.document_read_max_chars
        return DocumentText(
            doc_id=doc_id,
            source=self.name,
            path=stored.path,
            text=text[:ceiling],
            chunks=len(stored.pieces),
            truncated=len(text) > ceiling,
            # First-seen order, deduped: the coordinates a reader can check the text against.
            coordinates=list(dict.fromkeys(p.coordinate for p in stored.pieces if p.coordinate)),
            modified_at=stored.modified_at,
        )

    async def _search(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Run both legs and fuse them by rank.

        Dense and lexical are fused with the same `reciprocal_rank_fusion` the cross-source layer
        uses, for the reason `D-2026-08-01-a-cap-that-starves-a-source` established: a cosine and a
        `ts_rank` are different quantities, and the only thing that can be compared between them is
        *position*. Fusing here rather than shipping two data sources keeps the share one source
        with one entitlement, which is what it actually is.
        """
        index = self._backend()
        document_filter = DocumentFilter(
            tag=str(filters.get("tag") or ""),
            since=_as_datetime(filters.get("since"), end_of_day=False),
            until=_as_datetime(filters.get("until"), end_of_day=True),
        )
        top_k = settings.retrieval_top_k
        # Offloaded, not called inline: under the `openai_compatible` provider `embed_texts` reaches
        # the LLM endpoint, and this runs on the one event loop serving every SSE stream. A stall
        # here would freeze conversations that have nothing to do with the share.
        embedded = (await asyncio.to_thread(embed_texts, [query]))[0]
        # Concurrently, for the reason `gather_evidence` fans its sources out that way: these are
        # two independent queries against the same database, so running them in sequence costs the
        # sum of their latencies where the maximum would do.
        dense, lexical = await asyncio.gather(
            index.search_dense(self.name, embedded, top_k, document_filter),
            index.search_lexical(self.name, query, top_k, document_filter),
        )
        fused = reciprocal_rank_fusion(
            [self._chunks(dense), self._chunks(lexical)], k=settings.retrieval_fusion_k
        )
        # The score is re-stated as the chunk's *position* in this source's own ranking, so the
        # value and the order cannot disagree. It is deliberately not a similarity: after fusing a
        # cosine with a `ts_rank` there is no similarity left to report, and `EvidenceChunk.score`
        # is documented to order one source's list and nothing wider.
        return [
            chunk.model_copy(update={"score": round(1.0 / (1 + position), 4)})
            for position, chunk in enumerate(fused[:top_k])
        ]

    def _chunks(self, hits: list[DocumentHit]) -> list[EvidenceChunk]:
        """Turn ranked index hits into citable evidence."""
        return [
            EvidenceChunk(
                content=hit.content,
                # Not a knowledge-graph note id and not pretending to be one: a citation has to
                # resolve to something a reader can check, and for a file on a share that is the
                # file. `WarehouseVectorRetriever` cites its rows the same way.
                source_note_id=f"{self.name}:{hit.doc_id}#{hit.ordinal}",
                retriever=self.name,
                score=hit.score,
                source=f"{hit.path} [{hit.coordinate}]" if hit.coordinate else hit.path,
            )
            for hit in hits
            if hit.content.strip()
        ]
