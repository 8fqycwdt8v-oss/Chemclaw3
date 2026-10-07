"""The retrieve half: answer questions from the indexed share, for callers entitled to see it.

Imports nothing that can open a document: the registry builds retrieve halves in the chat pod, which
must not load the parsing stack (`tests/test_datasource_isolation.py` holds it).

The entitlement gate is the whole security model: a caller not in the share's group gets nothing
from this source. Roles carry Entra app roles and, with `entra_group_claims_as_roles`, group claims
as `group:<claim value>` (`GROUP_ROLE_PREFIX`); a bare object id matches nothing. A gated share
refuses when there is no actor (`require_actor`'s reject-if-absent rule); an ungated share needs
none.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, time
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.core.identity_context import get_current_actor, get_current_roles
from chemclaw.ingest.documents.binding import DocumentShareBinding, load_binding
from chemclaw.ingest.documents.index import (
    DocumentFilter,
    DocumentHit,
    DocumentIndex,
    DocumentText,
    default_document_index,
    require_schema_vector_width,
)
from chemclaw.ingest.documents.reassemble import join_chunks
from chemclaw.retrieval.evidence import EvidenceChunk, RetrieverSkip
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion, restated_as_position

logger = logging.getLogger(__name__)


def _as_datetime(value: Any, *, end_of_day: bool) -> datetime | None:
    """Widen a `gather_evidence` date filter to a UTC datetime, or `None` if it is not a date.

    `until` widens to the end of its day, so "until yesterday" does not exclude files touched after
    midnight.
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

        `name` is required with no default: two shares sharing a name would collapse into one and
        one's sweep would delete the other's rows. The registry stamps the manifest's name here.

        Args:
            binding: The share's declared layout, from the manifest's `config:` block.
            name: The data-source name this share is indexed and cited under.
            index: The backend, injected by tests; production resolves the Postgres one lazily so
            importing this module opens no connection.
        """
        self._binding: DocumentShareBinding = load_binding(binding)
        # Refused at construction, so a width mismatch is a startup message naming both numbers
        # rather than pgvector rejecting every chunk in a worker later.
        require_schema_vector_width()
        self.name = name
        self._index = index

    def share_binding(self) -> DocumentShareBinding:
        """The share this retriever answers from — how the sync job finds what to crawl.

        Also the marker `durable.document_sync` matches on, so `CHEMCLAW_DATA_SOURCES` stays the
        only enable switch.
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

        An empty list means the share was asked and holds nothing matching; a backend failure is
        raised, and `fanout._sweep` degrades this one source and reports it as failed, so an outage
        never reads as "no precedent". A decision not to contribute (an unentitled caller, an
        unanswerable filter) is a declared `RetrieverSkip`, not `[]`. A blank query returns `[]`.
        """
        if not query.strip():
            return []
        if not self._entitled():
            raise RetrieverSkip(
                f"the {self.name} share requires an entitled actor and this turn carries none"
            )
        # `note_type` names a knowledge-graph note type, which a file on a share does not have;
        # honouring the filter means skipping.
        if filters.get("type"):
            raise RetrieverSkip(f"the {self.name} share cannot serve a note-type filter")
        return await self._search(query, filters)

    async def read_document(self, doc_id: str) -> DocumentText | None:
        """Return one whole document of this share, or `None` when it cannot be read.

        A protocol is atomic, and the stored chunks are the only copy of the document's text; this
        reads back the address `retrieve` cites. Entitled exactly as `retrieve` is, including
        reject-if-absent, since a whole document is a larger disclosure than an excerpt. Never
        raises, so one unreadable document costs only itself: `None` means unreadable, and
        `truncated` means read but cut short.
        """
        if not doc_id.strip() or not self._entitled():
            return None
        ceiling = settings.document_read_max_chars
        try:
            stored = await self._backend().stored_document(
                self.name, doc_id, self._binding.chunking_key, ceiling
            )
        except Exception:
            logger.exception("%s: could not read document %s", self.name, doc_id)
            return None
        if stored is None or not stored.pieces:
            return None
        text = join_chunks(
            [p.content for p in stored.pieces], self._binding.chunk_overlap_chars, ceiling
        )
        return DocumentText(
            doc_id=doc_id,
            source=self.name,
            path=stored.path,
            text=text[:ceiling],
            chunks=len(stored.pieces),
            # From the backend, which knows whether more pieces existed. Inferring it from the
            # assembled string would mean having built the thing the ceiling exists to avoid.
            truncated=stored.truncated or len(text) > ceiling,
            # First-seen order, deduped: the coordinates a reader can check the text against.
            coordinates=list(dict.fromkeys(p.coordinate for p in stored.pieces if p.coordinate)),
            modified_at=stored.modified_at,
        )

    async def _search(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Run both legs and fuse them by rank.

        Fused with `reciprocal_rank_fusion` because cosine and `ts_rank` are not comparable, only
        positions are. Fusing here keeps the share one source with one entitlement.
        """
        index = self._backend()
        document_filter = DocumentFilter(
            tag=str(filters.get("tag") or ""),
            since=_as_datetime(filters.get("since"), end_of_day=False),
            until=_as_datetime(filters.get("until"), end_of_day=True),
        )
        top_k = settings.retrieval_top_k
        # Offloaded: under `openai_compatible` the embedding call is network I/O, and this loop
        # serves every SSE stream.
        embedded = (await asyncio.to_thread(embed_texts, [query]))[0]
        # Concurrently: two independent queries, so the latency is the maximum rather than the sum.
        dense, lexical = await asyncio.gather(
            index.search_dense(self.name, embedded, top_k, document_filter),
            index.search_lexical(self.name, query, top_k, document_filter),
        )
        fused = reciprocal_rank_fusion(
            [self._chunks(dense), self._chunks(lexical)], k=settings.retrieval_fusion_k
        )
        # The score is restated as the chunk's position in this source's ranking, so value and order
        # cannot disagree.
        return restated_as_position(fused[:top_k])

    def _chunks(self, hits: list[DocumentHit]) -> list[EvidenceChunk]:
        """Turn ranked index hits into citable evidence."""
        return [
            EvidenceChunk(
                content=hit.content,
                # Not a knowledge-graph note id: a share citation resolves to the file a reader can
                # check.
                source_note_id=f"{self.name}:{hit.doc_id}#{hit.ordinal}",
                retriever=self.name,
                score=hit.score,
                source=f"{hit.path} [{hit.coordinate}]" if hit.coordinate else hit.path,
            )
            for hit in hits
            if hit.content.strip()
        ]
