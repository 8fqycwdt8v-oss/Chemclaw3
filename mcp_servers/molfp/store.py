"""Fingerprint store: persist molecule fingerprints, rank by similarity (plan 3.2/3.3).

One interface, swappable backend — the same shape as the calculation store (D-011):
an in-memory backend proves the ranking logic everywhere, and a Postgres backend does
the same Tanimoto ranking in SQL (HNSW-accelerated) for real corpora. Only `find_similar`
is backend-specific (Python vs SQL); everything else (computing the query fingerprint,
substructure matching) is shared in `mcp_servers.molfp.search`, so ranking behaviour is defined
once and identically across backends.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from mcp_servers.molfp.fingerprint import tanimoto


class MoleculeRecord(BaseModel):
    """A stored molecule: a stable id, its SMILES, and its ECFP4 bitstring."""

    id: str = Field(min_length=1)
    smiles: str = Field(min_length=1)
    bits: str = Field(min_length=1)


class Match(BaseModel):
    """A structural-search hit: the molecule and its Tanimoto similarity to the query."""

    id: str
    smiles: str
    similarity: float


@runtime_checkable
class FingerprintStore(Protocol):
    """Persistence + similarity-search contract. Backends implement this."""

    async def add(self, record: MoleculeRecord) -> None:
        """Insert or replace a molecule fingerprint by id."""
        ...

    async def all_records(self) -> list[MoleculeRecord]:
        """Return every stored record (used for substructure scans)."""
        ...

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Return up to `top_k` records with Tanimoto >= `threshold`, most similar first."""
        ...


class InMemoryFingerprintStore:
    """Process-local `FingerprintStore` for tests and single-run use.

    Computes exact Tanimoto ranking without a database — the reference the Postgres
    backend matches (its SQL uses the same threshold and tie-break, exactly for small
    corpora, up to HNSW recall for large ones). Keyed by record id, so re-adding an id
    replaces it.
    """

    def __init__(self) -> None:
        """Start with an empty index."""
        self._records: dict[str, MoleculeRecord] = {}

    async def add(self, record: MoleculeRecord) -> None:
        """Insert or replace a molecule fingerprint by id."""
        self._records[record.id] = record

    async def all_records(self) -> list[MoleculeRecord]:
        """Return every stored record."""
        return list(self._records.values())

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Rank stored records by Tanimoto to `query_bits`, filtered and truncated.

        Ties are broken by id so the ordering is deterministic (stable across runs and
        matching what a Postgres `ORDER BY similarity DESC, id` would return).
        """
        scored = [
            Match(id=r.id, smiles=r.smiles, similarity=tanimoto(query_bits, r.bits))
            for r in self._records.values()
        ]
        hits = [m for m in scored if m.similarity >= threshold]
        hits.sort(key=lambda m: (-m.similarity, m.id))
        return hits[:top_k]
