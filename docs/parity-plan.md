# Phase F10 — Platform-parity hardening (implementation tickets)

> **Companion to** `docs/implementation-tickets.md` (F0–F9). Same conventions: config-not-magic-numbers
> (`CHEMCLAW_` prefix, one `pydantic-settings` source, `extra="forbid"`), one ADR per decision, `make
> lint type test` green + the phase **CHECKMATE** (G1–G7) as the done-gate, durability stays in Temporal
> (D-002), new knowledge goes through the PR-gate (D-005/D-018), and **no abstraction without a second
> real caller** (Rule of Three).
>
> **Why this phase exists.** A capability comparison against a commercial pharma-agent *platform*
> (IntuitionLabs "Custom Pharma AI Agents") found Chemclaw at parity or ahead on the durability /
> identity / audit / delivery spine, with the deltas concentrated in **retrieval breadth**, **output
> verification**, **fine-grained authorization**, **agent orchestration topology**, and **audit /
> metrics polish**. This phase closes the deltas that add real value now and *records the trigger
> condition* for the ones the repo consciously defers ("defer until measured").
>
> **Ticket format** (unchanged): **Goal** · **Touch** (＋created / ~changed) · **Build** (real symbols +
> config keys) · **Test** (`tests/…` + behavior) · **Done when** · **Deps** · **ADR**.

---

## Disposition summary (competitor platform capability → Chemclaw gap → ticket → disposition)

| Competitor platform capability | Chemclaw today | Ticket | Disposition |
|---|---|---|---|
| RAG / vector retrieval — dense embeddings, chunking, **hybrid dense + BM25** | Graph traversal + binary structural fingerprints; **no dense semantic, no lexical rank** | **F10-A** | Build behind flag (executes+extends F8-T2) |
| Hallucination mitigation — citations, **confidence scores, verifier agent, confidence routing** | Mandatory citations + PR-gate + report-only deterministic `verify_claims`; **no LLM verifier, no confidence, no routing** | **F10-B** | Build behind flag |
| Tool-use governance — RBAC **at every tool invocation** | Universal *audit*; authorization only on expensive triggers | **F10-C** | Build now (default-allow, no behavior change) |
| **Multi-agent orchestration** — orchestrator + specialized sub-agents, **child workflows**, exactly-once | Single agent + multi-*activity* workflows; no child-workflow fan-out | **F10-D** | Build now (report/memory); gate the conversational mesh |
| **Model-agnostic per-task model selection** | One provider seam, single model | **F10-E** | Build now (default single model) |
| Quality metrics — **precision/recall/F1**, **drift detection**, ground-truth sets | Eval harness + absolute-error tolerances; no P/R/F1, no drift | **F10-F** | Build now |
| Audit — **tamper-evident hash-chain**, **bi-temporal** traceability | Append-only audit + Temporal history; hash-chain + bi-temporal designed-not-built | **F10-G** | Build now |
| Unstructured ingestion — **OCR / LLM-vision** | DataSource seam + ELN CDC-style; no OCR/vision | §Gated | Gate-until-trigger |
| Enterprise vendor connectors (Veeva/SAP/LIMS) | Generic `DataSource` registry (one adapter + one token) | §Gated | Gate-until-trigger (seam *is* the plan) |
| GAMP 5 validation pack / 21 CFR Part 11 certification artifacts | Technical substrate present (audit, traces, sign-off, evals) | §Gated | Gate-until-trigger (QA-owned, not code) |

