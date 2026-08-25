"""The retrieve half of a reaction corpus: precedents as cited evidence, and the drain's binding.

Two jobs in one class, and they are the two halves of what a corpus source *is*. As a
`SourceRetriever` it puts patent precedents into `gather_evidence`, so a report or a research
question reaches the literature the same way it reaches the graph. As a corpus source it carries
the `corpus:` binding, so `durable/corpus_sync.py` can find every enabled corpus by asking the
registry — the shape `document_sync.share_sources()` already has, and for the same reason: the
alternative is a second list of corpora somewhere in config, which would be a place to forget one.

**It lives in `ingest/` and not in `retrieval/` because of the import direction.**
`ingest -> retrieval` is an allowed edge and `retrieval -> ingest` is not, and this needs the
warehouse driver and the label index. `ingest/documents/retriever.py` sits where it does for
exactly the same reason.

**What it can answer, and what it cannot.** A reaction-SMILES query is answered from its *products*
— the corpus is asked which reactions made something like this, which is question 2's pre-pass and
the one genuinely useful thing a bulk corpus offers a research sweep. A prose query returns nothing,
which is the same contract `FingerprintReactionRetriever` keeps: each retriever answers only what
its source can, and answering prose with a keyword scan over 13 million patent reactions would be a
worse answer than none.
"""

import logging
from typing import Any

from chemclaw.core.chem import InvalidSmilesError
from chemclaw.core.config import settings
from chemclaw.ingest.eln.warehouse.binding import CorpusBinding, WarehouseBinding, load_binding
from chemclaw.ingest.eln.warehouse.connect import open_warehouse
from chemclaw.ingest.eln.warehouse.driver import Warehouse
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.science.fingerprints.store import FingerprintError
from chemclaw.science.labels.molecules import corpus_fingerprints
from chemclaw.science.labels.search import conditions_for_similar_products
from chemclaw.science.labels.store import LabelIndex, default_label_index

logger = logging.getLogger(__name__)


class WarehouseCorpusRetriever:
    """Precedent evidence from a bulk reaction corpus held in a SQL warehouse.

    Built by the data-source registry from a manifest's `config:` block, so its constructor
    signature *is* the manifest's schema — and `name` is required rather than defaulted for the
    reason `WarehouseVectorRetriever` records: the registry passes it, and a default would let a
    source be named twice with the two names disagreeing.
    """

    def __init__(self, binding: dict[str, Any], name: str, index: LabelIndex | None = None) -> None:
        """Validate the binding at worker startup, not on the first row that breaks it."""
        self.name = name
        self._binding: WarehouseBinding = load_binding(binding)
        if self._binding.corpus is None:
            raise ValueError(
                f"data source {name!r} declares a corpus retriever but its binding has no "
                "`corpus:` section; there is nothing for it to read"
            )
        self._index = index if index is not None else default_label_index()
        self._warehouse: Warehouse | None = None

    def corpus_binding(self) -> CorpusBinding:
        """The `corpus:` block, for the drain. Present by construction — the constructor checks."""
        assert self._binding.corpus is not None
        return self._binding.corpus

    def open(self) -> Warehouse:
        """The warehouse connection, opened once per process and reused.

        Lazily, because a chat pod builds this retriever and may never issue a query that reaches
        the warehouse — and because a driver that cannot connect must not stop a worker starting.
        """
        if self._warehouse is None:
            self._warehouse = open_warehouse(self._binding.connection)
        return self._warehouse

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Precedents for the products of a reaction-SMILES query; nothing for prose.

        `filters` is accepted and unused, deliberately. The note filters (`type`/`tag`/`since`/
        `until`) are about the knowledge graph's own metadata and a patent carries none of it — and
        a filter silently ignored is worse than one that cannot be expressed, so this is said here
        rather than left to a reader to discover from an unchanged result.
        """
        product = _product_of(query)
        if product is None:
            return []
        version = await self._index.current_version()
        if version is None:
            return []
        try:
            found = await conditions_for_similar_products(
                self._index, corpus_fingerprints(), version, product
            )
        except (FingerprintError, InvalidSmilesError):
            return []
        return [
            EvidenceChunk(
                content=_describe(hit),
                # The patent, not a note id: this corpus is evidence and its citation is a document
                # anyone can read (`D-2026-08-25-a-corpus-is-evidence-not-an-eln`). Prefixed with
                # the source name so a report's reviewer can see which corpus it came from, the
                # same shape `WarehouseVectorRetriever` uses.
                source_note_id=f"{self.name}:{hit.citation}",
                retriever=self.name,
                score=settings.fingerprint_similarity_threshold,
                source=f"{self.name}:{hit.source}:{hit.reaction_id}",
            )
            for hit in found.hits
        ]


def _product_of(reaction_smiles: str) -> str | None:
    """The principal product of a reaction SMILES, or `None` if this is not one.

    The *last* product when several are written, which is the convention every corpus in scope
    follows: a by-product is recorded after the thing the chemist was making. A guess, and a
    documented one — the alternative is asking about every product and returning a chunk per
    by-product, which pads a report with evidence about water.
    """
    parts = reaction_smiles.split(">")
    if len(parts) != 3:
        return None
    products = [p.strip() for p in parts[2].split(".") if p.strip()]
    return products[-1] if products else None


def _describe(hit: Any) -> str:
    """One precedent as the sentence a report quotes, conditions and recipe included."""
    conditions = [
        f"{hit.temperature_c:.0f} °C" if hit.temperature_c is not None else "",
        f"{hit.time_h:g} h" if hit.time_h is not None else "",
        f"{hit.yield_percent:.0f}% yield" if hit.yield_percent is not None else "",
    ]
    recipe = "; ".join(
        f"{role}: {', '.join(smiles)}" for role, smiles in sorted(hit.agents.items())
    )
    detail = ", ".join(part for part in conditions if part)
    named = hit.named_reaction or "reaction"
    return (
        f"{named} ({hit.citation}): {hit.reaction_smiles}"
        + (f" — {detail}" if detail else "")
        + (f" — {recipe}" if recipe else "")
    )
