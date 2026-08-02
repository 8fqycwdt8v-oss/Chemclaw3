"""Evidence retrieval (plan F10-A + the gather_evidence sweep budgets).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class RetrievalSettings(BaseSettings):
    """Evidence retrieval (plan F10-A + the gather_evidence sweep budgets).

    Grouped because these knobs tune how evidence reaches the agent: the hybrid (dense/lexical)
    retrievers' bounds and fusion mode, the sweep's chunk cap and rank-before-truncate scoring,
    the shared note-excerpt budget, and the parsed-graph cache. The embedding *provider* knobs
    live in the LLM section (they ride the LLM transport); these are the retrieval-behavior
    knobs.
    """

    # Dense-embedding and lexical (Postgres FTS) retrievers complement the graph/fingerprint
    # search as *entry points* into graph traversal (D-004: the git-markdown graph stays the
    # source of truth, embeddings are a derived index). They attach through the F7 data-source
    # registry (`data_sources`), so the enable switch is registry membership, not a second
    # boolean. `retrieval_top_k` bounds each new retriever's hits. `retrieval_mode` picks how
    # `gather_evidence` combines sources: `graph` (default) keeps today's flat union + dedup;
    # `hybrid` fuses the per-source rankings by Reciprocal Rank Fusion (`retrieval_fusion_k` is
    # the RRF constant) so a note surfaced by any single source rises, then graph expansion
    # (expand_note) remains the reasoning path.
    retrieval_top_k: int = Field(default=8, gt=0)
    retrieval_mode: Literal["graph", "hybrid"] = "graph"
    retrieval_fusion_k: int = Field(default=60, gt=0)
    # Per-retriever weight in the hybrid fusion (gap IDEA-5). RRF is score-agnostic, which is
    # right for combining heterogeneous *rankers* and wrong for combining heterogeneous
    # *evidence classes*: a validated internal ELN entry and a transferred analogy otherwise
    # fuse identically. Keys are retriever names as they appear on `EvidenceChunk.retriever`; an
    # absent retriever weighs 1.0, and an empty map (the default) is exactly today's uniform
    # behavior. ENV override is JSON, e.g. CHEMCLAW_RETRIEVAL_SOURCE_WEIGHTS='{"graph": 1.5,
    # "vector": 0.8}'.
    retrieval_source_weights: dict[str, float] = Field(default_factory=dict)
    # How much of a source note's body an excerpt carries — shared by the report harness's
    # evidence excerpts and the memory layer's procedure excerpts (one note-excerpt budget,
    # neutral name since both consume it), so the two cannot drift.
    note_excerpt_chars: int = Field(default=240, gt=0)
    # Cap on how many evidence chunks `gather_evidence` hands the agent in one sweep, so a broad
    # question over a large corpus fills only as much context as it needs (the agent narrows the
    # query or drills in with expand_note when the sweep is truncated).
    gather_evidence_max_chunks: int = Field(default=40, ge=1)
    # Rank-before-truncate for the evidence sweep (KM-5): when `gather_evidence` exceeds its cap
    # it keeps the highest-scored chunks, not an arbitrary disk-order slice. Graph hits score by
    # note `confidence` (this default when a note has none), structural hits by their similarity
    # — so a broad sweep drops the least-supported evidence first, not whatever parsed last.
    retrieval_default_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # How far two same-compound, same-type notes' stated confidences must diverge before
    # `kg.conflicts` calls the pair suspicious (KM-8). Not a claim that they conflict — a claim
    # that a reader should look. Set low and the flag is noise on every ordinary pair of notes;
    # set to 1.0 and only a certain/impossible pair trips it. 0.3 is roughly "one note is
    # confident and the other is hedging".
    conflict_confidence_gap: float = Field(default=0.3, gt=0.0, le=1.0)
    # Whether retrieval flags disagreeing notes at all. On by default — two contradictory notes
    # returned without comment read as corroboration, which is the failure KM-8 names. The switch
    # exists because detection walks the whole current corpus per query; a deployment measuring a
    # retrieval regression wants to be able to take it out of the picture.
    conflict_detection_enabled: bool = True
    # Cache the parsed knowledge graph so interactive retrieval does not re-read + re-parse the
    # whole `knowledge_dir` on every query (KM-14). The cache is keyed by a cheap stat
    # fingerprint of the note tree (path + mtime + size), so any add/edit/delete of a note busts
    # it — retrieval stays always-live. Off makes every call re-parse (the pre-cache behavior);
    # leave on in prod.
    graph_cache_enabled: bool = True
    # How long a fingerprint scan may be reused before the note tree is stat'd again (DA-5/D-1).
    # The fingerprint above is what makes the cache safe, but computing it is itself O(notes) —
    # a `stat` per file, ~75 ms at 10k notes on local disk and materially worse on a networked
    # OpenShift PVC — and every query pays it, even a pure cache hit. That scan is the floor on
    # interactive latency. Within this window the last scan is trusted and skipped, making a
    # warm query O(1); the cost is that a note changed by something *outside* this process
    # (another pod, an out-of-band `git pull`) can stay invisible for up to this long.
    #
    # Changes made *through* this process do not wait: the PR-gate submitter calls
    # `kg.graph.invalidate_cache()` after it writes a note, so the authoring loop stays instant.
    # `0` disables the window — every query re-scans, which is the exact pre-DA-5 behavior and
    # the setting to choose where any staleness is unacceptable.
    #
    # Raised from 5 s to 60 s. At 5 s a busy pod re-ran the O(notes) `rglob` almost continuously —
    # a load test with 50 concurrent sessions kept it permanently expired, so the "cache" paid its
    # full scan on essentially every `gather_evidence`/`find_notes`. Nothing was gained for that:
    # the only *in-process* writer (the PR-gate submitter) calls `invalidate_cache()` explicitly,
    # so this window governs out-of-process changes only — and those arrive via the knowledge-sync
    # sidecar, whose own refresh cadence is 300 s. A 5-second window could not make a merged note
    # visible any sooner than the sync that delivers it; it only bought scans.
    graph_cache_ttl_seconds: float = Field(default=60.0, ge=0.0)

    @property
    def retrieval_source_weights_map(self) -> dict[str, float] | None:
        """The fusion weights, or `None` when unset — so the fusion keeps its uniform fast path."""
        return self.retrieval_source_weights or None

    # The derived note index is only as good as its last rebuild (gap SCH-2). The graph changes
    # on every merged PR, and RRF fusion is score-agnostic, so a stale dense/lexical entry would
    # rank confidently beside live graph hits with no staleness signal. `NoteReindexWorkflow`
    # runs on this cadence; the interval is therefore also the worst-case staleness of the
    # derived legs. Only earns its Schedule when a hybrid leg is actually attached (registry
    # membership, D-018), so `note_reindex_enabled` keeps a graph-only deployment from running
    # an index it never reads.
    note_reindex_enabled: bool = False
    note_reindex_schedule_minutes: float = Field(default=60.0, gt=0)
    note_reindex_timeout_seconds: float = Field(default=600.0, gt=0)


# The `vector(N)` width in `infra/sql/012_note_index.sql`. Duplicated here rather than parsed
# out of the SQL because the migration is the source of truth and this is the assertion
# against it — the cross-section validator in the package `__init__.py` fails startup if the
# two disagree.
_NOTE_INDEX_VECTOR_DIM = 1536

# The retrieve sources backed by `note_index`. Both of them, not just `vector`: `reindex_notes`
# embeds and upserts every row it writes regardless of which half will read it, so a `lexical`-only
# deployment reaches the `vector(N)` column exactly as a `vector` one does (DARK-8).
#
# Public because two things now need the same answer to "does this deployment use the derived
# index": the startup width check in the package `__init__.py`, and `chemclaw.evals.retrieval`,
# which refuses to report a graph-only figure when the deployment retrieves through the index
# instead.
NOTE_INDEX_SOURCES = frozenset({"vector", "lexical"})