**Guiding constraints for every ticket below.** New retrieval/verification/model paths ship **default-off**
so the classic behavior stays load-bearing (as F1's classic-agent fallback does). Graph traversal remains
the *reasoning* path — new retrievers are **entry points into the graph**, never a replacement (D-004
intact, exactly as F8-T2 framed it). Off-the-shelf over self-built: use Postgres-native FTS, the internal
OpenAI-compatible `/embeddings` endpoint, and LLM-as-judge structured output — no new NLI model, no new
datastore, no new orchestrator.

---

## F10-A — Hybrid retrieval: dense + lexical entry points, fused into graph traversal

> Executes and extends the already-planned **F8-T2** (derived pgvector index). Adds the missing **lexical
> (BM25-equivalent)** leg via Postgres FTS and a **fusion** step, so retrieval finds notes that share
> neither a substring nor a wikilink with the query. All three legs seed the existing graph expansion.

### F10-A1 — Embedding provider seam (mirror of the LLM seam)
- **Goal:** one place that builds an embedding client, config-selected, with an offline-deterministic dev
  fallback so tests never need a network.
- **Touch:** ＋`agents/embedding_provider.py`, ~`chemclaw/config.py`.
- **Build:** `def embed_texts(texts: list[str]) -> list[list[float]]` selecting on
  `embedding_provider: Literal["hash", "openai_compatible"] = "hash"`:
  - `openai_compatible` → the internal endpoint's `/embeddings` route (reuse `llm_base_url`/`llm_api_key`/
    `llm_tls_ca_bundle`), model `embedding_model`.
  - `hash` → a deterministic locality-insensitive hash embedder (stable, offline, for dev/CI only — never
    a production retrieval quality claim; documented as such).
  Config: `embedding_model: str = ""`, `embedding_dim: int = Field(default=1536, gt=0)`. This is the
  **only** place an embedding client class is imported (same rule as `agents/llm_provider.py`).
- **Test:** ＋`tests/test_embedding_provider.py` — `hash` returns stable `embedding_dim`-length vectors
  offline; `openai_compatible` (fake HTTP) returns endpoint vectors; provider switch is one config change.
- **Done when:** embeddings are obtainable offline (hash) and via the internal endpoint (fake), one seam.
- **Deps:** — · **ADR D-A10**.

### F10-A2 — pgvector note-embedding index + Postgres FTS (the two new legs)
- **Goal:** a derived, rebuildable index over knowledge-graph notes — dense embeddings **and** a lexical
  `tsvector` — with Git-Markdown still the source of truth (D-004).
- **Touch:** ＋`report/vector_index.py`, ＋`infra/sql/012_note_index.sql`, ~`report/retrievers.py`.
- **Build:**
  - `012_note_index.sql`: `note_index(note_id text primary key, embedding vector(<dim>), lexeme tsvector,
    updated_at timestamptz)`, HNSW `vector_cosine_ops` on `embedding`, GIN on `lexeme`. Wired into
    `make db-migrate` (D-034 ledger).
  - `report/vector_index.py`: `reindex_notes(notes)` (embed body + `to_tsvector`), an idempotent upsert;
    a `background-jobs` reindex activity triggered after a note merges (reuse the D-035 schedule pattern —
    add to `scripts/schedules.py`).
  - `report/retrievers.py`: `VectorRetriever` (cosine top-k) and `LexicalRetriever`
    (`websearch_to_tsquery` + `ts_rank`), both behind the **F7 source registry** so they auto-join
    `gather_evidence`. Each returns the existing `EvidenceChunk` type carrying `source_note_id` (no new
    DTO) so downstream citation/framing is unchanged (D-034 envelope preserved).
  - Config: `vector_index_enabled: bool = False`, `lexical_index_enabled: bool = False`,
    `retrieval_top_k: int = Field(default=8, gt=0)`.
- **Test:** ＋`tests/test_vector_index.py` — reindex N notes; a query with no shared substring/wikilink
  retrieves the semantically-related note via `VectorRetriever` (hash embedder makes it deterministic);
  `LexicalRetriever` ranks a term-overlap note above a non-match; both disabled → today's behavior. PG
  round-trip skips offline (mirrors `test_postgres_store.py`).
- **Done when:** dense + lexical retrieval work as registry retrievers; graph stays source of truth.
- **Deps:** F10-A1, F7-T2 · **ADR D-A10**.

### F10-A3 — Hybrid fusion (RRF), graph expansion stays the reasoning path
- **Goal:** fuse graph + fingerprint + vector + lexical hits into one ranked entry-set, then expand in the
  graph as today — a strict superset of the current single-retriever behavior.
- **Touch:** ＋`report/hybrid.py`, ~`agents/research_tools.py`.
- **Build:** `HybridRetriever` = **Reciprocal Rank Fusion** over the registry-declared retrievers
  (`retrieval_fusion_k: int = Field(default=60, gt=0)`), returning the top entry notes; `gather_evidence`
  then runs the existing 1–2 hop graph expansion from those entries (`expand_note`), so the *context* the
  agent sees is still graph neighborhoods, not raw top-k chunks. Config
  `retrieval_mode: Literal["graph", "hybrid"] = "graph"` (graph = today; hybrid = fused).
