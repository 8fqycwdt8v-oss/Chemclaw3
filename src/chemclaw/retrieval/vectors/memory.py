"""The in-memory reference `VectorStore` — exact cosine, no server, no client package.

What every test of the composition above runs against, and the definition of what the adapters are
expected to agree with. `InMemoryDocumentIndex` plays the same role for the catalogue: the ranking
is computed in Python so a test exercises the real loop rather than a mock agreeing with itself.

Exact rather than approximate, which is the one way it differs from any real backend: a production
store answers from an ANN index (HNSW in pgvector and in Qdrant alike) and may miss a true neighbour
at high recall settings. The *ordering* is the same; the recall is not, and that difference is a
property of ANN search rather than of any adapter here.
"""

import math

from chemclaw.retrieval.vectors.base import VectorMatch, VectorPoint


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector.

    Clamped to [0, 1] for the reason `ingest.documents.index._cosine` documents at length: the
    denominator is two square roots and rounds below the numerator, so an *identical* vector scores
    fractionally above 1.0 about half the time, and `VectorMatch.score` is bounded `le=1.0`. A
    chemist pasting a sentence back is an exact match, and it must not raise.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return min(1.0, max(0.0, dot / norm)) if norm else 0.0


class InMemoryVectorStore:
    """Process-local `VectorStore` for tests and single-run use (the reference ranking)."""

    def __init__(self) -> None:
        """Start empty; points keyed by `(collection, id)` so two collections cannot collide."""
        self._points: dict[tuple[str, str], tuple[list[float], str]] = {}

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        """Insert or replace each point by id, remembering the group it belongs to."""
        for point in points:
            self._points[(collection, point.id)] = (point.vector, point.group_key)

    async def search(
        self,
        collection: str,
        embedding: list[float],
        top_k: int,
        groups: set[str] | None = None,
    ) -> list[VectorMatch]:
        """Rank by exact cosine within the scope; drop non-positive similarity, best first."""
        # A zero query vector is cosine 0 against everything — no hit, and never an ordering over
        # NaN distances. The same short-circuit both Postgres backends make before touching the
        # index.
        if not any(embedding):
            return []
        matches = [
            VectorMatch(id=point_id, score=_cosine(embedding, vector))
            for (held, point_id), (vector, group) in self._points.items()
            if held == collection and (groups is None or group in groups)
        ]
        # Tie-broken by id, so equal-similarity points order deterministically here and in any
        # adapter that copies the rule — the same `(-score, id)` the note index settled on.
        matches = [match for match in matches if match.score > 0.0]
        matches.sort(key=lambda match: (-match.score, match.id))
        return matches[:top_k]

    async def delete(self, collection: str, ids: list[str]) -> None:
        """Remove these points; absent ids are the state being asked for, not an error."""
        for point_id in ids:
            self._points.pop((collection, point_id), None)
