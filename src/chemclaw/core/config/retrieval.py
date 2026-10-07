"""Settings for evidence retrieval and the `gather_evidence` sweep budgets.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

import math
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class RetrievalSettings(BaseSettings):
    """Evidence retrieval (plan F10-A + the gather_evidence sweep budgets).

    Grouped because these knobs tune how evidence reaches the agent: the hybrid (dense/lexical)
    retrievers' bounds and fusion mode, the sweep's chunk cap and rank-before-truncate scoring,
    the shared note-excerpt budget, and the parsed-graph cache. The embedding *provider* knobs
    live in the LLM section (they ride the LLM transport); these are the retrieval-behavior
    knobs.
    """

    # Dense and lexical retrievers are entry points into graph traversal (the git graph stays the
    # source of truth), enabled by membership in `data_sources`. `retrieval_top_k` bounds each one's
    # hits. `retrieval_mode`: `graph` is a flat union with dedup; `hybrid` fuses per-source rankings
    # by Reciprocal Rank Fusion with constant `retrieval_fusion_k`.
    retrieval_top_k: int = Field(default=8, gt=0)
    retrieval_mode: Literal["graph", "hybrid"] = "graph"
    retrieval_fusion_k: int = Field(default=60, gt=0)
    # Per-retriever weight in hybrid fusion, keyed by `EvidenceChunk.retriever`; absent means 1.0.
    # Applied in rank space (`k + rank / weight`, see `retrieval.hybrid.reciprocal_rank_fusion`).
    # JSON in the env, e.g. CHEMCLAW_RETRIEVAL_SOURCE_WEIGHTS='{"graph": 1.5, "vector": 0.8}'.
    retrieval_source_weights: dict[str, float] = Field(default_factory=dict)
    # Characters of a note body an excerpt carries, shared by report and memory excerpts.
    note_excerpt_chars: int = Field(default=240, gt=0)
    # Most evidence chunks one `gather_evidence` sweep returns; the agent narrows or uses
    # `expand_note` when truncated.
    gather_evidence_max_chunks: int = Field(default=40, ge=1)
    # The same cap in characters of serialized chunk, since chunk sizes differ several-fold by
    # source. Both bounds apply. Spent along the round-robin (or RRF) ranking so no source is
    # starved. Keep it equal to `agent_max_tool_result_chars`, or the tool-result cap cuts the
    # middle of the ranking.
    gather_evidence_max_chars: int = Field(default=60_000, ge=1_000)
    # ── Condensing whole protocols into one comparison (`agent.condense`) ──────────────────
    # A protocol cannot be split, so these bound a turn's condensation by count and by text.
    #
    # One map unit's ceiling (~6k tokens). A larger protocol is refused, not head-truncated,
    # because yield and purity come at the end.
    protocol_digest_max_chars: int = Field(default=24_000, ge=1_000)
    # Protocols one turn-time call may take: about two pages of similar reactions.
    protocol_digest_max_protocols: int = Field(default=24, ge=1)
    # Total characters across protocols; the count alone does not bound size.
    protocol_digest_total_max_chars: int = Field(default=400_000, ge=10_000)
    # Map concurrency against one endpoint on the interactive path, via an `asyncio.Semaphore`
    # (Temporal fan-out is unreachable from a tool).
    protocol_digest_max_parallel: int = Field(default=4, ge=1)
    # When a sweep exceeds its cap the highest-scored chunks are kept: graph hits by note
    # `confidence` (this default when absent), structural hits by similarity.
    retrieval_default_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # Confidence divergence at which `kg.conflicts` flags two same-compound, same-type notes as
    # worth a look; 0.3 is roughly "confident vs hedging".
    conflict_confidence_gap: float = Field(default=0.3, gt=0.0, le=1.0)
    # Disagreements one note's flag names, worst first, declared before suspected. The full count is
    # carried beside the list, so truncation never reads as complete.
    conflict_max_per_note: int = Field(default=3, ge=1)
    # Whether retrieval flags disagreeing notes at all; unflagged contradictions read as
    # corroboration. Switchable because detection walks the corpus per query.
    conflict_detection_enabled: bool = True
    # Cache the parsed knowledge graph, keyed by a stat fingerprint (path, mtime, size) so any note
    # change busts it. Off re-parses every call.
    graph_cache_enabled: bool = True
    # How long a fingerprint scan (O(notes) stats) is trusted before re-scanning. In-process writes
    # call `kg.graph.invalidate_cache()`, so this only delays out-of-process changes, which arrive
    # via the knowledge-sync sidecar every 300 s anyway. 0 re-scans every query.
    graph_cache_ttl_seconds: float = Field(default=60.0, ge=0.0)

    # ── pgvector HNSW recall knobs ────────────────────────────────────────────────────────────
    # Transaction-local settings applied by `chemclaw.core.db.apply_vector_recall_settings` in both
    # dense searches (`chemclaw.retrieval.vector_index`, `chemclaw.ingest.documents.index`). Under
    # HNSW an eligibility predicate post-filters the candidate list, so a selective scope can return
    # short. Run `ANALYZE` first; stale statistics are the usual cause. Both default to leaving the
    # server alone.
    #
    # `0` keeps pgvector's default (40) and sends nothing. The ceiling is below pgvector's 1000
    # because past ~200-400 the planner may abandon the index for a sequential scan.
    hnsw_ef_search: int = Field(default=0, ge=0, le=400)
    # `off` (pgvector's default) sends nothing. The others keep scanning until the filter is
    # satisfied; they need pgvector >= 0.8, where setting an unknown `hnsw.` parameter is an error.
    # `strict_order` keeps exact distance order and is the one to try first; `relaxed_order` may
    # change which rows come back.
    hnsw_iterative_scan: Literal["off", "strict_order", "relaxed_order"] = "off"

    @field_validator("retrieval_source_weights")
    @classmethod
    def _weights_are_positive(cls, value: dict[str, float]) -> dict[str, float]:
        """Refuse a zero, negative or non-finite tier factor, which the fusion cannot express.

        A weight divides the rank: `0` divides by zero, a negative inverts the ordering, NaN makes
        the sort meaningless (every comparison is False) and `+inf` erases a source's ordering.
        Refused, not clamped. There is no upper bound here: a large weight's harm depends on legs
        and cut, so `retrieval/hybrid.py::with_no_leg_cut_out` bounds the mix at the cut instead.
        """
        unordered = sorted(name for name, weight in value.items() if not math.isfinite(weight))
        if unordered:
            raise ValueError(
                f"retrieval_source_weights must be finite; {unordered} are not. A weight divides "
                "the rank, so NaN makes every fused score NaN — the ranking then degenerates to "
                "the order the sources happened to arrive in — and an infinity flattens its "
                "source's every rank onto one score"
            )
        bad = sorted(name for name, weight in value.items() if weight <= 0)
        if bad:
            raise ValueError(
                f"retrieval_source_weights must be positive; {bad} are not. A weight is a tier "
                "factor applied in rank space, so zero or less names no ordering at all"
            )
        return value

    @property
    def retrieval_source_weights_map(self) -> dict[str, float] | None:
        """The fusion weights, or `None` when unset — so the fusion keeps its uniform fast path."""
        return self.retrieval_source_weights or None

    # Rebuild the derived note index (`NoteReindexWorkflow`) on a cadence, which is also the
    # worst-case staleness of the dense and lexical legs. `None` derives it: reindex iff an
    # index-backed note source is enabled; an explicit value wins. Read via
    # `note_reindex_effective`.
    note_reindex_enabled: bool | None = None
    note_reindex_schedule_minutes: float = Field(default=60.0, gt=0)
    note_reindex_timeout_seconds: float = Field(default=600.0, gt=0)

    # Characters of one note's `search_text` embedded and indexed (~6k tokens, inside an 8,192-token
    # window), so one huge note cannot fail the whole reindex. Truncation only shortens that note's
    # vector and `tsvector`; other legs see it whole.
    note_embed_max_chars: int = Field(default=24_000, ge=1_000)
    # Notes per embed-and-upsert round; a failure costs only its batch.
    note_embed_batch_size: int = Field(default=64, ge=1)


# The `vector(N)` width every embedding column was migrated with (`note_index.embedding`,
# `document_chunks.embedding`); the assertion against the migrations. One constant because every
# vector column is written from `embedding_dim` by one provider.
SCHEMA_VECTOR_DIM = 1536

# Retrieve sources backed by `note_index`; both reach the `vector(N)` column because `reindex_notes`
# writes full rows. Used by the startup width check and `chemclaw.evals.retrieval`.
NOTE_INDEX_SOURCES = frozenset({"vector", "lexical"})