- **Test:** ＋`tests/test_hybrid.py` — a query where each leg surfaces a different true-positive: fusion
  returns all; graph expansion from the fused entries yields the same neighborhood contract as today;
  `retrieval_mode="graph"` reproduces current results exactly.
- **Done when:** hybrid retrieval is a superset of graph-only; graph traversal remains the reasoning path.
- **Deps:** F10-A2 · **ADR D-A10**.

> **CHECKMATE F10-A:** with `retrieval_mode="hybrid"` the agent retrieves a note that substring/wikilink
> retrieval provably misses, while graph expansion still supplies the reasoning context; all new paths
> default-off; F8-T2's intent (derived index as an *entry point*, D-004 intact) upheld. **ADR D-A10**
> (hybrid retrieval: dense + lexical entry points, RRF fusion, graph expansion unchanged).

---

## F10-B — Answer verification, confidence scoring & confidence routing

> Generalizes the report path's deterministic `verify_claims` (5b.4 — a claim survives only if it cites
> retrieved evidence) into (a) an **LLM verifier** that scores citation faithfulness on the conversational
> answer path, and (b) **confidence routing** into the *existing* human-in-the-loop hold (D-032). No new
> gate mechanism, no bespoke model.

### F10-B1 — Faithfulness verifier (LLM-as-judge, structured output)
- **Goal:** for a drafted answer + its retrieved evidence, check each factual sentence against a cited
  `<retrieved-note id=…>` chunk and emit a per-claim verdict + an aggregate confidence.
- **Touch:** ＋`agents/verifier.py`, ~`chemclaw/config.py`.
- **Build:** `verify_answer(answer: str, evidence: list[EvidenceChunk]) -> VerificationResult` where
  `VerificationResult(claims: list[ClaimCheck], confidence: float)` and
  `ClaimCheck(text, supported: bool, cited_note_id: str | None)`. Implemented as a **structured-output**
  LLM call (MAF `response_format`) on the cheap routed model (F10-E, task `"verifier"`), reusing the
  D-034 framing envelope so evidence is data-not-instructions. Deterministic fallback = the existing
  report `verify_claims` citation check when `verifier_enabled` is off. Config
  `verifier_enabled: bool = False`, `verifier_confidence_threshold: float = Field(default=0.7, ge=0, le=1)`.
- **Test:** ＋`tests/test_verifier.py` (fake structured-output client) — a fabricated claim → `supported=
  False`, low aggregate confidence; a fully-cited answer → all supported, high confidence. No network.
- **Done when:** every conversational answer can be scored for citation faithfulness; off = today.
- **Deps:** F10-E · **ADR D-A11**.

### F10-B2 — Confidence routing into the existing HITL hold + event contract
- **Goal:** low-confidence answers are flagged for human review instead of returned as authoritative;
  high-confidence proceed.
- **Touch:** ~`service/runner.py`, ~`service/events.py`, ~`agents/verifier.py`.
- **Build:** the runner calls `verify_answer` after a turn's final answer; when
  `confidence < verifier_confidence_threshold` it (a) stamps `AnswerEvent.confidence` +
  `AnswerEvent.unsupported_claims` (extend the `service/events.py` union) so the thin UI renders a review
  affordance, and (b) optionally opens an `InteractionApprovalWorkflow` (D-032) hold so the flag is
  durable past the turn. **No new approval primitive** — reuses the Yes/No seam. High-confidence answers
  are unchanged.
- **Test:** ~`tests/test_service.py` — a scripted low-confidence turn emits `AnswerEvent` with
  `confidence` + `unsupported_claims` and (when enabled) starts one approval hold; high-confidence emits a
  plain answer. Offline with fakes.
- **Done when:** low-confidence answers route to review; the routing reuses D-032; UI can render it.
- **Deps:** F10-B1, F2-T3, D-032 · **ADR D-A11**.

### F10-B3 — Report-section verification wiring (DRY with F10-B1)
- **Goal:** each drafted report section passes through `verify_answer`; unsupported sentences are marked
  in the PR-gated report note (not silently dropped, not invented).
