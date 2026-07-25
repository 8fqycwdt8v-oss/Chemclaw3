"""External literature as one more retriever behind the existing seam (gap TOOL-6).

`DEFERRED.md` deferred external retrievers with the trigger *"after Phase 5b core; add as one more
retriever behind the same interface"*. Phase 5b completed, F7 shipped the source registry and F10-A
shipped the fusion — the seam was finished and empty. Process-R&D questions ("has anyone run this
coupling on a chloro-pyridine") are literature questions at least as often as internal-corpus ones,
and a new deployment's internal corpus is empty by construction.

**The source decision, made explicitly.** This was the one item genuinely blocked on a choice rather
than on work. The choice made here is **PubChem PUG-REST**, because it is the only option that
clears every constraint this repo actually has:

- *Public and licence-clean* — NCBI/NLM data, usable without a negotiated agreement, unlike Reaxys
  or SciFinder which need a site licence this repo cannot assume.
- *No credential* — every other external integration in this codebase (LLM, Nextflow, Entra) needs
  a secret; one that does not is far easier to enable safely.
- *Chemistry-shaped* — it answers by *structure*, which is what this system already speaks, so a
  hit joins the corpus on the same key the fingerprint index uses.

Reaxys/SciFinder/an internal mirror remain perfectly reasonable and are **one more class here**:
that is exactly what the retriever seam buys, and swapping the source is a config change plus a
sibling class, not a re-architecture.

**Off by default and never load-bearing.** It attaches only when a deployment adds `literature` to
`data_sources`, honours the NetworkPolicy (a cluster that does not allow the egress simply gets no
hits), and any failure degrades to *empty* rather than failing the sweep — external evidence must
never be able to take down an answer the internal corpus could already give.

Every chunk is framed as external and marked `retriever="literature"`, so IDEA-5's source weighting
can rank it below measured internal evidence — the mechanical expression of "evidenced history and
transferred analogy stay separate".
"""

import logging
from typing import Any
from urllib.parse import quote

import httpx

from chemclaw.chem import InvalidSmilesError, require_canonical_smiles
from chemclaw.config import settings
from report.evidence import EvidenceChunk

logger = logging.getLogger(__name__)


class PubChemLiteratureRetriever:
    """Look a structure up in PubChem and return its identity + literature-facing summary.

    Retrieves by *structure*, not by free text: a SMILES in the query is the reliable join key, and
    PubChem's full-text search over an arbitrary chemistry question returns noise. When the query
    carries no parseable structure this returns empty rather than guessing — the same conservatism
    `resolve_compound` applies, for the same reason.
    """

    name = "literature"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Use an injected HTTP client (tests pass a fake transport), else build one per call."""
        self._client = client

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return external chunks for any structure in `query`; empty on anything unexpected."""
        smiles = self._structure_in(query)
        if smiles is None:
            return []
        try:
            payload = await self._fetch(smiles)
        except Exception:
            # External evidence must never be able to sink an answer the internal corpus could
            # already give — a timeout, a blocked egress, or a schema change all degrade to empty.
            logger.info(
                "literature lookup failed for %s; continuing without it", smiles, exc_info=True
            )
            return []
        return self._chunks(smiles, payload)

    @staticmethod
    def _structure_in(query: str) -> str | None:
        """The first token in `query` that parses as a structure, or None."""
        for token in query.split():
            try:
                return require_canonical_smiles(token)
            except InvalidSmilesError:
                continue
        return None

    async def _fetch(self, smiles: str) -> dict[str, Any]:
        """Fetch PubChem's compound record for a SMILES."""
        url = (
            f"{settings.literature_base_url}/compound/smiles/{quote(smiles, safe='')}"
            "/property/IUPACName,MolecularFormula,MolecularWeight/JSON"
        )
        if self._client is not None:
            response = await self._client.get(url)
        else:
            async with httpx.AsyncClient(timeout=settings.literature_timeout_seconds) as client:
                response = await client.get(url)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def _chunks(self, smiles: str, payload: dict[str, Any]) -> list[EvidenceChunk]:
        """Map a PubChem property record to evidence chunks, one per matched compound."""
        properties = payload.get("PropertyTable", {}).get("Properties", [])
        chunks: list[EvidenceChunk] = []
        for entry in properties[: settings.retrieval_top_k]:
            cid = entry.get("CID")
            if cid is None:
                continue
            name = entry.get("IUPACName", "")
            formula = entry.get("MolecularFormula", "")
            weight = entry.get("MolecularWeight", "")
            chunks.append(
                EvidenceChunk(
                    content=(
                        f"PubChem CID {cid}: {name or smiles} "
                        f"({formula}, MW {weight}). External reference, not an internal result."
                    ),
                    # The citation is the external identifier, deliberately *not* a note id: it must
                    # be visibly un-internal so a reader can never mistake it for a merged note.
                    source_note_id=f"pubchem-cid-{cid}",
                    retriever=self.name,
                    # Below the neutral 0.5: an external record is context, not evidence this
                    # organisation produced. IDEA-5's weights can lower it further per deployment.
                    score=0.3,
                )
            )
        return chunks
