# DEFERRED

Consciously postponed items — each with the reason it is *not now* and the trigger that would
revisit it. Default is "off-the-shelf, defer until measured".

| Item | Why not now | Trigger to revisit |
|---|---|---|
| **HPC/DFT real integration** (SLURM, `submit_to_hpc`) | User deferred it; the mock spine proves the durable pattern and the early compute (xTB/GFN2 + ML predictors, Phase 1c) covers near-term needs locally | Heavy QM/DFT accuracy is genuinely required **and** HPC access is provisioned |
| Postgres RLS mirror of the graph | Broad internal read access is fine for cross-project learning; a mirror adds a sync pipeline + a second source of truth | Real, combinatorial project-level confidentiality requirements |
| `knowledge/` as its own Git repo | A subfolder is enough for v1 | Governance/confidentiality boundary requires repo split |
| Second queue system (pg-boss) | Temporal already runs; a second task queue covers small jobs | — (decided against, see D-006) |
| MAF Durable Extension for jobs | Temporal owns durability; Azure-Functions-native and job-inappropriate | Only very long *conversation* pauses (days awaiting human input) |
| Universal ELN abstraction | Individuality of each ELN can't be abstracted away up front | At the third real ELN source, generalize shared adapter bits |
| External literature/patent retrievers | Internal knowledge+data harness comes first | After Phase 5b core; add as one more retriever behind the same interface |
| Tabular Foundation Model (TabPFN/TabICL) tool | The "which experiment next" question is now answered by BoFire inline (`suggest_next_experiment`, D-024); a tabular FM is a *different*, non-critical capability (few-shot numeric prediction from a table) and needs a model download + license check | When few-shot numeric-trend prediction over historic tables is a real need; check the model version's license first |
| ~~Conversation-history trimming in the agent~~ | **Done (D-025):** MAF `CompactionProvider` — token-budget-triggered tool-result collapse + sliding window, LLM-free | — |
| LLM *summarization* of compacted history (`SummarizationStrategy`) | The deterministic collapse+window (D-025) reclaims context without an LLM call, and MAF flags an untrusted summarizer as an indirect-prompt-injection risk that persists in history | Token-frugal collapse proves insufficient (essential older context lost) **and** a trusted summarization client is available — then add it as the first strategy in the composed budget |
| ~~xTB semiempirical~~ | **Pulled forward → Phase 1c** (now a primary early calculator, GFN2), not a DFT pre-filter | — (decided in, D-010) |
| Retrosynthesis + reaction prediction | Not on the spine-first critical path; big domain module | After the spine + graph + fingerprint layers exist; when route planning is a real user need |
| ~~DoE / Bayesian optimization~~ | **Pulled forward → Phase 1d** (BoFire as BO engine), drives which calculations are worth running | — (decided in, D-012) |
| Lab automation / SiLA2 closed-loop | Requires real instrument integration; out of v1 scope | When physical/robotic execution enters scope |
| Process flowsheet synthesis/simulation | Separate capability area (e.g. Aspen HYSYS) | When process-design (not just reaction) is in scope |
| Multimodal analytical data (spectra/images) | Adds modality-specific ingestion complexity | When analytical raw data beyond structured fields is needed |
| Domain foundation models | Heavy; general LLM + tools suffices for v1 | When task accuracy plateaus and a domain model is justified |
| Sub-quadratic playbook clustering | `memory/playbook.py` pairwise Tanimoto is O(n²) — fine for the current corpus, and simple/exact | ~10⁴ reactions (~10⁸ comparisons per run); switch to per-reaction Postgres HNSW k-NN |
| Per-key in-flight dedup in the calc store | Two *concurrent* misses on one key both compute (benign last-writer-wins upsert); serializing needs cross-process locking | Duplicate expensive runs (real HPC/DFT) become measurable cost |
| Wire the ORD adapter into the durable `ElnSyncWorkflow` | The workflow runs the one real source (`JsonExportAdapter`); `OrdJsonAdapter` satisfies the same `ElnAdapter` contract and is proven through `sync_entries`, but no ORD-exporting source is connected yet | A real ORD feed exists — then run both adapters (or a composite) on the background queue, each with its own cursor |
| Per-step species linking from free-text prose | Prose steps carry no `components` — guessing a SMILES from a name mid-sentence would fabricate structure; the coarse `StepKind` label and per-step temp/time are the deterministic floor | Wire the `eln-reaction-extraction` skill's per-field LLM to resolve named reagents per step (name→SMILES tool), still PR-gated |
| Durable multi-step "deep research" as a Temporal workflow | Research is interactive Q&A (MAF's job); `gather_evidence` + tools + the `deep-research` skill cover it conversationally without a durable job | A single research question needs many expensive fan-out steps (broad literature sweeps, batch computations) that must survive a restart |
| Compound notes so molecule/substructure hits cite a note directly | Molecules are indexed by SMILES, not yet as graph notes; `find_substructure_matches` returns SMILES and the agent bridges to reactions via `find_notes` | Compound notes exist (a later ELN step) — then a molecule retriever can cite `compound-<id>` and join gather_evidence directly |

> Source for the last six rows: `docs/research-review.md` (external 2025/2026 gap analysis).
> Two items were instead promoted to `BACKLOG.md` for a *now* decision — the evaluation/metrics
> layer and the chemical/bio safety layer — because they are cross-cutting, not deferrable modules.

## Foundation-review (F4–F7) accepted deferrals

Post-implementation adversarial review of F4–F7 confirmed the core paths are correct; the fixed
findings are recorded in D-051. These residuals were **consciously not fixed now**:

| Deferred | Why not now | Trigger |
|---|---|---|
| Per-source pipeline cursor in the ELN sync | The durable sync carries one high-water cursor keyed by `eln_sync_adapter`; with the single default ingest source (`eln-json`) this is correct. With **two** active ingest sources whose newest entries differ, the shared `max()` cursor can skip the lagging source's entries (F7 review F-1/F-2). Fixing means per-source cursors + tying the cursor key to the *active ingest set*, not `eln_sync_adapter`. **Interim guard added (2026-07-22):** `sync_eln_entries` now fails fast + non-retryably when >1 ingest source is active, so the silent-skip is impossible until per-source cursors land. | The first second ingest source lands — i.e. the custom Snowflake ELN connector, which the plan already scopes to bring its own pipeline cursor. Lift the guard and key cursors per source then |
| Thundering-herd lock in `WorkloadTokenProvider` | On a cold/stale cache, N concurrent `get_service_token(scope)` all fire the exchange (correctness fine — never a stale token, just redundant calls) | Measurable duplicate token exchanges under real concurrency — add an `asyncio.Lock` per scope |
| Generic ingest half still shaped like `ElnAdapter` | `IngestHalf = ElnAdapter` (verbatim re-host) means a future non-ELN ingest source must expose `fetch_new_entries`/`map_to_ord`. Acceptable while every ingest source is reaction-shaped (maps to the canonical ORD reaction) | A non-reaction ingest source appears — then generalize the ingest half's mapped type |

## Critical re-review (2026-07-22)

Every deferred item above was re-examined against current reality, asking "does the trigger hold
*now*?" rather than assuming the deferral still stands. Verdict: **all remain correctly deferred
except one**, which got an interim guard.

**Acted on now — Per-source ELN cursor.** The full per-source-cursor fix stays deferred (no second
real feed), but the F7/DUP-1 config seam made the *unsafe* two-ingest-source setup reachable by
config, where the one shared cursor silently skips entries. Added the fail-fast guard this item's own
"add a startup validator then" note called for (see the row above). This converts a silent data-loss
into a loud, non-retryable failure — cheap insurance, no scope creep into the full fix.

**Confirmed still-deferred (trigger genuinely unmet).** Nothing else crossed its threshold:

- *Need real infrastructure we don't have:* HPC/DFT real integration, Postgres RLS graph mirror,
  `knowledge/` as its own repo, the Snowflake ELN connector — all gated on a real cluster / tenant /
  confidentiality boundary that does not exist in this environment.
- *Need a scale we haven't reached:* sub-quadratic playbook clustering (O(n²) is fine below ~10⁴
  reactions), per-key in-flight calc-store dedup (only worth it when duplicate *expensive* HPC runs
  are a measured cost — still mock), the `WorkloadTokenProvider` thundering-herd lock (no real
  concurrent token exchanges to measure).