- **Touch:** ~`report/harness.py` (route `verify_claims`→`verify_answer` when `verifier_enabled`),
  ~ the durable `DevelopmentReportWorkflow` per-section activity.
- **Build:** one verification function for both the conversational path and report sections (the report's
  deterministic check stays the default/fallback). Marked-unsupported sections carry a visible flag into
  the PR-gate note so the human reviewer sees them.
- **Test:** ~`tests/test_report_harness.py` — a section with an unsupported sentence is flagged (not
  removed, not fabricated); a fully-evidenced section passes clean.
- **Done when:** report + chat share one verifier; unsupported content is surfaced to the PR reviewer.
- **Deps:** F10-B1 · **ADR D-A11**.

> **CHECKMATE F10-B:** an answer/report with an unsupported claim is flagged with a confidence score and
> routed to the existing human hold; a grounded answer scores high and proceeds; the verifier reuses the
> report citation-check as its off-path fallback (DRY); default-off. **ADR D-A11** (answer verification +
> confidence routing; verifier runs on the cheap routed model).

---

## F10-C — Fine-grained per-tool authorization (generalize the single trigger gate)

> Today `authorize_trigger` guards only `submit_qm_job` (F4-T5). This makes authorization a **uniform
> middleware over every tool call**, mirroring the existing audit middleware — one interceptor, config-
> driven, default-allow so nothing regresses, enforced under `entra_required`.

### F10-C1 — Tool-authz middleware
- **Goal:** an agent cannot invoke a tool its role is not permitted, checked at **every** invocation, and
  the decision is audited.
- **Touch:** ＋`agents/tool_authz.py`, ~`agents/chemclaw_agent.py`, ~`chemclaw/config.py`.
- **Build:** `make_tool_authz_middleware(principal, gates)` — a MAF `@function_middleware` (same shape as
  `agents/audit.py`) that, before each tool runs, looks up `tool_role_gates: dict[str, list[str]]`
  (JSON config, tool-name → allowed roles) and calls the **existing** `agents/authz.authorize_trigger`
  logic (refactored so the middleware and the expensive-trigger call share one predicate — DRY, one authz
  function). Config `tool_role_gates: str = "{}"` (parsed), `tool_authz_default: Literal["allow","deny"]
  = "allow"`. Enforcement active only when `entra_required`; otherwise pass-through (dev). Attached in
  `build_agent` alongside the audit middleware. The expensive-trigger `authorize_trigger` call stays as
  defense-in-depth.
- **Test:** ＋`tests/test_tool_authz.py` — a role missing a gated tool is denied at invocation and the
  denial is audited; `default="allow"` + empty gates = current behavior; `default="deny"` + a gate list
  enforces an allowlist; `entra_required=False` never blocks.
- **Done when:** authorization is uniform per tool call, config-driven, default-safe, one authz predicate.
- **Deps:** F4-T5 · **ADR D-A12**.

> **CHECKMATE F10-C:** an unauthorized role is blocked from a specific tool at call time (not just the
> expensive trigger), audited, with zero behavior change by default and one shared authz predicate.
> **ADR D-A12** (per-tool authorization middleware supersedes the expensive-trigger-only scope of D-044).

---

## F10-D — Sub-agent orchestration via Temporal child workflows

> The competitor's "orchestrator + specialized sub-agents with exactly-once child workflows" — built only
> where a **concrete second caller** exists (Rule of Three): report-section drafting and memory synthesis
> already fan a task into independent steps. Orchestration is a **Temporal-layer** concern (layer
> separation intact); MAF stays the single conversational agent.

### F10-D1 — Generic child-workflow fan-out seam
- **Goal:** one typed helper to run N independent sub-tasks as child workflows with per-child retry +
  durability, reused by ≥2 callers.
- **Touch:** ＋`workflows/orchestrator.py`.
- **Build:** `async def fan_out(child, inputs: list[I]) -> list[R]` wrapping
  `workflow.start_child_workflow` with bounded parallelism and the existing bad-data retry policy
  (`workflows/publish.py::BAD_DATA_RETRY`). Each child input carries `requested_by` (F4-T3) so audit
  identity flows into every child. Runs on `background-jobs`.
