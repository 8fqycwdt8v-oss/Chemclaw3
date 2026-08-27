"""Evidence retrieval (plan F10-A + the gather_evidence sweep budgets).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

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
    # behavior. A weight is applied in *rank* space (`k + rank / weight`) rather than as a
    # multiplier on the fused score — `retrieval.hybrid.reciprocal_rank_fusion` carries the
    # measurement of why, and it is the difference between "graph rank 3 fuses like rank 2" and
    # "the dense leg contributes nothing". ENV override is JSON, e.g.
    # CHEMCLAW_RETRIEVAL_SOURCE_WEIGHTS='{"graph": 1.5, "vector": 0.8}'.
    retrieval_source_weights: dict[str, float] = Field(default_factory=dict)
    # How much of a source note's body an excerpt carries — shared by the report harness's
    # evidence excerpts and the memory layer's procedure excerpts (one note-excerpt budget,
    # neutral name since both consume it), so the two cannot drift.
    note_excerpt_chars: int = Field(default=240, gt=0)
    # Cap on how many evidence chunks `gather_evidence` hands the agent in one sweep, so a broad
    # question over a large corpus fills only as much context as it needs (the agent narrows the
    # query or drills in with expand_note when the sweep is truncated).
    gather_evidence_max_chunks: int = Field(default=40, ge=1)
    # The same cap in the currency the count cannot express. `gather_evidence_max_chunks` counts
    # chunks whose sizes differ by roughly **7.5x** across sources — a note-backed chunk is
    # excerpted to `note_excerpt_chars` (240) while a mounted share's chunk is up to its binding's
    # `chunk_chars` (1,800) — and nothing normalised them, so 40 chunks is ~9.6 kB from the graph
    # and ~72 kB from a share. A count of things cannot bound anything, because what a thing costs
    # is whatever is in it: the same finding as `agent_keep_last_conversation_groups`, where the
    # count-only version left a 300k-token thread at 180k against a 100k budget.
    #
    # Both bounds apply, and the count is kept rather than replaced — it is ENV-visible and
    # deployments set it, which is `agent_keep_last_tool_groups`' argument for keeping a name whose
    # meaning has been refined. ~60,000 characters is ~15k tokens against
    # `agent_context_token_budget`'s 100,000.
    #
    # **Counted over the serialized chunk, not its `content`**, which is what makes that 15k figure
    # true. The first implementation charged `content` alone — measured, a 47% under-count on a
    # realistic chunk carrying conflicts and provenance, so this budget really spent about 114,000
    # characters. The default is unchanged because it was always right *for the payload*; what
    # changed is that it is now measured against the payload, so a share-heavy sweep keeps roughly
    # half as many chunks as it did — which is the behaviour this setting always claimed.
    #
    # Spent by walking the already round-robin (or RRF-fused) ranking, so it is spent
    # cross-source-fairly for exactly the reason the count is —
    # `D-2026-08-01-a-cap-that-starves-a-source` is about the *shape* of a cut, and a second cap
    # in a different currency applied the old way would reintroduce the starvation it fixed.
    gather_evidence_max_chars: int = Field(default=60_000, ge=1_000)
    # ── Condensing many whole protocols into one comparison (`agent.condense`) ────────────
    # Asking for similar reactions returns many protocols, and a protocol is atomic: it cannot be
    # split, so the unit that must fit is one whole procedure. These bound what a single turn may
    # condense, in the two currencies that actually bind — how many protocols, and how much text —
    # because either alone is unbounded in the other.
    #
    # `protocol_digest_max_chars` is one *map unit's* ceiling, ~6k tokens. An ELN procedure with a
    # charge sheet runs 3–8 kB, so this holds the ordinary case whole and refuses the outlier rather
    # than splitting it — a head-truncated protocol would return conditions with the outcome
    # silently missing, because yield and purity are at the *end*, and a row whose outcome looks
    # unmeasured against neighbours that measured it is worse than a row that says "not read".
    protocol_digest_max_chars: int = Field(default=24_000, ge=1_000)
    # How many protocols one turn-time call may take. Sized against `fingerprint_top_k` (10) and
    # `fingerprint_max_top_k` (100): two pages of similar reactions plus what the text sources add.
    # Small enough that the refusal above it is reachable in practice rather than theoretical.
    protocol_digest_max_protocols: int = Field(default=24, ge=1)
    # The other half of the same bound, in the currency the count cannot express. 24 x 24k is 576k
    # characters; a count alone would not bound that at all. This is the
    # `agent_keep_last_conversation_groups` lesson — a count of things cannot bound anything,
    # because what a thing costs is whatever is in it.
    protocol_digest_total_max_chars: int = Field(default=400_000, ge=10_000)
    # Map concurrency against one endpoint on the interactive path — `fan_out`'s role, on an
    # `asyncio.Semaphore` rather than on Temporal, because `durable.orchestrator.fan_out` starts
    # child *workflows* and is unreachable from a tool.
    protocol_digest_max_parallel: int = Field(default=4, ge=1)
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
    # How many disagreements one note's flag names, worst first (KM-8). The heuristic pairs notes
    # sharing a `(type, compound)`, and an optimization campaign is many runs on one substrate — so
    # on a programme-shaped 2,000-note corpus over 7 substrates the exhaustive scan produced 141,156
    # pairs and put ~141 ids on every evidence chunk reaching the model. A list that long is a fact
    # about the corpus, not a signal about the note; three is "the disagreements worth your eye",
    # which is what KM-8 asked for. Declared conflicts outrank suspected ones for the places, and
    # the count of everything is carried beside the list so a truncation never reads as the whole.
    conflict_max_per_note: int = Field(default=3, ge=1)
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

    # ── pgvector HNSW recall knobs ────────────────────────────────────────────────────────────
    # Applied as *transaction-local* Postgres settings on the connection running a dense query, and
    # only there: the lexical leg's `ts_rank` over a GIN index is exact and has no such parameter,
    # and an upsert is not a search. `chemclaw.core.db.apply_vector_recall_settings` is where they
    # are read, and **both** pgvector dense searches call it — the note index
    # (`chemclaw.retrieval.vector_index`) and the document index
    # (`chemclaw.ingest.documents.index`). Both, because the residual described below was measured
    # on the *document* index, and a knob wired only into the note index would have left the case
    # that motivated it untouched.
    #
    # **Why a knob exists at all.** With the HNSW index in use, an eligibility predicate (`within=`,
    # or the document index's `EXISTS` over `document_files`) is a *post* filter over the ef_search
    # candidate list rather than a bound on what the index scan considers, so a selective scope can
    # leave fewer than `retrieval_top_k` candidates alive and the search returns short.
    #
    # **Neither of these is the first thing to reach for, and saying so is the point.** The measured
    # cause of the large shortfalls was stale planner statistics, not ANN recall: before `ANALYZE`
    # the same statements went short on 13 of 20 and 20 of 20 queries; after it, 0 of 20 on the note
    # index and 1–2 of 20 on the document index. `ANALYZE` (autovacuum's, or by hand after a bulk
    # load) is the fix for the bulk of it. These two address only that small residual, which is why
    # both default to "leave the server alone" and neither changes a single query until an operator
    # sets it.
    #
    # **The document-index residual above did not reproduce when the knob was wired into that path**
    # (live PostgreSQL 16 / pgvector 0.8.0, 20,000 chunks, `ANALYZE`d): 20 queries × 6 filter
    # selectivities × 4 combinations of these two settings returned **0 of 480 short**. The plan is
    # the one the residual needs — an eligibility semi join *above* an HNSW scan, down to a tag
    # matching 10% of the corpus, below which the planner takes an exact plan instead — so the
    # hazard is real in shape and unobserved at this size. Reach for these after `ANALYZE` and after
    # measuring a shortfall you actually have, not on the strength of the number above.
    #
    # `0` means "do not set it" — pgvector's own default (40) stands and no extra round trip is
    # made. The `le` ceiling is *not* pgvector's maximum (1000): above roughly 200–400 the planner's
    # cost estimate for the index scan can exceed a sequential scan and it abandons the index
    # entirely, so a larger value silently buys the opposite of the recall it was set for. That
    # 200–400 band is sourced from the August-2026 external retrieval review, not measured here;
    # 400 is its upper edge, taken as the ceiling so the documented safe range is expressible and
    # the pathological range is not.
    hnsw_ef_search: int = Field(default=0, ge=0, le=400)
    # `off` (the default, and pgvector's) keeps today's behavior exactly. The other two make the
    # index scan keep walking until the filter is satisfied instead of stopping at the first
    # candidate window, which is the knob that addresses the `within=` residual above directly
    # rather than bluntly. **Requires pgvector >= 0.8** — the parameter did not exist before it, and
    # pgvector reserves the `hnsw.` prefix, so setting it on an older server is an error rather than
    # an ignored placeholder. That is why `off` emits no statement at all: a deployment on the
    # `pgvector >= 0.7` floor the fingerprint migrations state (`infra/sql/002`) keeps working
    # untouched, and only a deployment that opts in needs the newer server. The dev image
    # (`pgvector/pgvector:pg16`) and the live lane's pinned `v0.8.6` both satisfy it.
    # `strict_order` returns rows in exact distance order; `relaxed_order` may return them slightly
    # out of order in exchange for filling `top_k` sooner. `strict_order` is the one to reach for
    # first here: this index's hits are re-sorted by score downstream, but a relaxed scan changes
    # *which* rows come back, and a recall knob that also perturbs the ranking makes the next
    # measurement ambiguous.
    hnsw_iterative_scan: Literal["off", "strict_order", "relaxed_order"] = "off"

    @field_validator("retrieval_source_weights")
    @classmethod
    def _weights_are_positive(cls, value: dict[str, float]) -> dict[str, float]:
        """Refuse a zero or negative tier factor, which the fusion cannot express.

        A weight divides the rank, so `0` is a division by zero and a negative one inverts the
        source's own ordering — a deployment would be asking for its worst hit first. Both were
        also meaningless under the multiplier this replaced (`0` silently deleted a source from
        every sweep, which is precisely the starvation the knob is meant to prevent), so this is
        the config saying out loud what the arithmetic always required.
        """
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

    # The derived note index is only as good as its last rebuild (gap SCH-2). The graph changes
    # on every merged PR, and RRF fusion is score-agnostic, so a stale dense/lexical entry would
    # rank confidently beside live graph hits with no staleness signal. `NoteReindexWorkflow`
    # runs on this cadence; the interval is therefore also the worst-case staleness of the
    # derived legs.
    #
    # **Derived from the source list by default** (`None`), the move
    # `D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` made for connectors and for the
    # same reason: as an independent switch defaulting to off, enabling `vector`/`lexical` in
    # `CHEMCLAW_DATA_SOURCES` without also remembering this flag left both legs querying a
    # never-built index forever — `chunks: 0, failed: false` on every sweep, and the deployment
    # believed it ran hybrid retrieval. `note_reindex_effective` is what readers consult: an
    # explicit True/False still wins (a deployment that rebuilds its index out of band may opt
    # out), and unset means "reindex iff an index-backed note source is enabled".
    note_reindex_enabled: bool | None = None
    note_reindex_schedule_minutes: float = Field(default=60.0, gt=0)
    note_reindex_timeout_seconds: float = Field(default=600.0, gt=0)


# The `vector(N)` width every embedding column in this schema was migrated with —
# `note_index.embedding` (`infra/sql/012`) and `document_chunks.embedding` (`infra/sql/037`).
# Duplicated here rather than parsed out of the SQL because the migrations are the source of truth
# and this is the assertion against them.
#
# **One constant, not one per table.** There is no coherent deployment in which two vector columns
# in the same database have different widths: they are all written from `embedding_dim`, by one
# provider seam, and compared to queries embedded by that same seam. A second constant would be the
# same fact written twice, which is how the two come to disagree
# (`D-2026-08-05-one-rule-in-three-places-is-three-rules`).
SCHEMA_VECTOR_DIM = 1536

# The retrieve sources backed by `note_index`. Both of them, not just `vector`: `reindex_notes`
# embeds and upserts every row it writes regardless of which half will read it, so a `lexical`-only
# deployment reaches the `vector(N)` column exactly as a `vector` one does (DARK-8).
#
# Public because two things now need the same answer to "does this deployment use the derived
# index": the startup width check in the package `__init__.py`, and `chemclaw.evals.retrieval`,
# which refuses to report a graph-only figure when the deployment retrieves through the index
# instead.
NOTE_INDEX_SOURCES = frozenset({"vector", "lexical"})