- *Need a capability/source that isn't in scope for v1:* universal ELN abstraction (only a 3rd real
  ELN triggers it — we have 2 adapters), external literature/patent retrievers (Phase 5b core is
  done, but this is a net-new source needing an API decision, not a latent gap), the tabular
  foundation model, retrosynthesis/reaction prediction, lab automation/SiLA2, process flowsheet
  synthesis, multimodal analytical data, domain foundation models, per-step species linking, durable
  deep-research workflow, compound notes — each waits on a concrete user need, not on us.
- *Deliberately declined / superseded:* LLM summarization of compacted history (the deterministic
  collapse D-025 suffices and an untrusted summarizer is an injection risk), MAF Durable Extension for
  jobs (Temporal owns durability), the second queue system (decided against, D-006), the generic
  ingest-half reshape (every ingest source is still reaction-shaped).

No other item warranted implementing now. The deferrals are conscious and their triggers are the
right ones; this review changed one thing (the cursor guard) and left the rest as designed.

## F10 review-cycle accepted deferrals (2026-07-22)

The post-F10 adversarial review (five agent teams over the new features + the whole codebase) fixed
the confirmed defects in-branch (recorded in D-060); these three residuals were **consciously not
built now**, each because it needs a live edge this offline environment does not have:

| Deferred | Why not now | Trigger to revisit |
|---|---|---|
| **F10-B3 — LLM faithfulness check of drafted report sections** | The conversational verifier (B2) scores a chat turn's cited prose. The *durable* report path assembles evidence per section and renders it via a template — there is no free-form synthesized prose in the workflow to LLM-judge, only citations (already gated by `verify_claims`). Wiring an LLM judge in would require the report skill's prose-drafting step to run inside the durable workflow, which it does not. | The report workflow gains an in-workflow LLM prose-synthesis step — then route that prose through `verify_answer` exactly as the chat runner does |
| **Live-retriever drift eval (retrieval P/R/F1 over the deployment graph)** | `evals.retrieval.run_retrieval_eval` scores a live retriever against labelled cases, but the shipped knowledge graph is empty (deployment-populated) and the labelled retrieval cases are deployment-local (a committed case would be dead/misleading here). So the scheduled drift job scores the deterministic committed case-set — a *deployment-consistency tripwire*, not a runtime quality monitor (documented in `workflows/eval_drift.py`). | A deployment with a populated graph + local labelled retrieval cases exists — then point the drift activity at `run_retrieval_eval` over that graph for genuine runtime drift |
| **Audit-chain tip-truncation anchor** | The hash chain now catches modification, reordering, interior deletion, and prefix (genesis) truncation. Detecting *trailing* deletion (tip truncation) needs an external append-count/max-id anchor recorded out-of-band (a second store), since the remaining rows still link cleanly. | A regulator/GxP audit requires provable completeness of the tail — then add an out-of-band monotonic append-count anchor (e.g. a signed high-water row-count) and verify it |