- **Test:** ＋`tests/test_orchestrator.py` (Temporal test env, `tests/temporal_env.py`) — N inputs → N
  child workflows; one child failing retries/isolates without restarting the siblings; results preserve
  input order.
- **Done when:** a reusable, durable fan-out exists with per-child isolation.
- **Deps:** F4-T3 · **ADR D-A13**.

### F10-D2 — Adopt fan-out in the two real callers
- **Goal:** replace the sequential per-section / per-group loops with the child-workflow fan-out.
- **Touch:** ~ the durable `DevelopmentReportWorkflow` (report sections → child workflows), ~ the memory
  synthesis workflow(s) that back `memory/jobs.py` (`synthesize_campaigns`/`distill_playbooks` groups →
  children).
- **Build:** each section/group becomes a child workflow; parent aggregates. No behavior change to the
  section/group logic itself (still PR-gated notes, still cited) — only the execution topology gains
  parallelism + independent retry. Bounded by `orchestrator_max_parallel_children: int = Field(default=8,
  ge=1)`.
- **Test:** ~`tests/test_report_workflow.py` / memory workflow tests — a multi-section report spawns one
  child per section; killing a worker mid-section resumes only that child; a poison section rejects-and-
  continues (D-030 discipline) without losing the others.
- **Done when:** report + memory synthesis run as orchestrated child workflows; durability per child.
- **Deps:** F10-D1 · **ADR D-A13**.

> **Gated (NOT this phase): conversational multi-agent mesh.** A super-agent routing a *single turn* to
> multiple specialist MAF agents. **Trigger:** a use case needs >1 specialist persona within one turn that
> role-scoped skills (D-052) cannot express. Until then the single agent + on-demand skills is the KISS
> answer; building an agent mesh now would be a one-caller abstraction.

> **CHECKMATE F10-D:** report sections and memory groups run as exactly-once child workflows with per-
> child retry and worker-restart durability, via one shared `fan_out`; the conversational mesh is deferred
> with a written trigger. **ADR D-A13** (Temporal child-workflow orchestration for fan-out jobs).

---

## F10-E — Per-task model routing

> Enabler for F10-B (verifier on a cheap model) and future high-throughput steps. Keeps the single-seam
> rule: still the only place a chat-client class is imported.

- **Goal:** select the model per task (frontier for reasoning, cheaper for verify/classify), one config
  table, default = today's single model.
- **Touch:** ~`agents/llm_provider.py`, ~`chemclaw/config.py`.
- **Build:** `build_chat_client(task: str = "agent")` consults `model_routes: str = "{}"` (JSON, task →
  model id) and falls back to `llm_model`/`agent_model`. Tasks in use: `agent` (frontier), `verifier`,
  `classify`, `narrative`. Same `base_url`/credential (per-task = per-model on the one internal endpoint);
  generalizes the F0-T4 `llm_planning_model` mitigation knob. No second import site.
- **Test:** ~`tests/test_llm_provider.py` — `build_chat_client("verifier")` yields the routed model;
  unknown task → default; empty `model_routes` = current behavior.
- **Done when:** per-task model selection is one config change; the seam stays single-import.
- **Deps:** F0-T2 · **ADR D-A11** (folded — the routing exists to serve the verifier).

---

## F10-F — Quality metrics: precision/recall/F1 + drift detection

> Extends the existing eval harness (2b) and the planned autonomy evals (F9-T3) with classification
> metrics and a scheduled drift check — reusing the `@metric` registry and the A/B noise-floor idea, no
> new eval system.

### F10-F1 — Classification metrics + retrieval eval cases
- **Goal:** precision/recall/F1 for retrieval/extraction, scored against ground-truth sets in case
  frontmatter.
- **Touch:** ~`evals/metrics.py`, ~`evals/metric.py` (if a set-typed result is needed),
  ＋`evals/cases/retrieval_*.md`.
- **Build:** `@metric` `precision`, `recall`, `f1` over a case's `expected_note_ids` vs the retriever's
  returned ids; retrieval cases pin queries → expected relevant notes (scores F10-A). Reuse
  `render_report` for citable output.
- **Test:** ＋`tests/test_metrics_classification.py` — P/R/F1 compute correctly on a scripted
  predicted-vs-expected set; a retrieval case scores the hybrid retriever.
- **Done when:** retrieval/extraction quality is measured as P/R/F1 on versioned cases.
- **Deps:** F10-A3 · **ADR D-A14**.

### F10-F2 — Drift-detection job
- **Goal:** catch silent quality regressions by re-running the committed case-set on a cadence and
  alerting when a metric falls outside a noise band vs a stored baseline.
- **Touch:** ＋`workflows/eval_drift.py`, ＋`evals/baseline.py` (committed baseline scores),
  ~`scripts/schedules.py`, ~`chemclaw/config.py`.
- **Build:** a `background-jobs` workflow that runs `run_eval`, compares aggregate metrics to
  `evals/baseline.json` (Git-committed), and on a regression beyond `eval_drift_epsilon` emits a
  `session_event` / audit alert (reuse `workflows/notify.py`). Config `eval_drift_enabled: bool = False`,
  `eval_drift_schedule_minutes: int = Field(default=1440, ge=1)`,
  `eval_drift_epsilon: float = Field(default=0.05, ge=0)` (mirrors `eval_ab_epsilon`). Scheduled via the
  D-035 `apply_schedules` path.
- **Test:** ＋`tests/test_eval_drift.py` — a seeded score regression beyond epsilon raises an alert;
  within-band noise does not; baseline round-trips.
- **Done when:** a scheduled job flags eval regressions beyond the noise floor.
- **Deps:** F10-F1, D-035 · **ADR D-A14**.

> **CHECKMATE F10-F:** retrieval/extraction report P/R/F1 on versioned cases; a seeded regression trips a
> scheduled drift alert while within-noise changes stay quiet. **ADR D-A14** (classification metrics +
> drift detection on the existing eval harness).

---

## F10-G — Audit hardening: tamper-evident hash-chain + bi-temporal note fields

> Completes what D-034 explicitly left "for Phase 6" (the audit hash chain) and what `architektur.md`
> §10.4 proposed but never schematized (bi-temporal note fields). Both are low-complexity, GxP-relevant.

### F10-G1 — Hash-chained audit events
- **Goal:** make the append-only audit log tamper-evident, so any altered/removed row is detectable.
- **Touch:** ~`agents/audit_store.py`, ＋`infra/sql/011_audit_hash_chain.sql`,
  ＋`scripts/verify_audit_chain.py`, ~`Makefile` (`audit-verify`).
- **Build:** `011_…sql` adds `prev_hash text`, `row_hash text` to `audit_events` (new migration — never
  edit an applied file, per D-034 drift rule). `PostgresAuditSink` computes `row_hash =
  stable_hash(prev_hash || canonical(event))` on insert (reuse `chemclaw.ids.stable_hash`, so one hashing
  scheme — D-033). `verify_audit_chain.py` walks rows in order and reports the first broken link. Chain is
  always-on when a PG sink is configured (no toggle); the `NullAuditSink` default is unaffected.
- **Test:** ＋`tests/test_audit_chain.py` — a clean chain verifies; mutating one stored row's args makes
  the verifier report the exact break point; the hashing reuses `stable_hash` (no new hash helper).
- **Done when:** tampering with the audit store is detectable by `make audit-verify`.
- **Deps:** D-034 · **ADR D-A15**.

### F10-G2 — Bi-temporal note frontmatter
- **Goal:** notes can record *what we knew and when it was valid* ("what did we know at time T").
- **Touch:** ~`kg/note.py`, ~`kg/validate.py`.
- **Build:** add optional `valid_from: date | None = None`, `valid_to: date | None = None` to the note
  schema (trivial, per architektur §10.4); `kg-validate` rejects `valid_to < valid_from`. Retrievers may
  later filter on these (out of scope here — schema + validation only, no premature consumer).
- **Test:** ~`tests/test_note.py` + ~`tests/test_kg_validate.py` — notes round-trip the two fields;
  reversed interval is rejected; absent fields behave as today.
- **Done when:** notes carry validated bi-temporal fields; existing notes unaffected.
- **Deps:** — · **ADR D-A15**.

> **CHECKMATE F10-G:** an altered audit row is caught by chain verification; notes support validated
> bi-temporal fields; both reuse existing primitives (`stable_hash`, the note schema, `kg-validate`).
> **ADR D-A15** (audit hash-chain + bi-temporal notes).

---

## Gate-until-trigger (documented, deliberately NOT built this phase)

Respecting the repo's "off-the-shelf / defer until measured" discipline (D-018 / DEFERRED.md), these are
recorded with their trigger conditions rather than pre-built:

- **OCR / LLM-vision ingestion.** *Trigger:* a real scanned-notebook / legacy-PDF source attaches via the
  F7 `DataSource` seam. *Then:* a `map_to_ord` half using an off-the-shelf OCR (e.g. `docling`/
  `pytesseract`) with an LLM-vision fallback on the routed model (F10-E) — **inside one adapter**, no new
  subsystem. No universal document pipeline ahead of a real document.
- **Enterprise vendor connectors (Veeva Vault / SAP / LIMS).** *No platform work needed:* the F7 registry
  already makes each "one adapter + one config token" (proven by the ELN re-host). *Trigger:* a real
  target system; the first live connector remains the deferred custom Snowflake ELN source (F7 backlog).
- **GAMP 5 validation pack / 21 CFR Part 11 certification artifacts.** These are **process deliverables,
  not code** — a Validation Plan, GxP-impact risk assessment per component, and IQ/OQ/PQ protocols
  authored *against* the technical substrate Chemclaw already emits (append-only + hash-chained audit
  trail, logged reasoning traces, PR-gate human sign-off, versioned eval evidence). *Trigger:* a regulated
  deployment; *owner:* QA/validation, not this repo. Chemclaw's job is to keep emitting the substrate
  (F10-G strengthens it), not to self-certify.
- **Conversational multi-agent mesh** (see F10-D gated note).

---

## Sequencing & critical path

```
F10-E (model router, tiny) ─► F10-B (verifier + confidence routing)
F10-A1 ─► F10-A2 ─► F10-A3 (hybrid retrieval) ─► F10-F1 ─► F10-F2 (metrics + drift)
F10-C (per-tool authz) ── independent ──┐
F10-G (audit hash-chain + bi-temporal) ─┴─ independent, quick wins
F10-D1 ─► F10-D2 (orchestration) ── independent, largest; schedule after the quick wins
```

Recommended order: **F10-E → F10-C, F10-G (quick, independent) → F10-A → F10-B → F10-F → F10-D.**
Each ticket ends green (`make lint type test` + its new tests) and default-off where it changes a path.

---

## Appendix A — New config keys (all `CHEMCLAW_`-prefixed, in `chemclaw/config.py`)

| Ticket | Keys |
|---|---|
| F10-A | `embedding_provider`, `embedding_model`, `embedding_dim`, `vector_index_enabled`, `lexical_index_enabled`, `retrieval_top_k`, `retrieval_mode`, `retrieval_fusion_k` |
| F10-B | `verifier_enabled`, `verifier_confidence_threshold` |
| F10-C | `tool_role_gates`, `tool_authz_default` |
| F10-D | `orchestrator_max_parallel_children` |
| F10-E | `model_routes` |
| F10-F | `eval_drift_enabled`, `eval_drift_schedule_minutes`, `eval_drift_epsilon` |

## Appendix B — New SQL migrations (`infra/sql/`, after `009_session_events.sql`)

`012_note_index.sql` (pgvector `embedding` + `tsvector` `lexeme`, HNSW + GIN) · `011_audit_hash_chain.sql`
(`prev_hash`/`row_hash` columns on `audit_events`). Both wired into `make db-migrate` (D-034 ledger).

## Appendix C — New top-level modules

`agents/embedding_provider.py` · `agents/verifier.py` · `agents/tool_authz.py` · `report/vector_index.py`
· `report/hybrid.py` · `workflows/orchestrator.py` · `workflows/eval_drift.py` · `evals/baseline.py` ·
`scripts/verify_audit_chain.py`.

## Appendix D — ADRs introduced

`D-A10` hybrid retrieval · `D-A11` answer verification + confidence routing + per-task model routing ·
`D-A12` per-tool authorization middleware (supersedes D-044 scope) · `D-A13` Temporal child-workflow
orchestration · `D-A14` classification metrics + drift detection · `D-A15` audit hash-chain + bi-temporal
notes. Recorded in `DECISIONS.md` at implementation time (next running-log numbers), one per CHECKMATE.
