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
| ~~Per-source pipeline cursor in the ELN sync~~ | **Done (D-054, 2026-07-22):** the sync now keys one high-water cursor per active ingest source (registry name), iterating every source; the interim fail-fast guard is removed and multi-ingest is safe. Both existing adapters are datetime-cursored (`ElnAdapter` contract), so this is a faithful generalization, not a guess. `eln_sync_adapter` config field deleted (was the dead single-cursor label — audit DUP-2). | — (a future non-datetime cursor source generalizes the `ElnAdapter` contract itself) |
| ~~Thundering-herd lock in `WorkloadTokenProvider`~~ | **Done (D-054, 2026-07-22):** a per-scope `asyncio.Lock` with a double-checked cache re-read collapses N concurrent cold/stale callers onto one exchange; different scopes never block. | — |
| Generic ingest half still shaped like `ElnAdapter` | `IngestHalf = ElnAdapter` (verbatim re-host) means a future non-ELN ingest source must expose `fetch_new_entries`/`map_to_ord`. Acceptable while every ingest source is reaction-shaped (maps to the canonical ORD reaction) | A non-reaction ingest source appears — then generalize the ingest half's mapped type |

## Critical re-review (2026-07-22)

Every deferred item above was re-examined against current reality, asking "does the trigger hold
*now*?" rather than assuming the deferral still stands. Verdict: **all remain correctly deferred
except one**, which got an interim guard.

**Implemented (D-054, superseding the interim guard).** A follow-up "close all found gaps" pass then
did the *full* fixes for the two items that turned out to be closable offline against the existing
contracts:

- **Per-source ELN cursor.** First shipped as an interim fail-fast guard, then replaced by real
  per-source cursors: the sync iterates every active ingest source and keys one cursor per source
  (the `sync_cursors` table already keyed by name). Both current adapters are datetime-cursored
  (that *is* the `ElnAdapter` contract), so this is the faithful generalization of today's contract,
  not a guess about the not-yet-existing Snowflake source. The `eln_sync_adapter` dead field is gone
  (audit DUP-2). Multi-ingest is now first-class.
- **`WorkloadTokenProvider` thundering-herd lock.** A per-scope `asyncio.Lock` + double-checked cache
  now collapses concurrent cold-cache callers onto one exchange — cheap, correct, offline-testable.

**Confirmed still-deferred (trigger genuinely unmet).** Nothing else crossed its threshold:

- *Need real infrastructure we don't have:* HPC/DFT real integration, Postgres RLS graph mirror,
  `knowledge/` as its own repo, the Snowflake ELN connector — all gated on a real cluster / tenant /
  confidentiality boundary that does not exist in this environment.
- *Need a scale we haven't reached:* sub-quadratic playbook clustering (O(n²) is fine below ~10⁴
  reactions), per-key in-flight calc-store dedup (only worth it when duplicate *expensive* HPC runs
  are a measured cost — still mock). (The `WorkloadTokenProvider` thundering-herd lock was in this
  bucket but is now implemented — the lock is cheap enough to add before the scale arrives — D-054.)
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
the confirmed defects in-branch (recorded in D-065); these three residuals were **consciously not
built now**, each because it needs a live edge this offline environment does not have:

| Deferred | Why not now | Trigger to revisit |
|---|---|---|
| **F10-B3 — LLM faithfulness check of drafted report sections** | The conversational verifier (B2) scores a chat turn's cited prose. The *durable* report path assembles evidence per section and renders it via a template — there is no free-form synthesized prose in the workflow to LLM-judge, only citations (already gated by `verify_claims`). Wiring an LLM judge in would require the report skill's prose-drafting step to run inside the durable workflow, which it does not. | The report workflow gains an in-workflow LLM prose-synthesis step — then route that prose through `verify_answer` exactly as the chat runner does |
| **Live-retriever drift over the deployment's *own* graph** | The KM-13 retrieval gold-set (D-056) already scores `GraphRetriever` over a committed fixture corpus (`evals/retrieval_corpus/`), and the scheduled F10-F2 drift job re-runs that deterministic case-set — a *deployment-consistency tripwire* (documented in `workflows/eval_drift.py`). What stays deferred is drift over the *deployment's live, changing* knowledge graph (genuine runtime quality drift), whose labelled cases are deployment-local (the shipped graph is empty), not a committed fixture. | A deployment with a populated graph + local labelled retrieval cases exists — then score the live retriever over that graph on the drift cadence, alongside the fixture tripwire |
| **Audit-chain tip-truncation anchor** | The hash chain now catches modification, reordering, interior deletion, and prefix (genesis) truncation. Detecting *trailing* deletion (tip truncation) needs an external append-count/max-id anchor recorded out-of-band (a second store), since the remaining rows still link cleanly. | A regulator/GxP audit requires provable completeness of the tail — then add an out-of-band monotonic append-count anchor (e.g. a signed high-water row-count) and verify it |

## Engine gap-doc follow-ups (2026-07-22)

The two engine gap analyses (`docs/audit/08-agentic-engine-gaps.md`,
`09-knowledge-management-gaps.md`) surfaced a cluster of non-`None`, non-deferred gaps. The
closable ones were implemented across three passes: KM-6/KM-7 provenance+freshness (D-055), the
KM-13 retrieval gold-set (D-056), and then **KM-5, AG-14, AG-15, and the retrieval half of KM-14
(D-057)**. Each of those four made a defensible default decision (documented in D-057) rather than
staying blocked. **One remains deferred**, and it is genuinely infra-gated:

| Item | Why not now | Trigger to revisit |
|---|---|---|
| **AG-13** — agent-behavior / prompt / skill regression eval | A faithful agent-behavior eval must run the agent against a real LLM to observe tool-selection and citation; the target internal OpenAI-compatible endpoint is not reachable offline, and a mock LLM would only test the mock, not behavior. **Genuinely infra-gated** (unlike KM-13, which scores the deterministic retrieval path and *was* done in D-056). | The live internal LLM endpoint is reachable from CI or a test harness — then add a behavior suite (tool-selection + citation assertions over representative prompts), reusing the `evals/` case-set + `@metric` seam |

Two narrower sub-gaps also remain, each with its own existing deferral: the **O(n²) playbook
clustering** half of KM-14 (see the row in the main table above — sub-quadratic clustering at ~10⁴
reactions) and the **durable** half of per-user turn/token budgeting (see below — the in-process
guard is now in via D-066; a rolling-window quota surviving restart/multi-pod waits on a real
multi-tenant need). Neither is a latent bug.

## Resilience-hardening deferrals (2026-07-23, D-066)

Three residual failure-mode gaps were closed on the feature branch (DB-query clamps, front-door
session reattach, in-process turn/token budgets). Two narrower pieces were consciously left out:

| Item | Why not now | Trigger to revisit |
|---|---|---|
| **Durable / rolling-window budget quota** | `service.budget.BudgetTracker` bounds a *running process's* runaway (the "$400 in twenty minutes" failure), which is what the per-turn loop cap left open. A quota that survives a restart or is shared across pods needs a durable store (Postgres) and a time-window policy (per-day/per-month reset) — a bigger piece whose value is real only under multi-tenant billing/fairness pressure, not the single-process runaway this guards. | A real multi-tenant deployment needs per-user spend fairness *across* restarts/pods — then back the counters with a Postgres table + a windowed reset, reusing the same `check`/`record` seam |
| **Substructure pattern-fingerprint prefilter** | `find_substructure_matches` now bounds its scan to `substructure_scan_max_records` (5000) and warns on truncation, so the full-table-load footgun is closed. Screening candidates with a pattern fingerprint before the RDKit match (to raise the effective ceiling without loading every row) is a genuine optimization, but ECFP bits cannot screen substructures soundly — it needs a dedicated pattern-fingerprint column + index. | The molecule corpus grows past the scan cap in real use (the truncation warning fires) — then add a pattern-fingerprint prefilter column so substructure search scales past ~10⁴ molecules |

## Review-campaign deferrals (2026-07-24, D-072)

- **`within=` id-array scaling** — retrieval eligibility ships the full eligible-id list as a SQL
  array parameter; fine at the current corpus scale (10^3–10^4 notes). Revisit with indexed
  type/tag/currency columns when the corpus approaches ~10^5 notes.
- **`XtbInput.charge` redundancy** — with charge now validated against the SMILES formal charge,
  the field is fully determined by the SMILES; kept so the LLM tool signature stays loud on
  mismatch rather than silently ignoring the argument. Revisit if the tool schema is ever versioned.
- **Even-electron open-shell species** (e.g. triplet O2) — undetectable from SMILES, which carries
  no spin multiplicity; documented input-format limit of `require_closed_shell`. Revisit only if a
  spin-aware input format is adopted.
- **JS test infra** — `service/static/app.js` error surfacing is covered by `node --check` only;
  no JS test runner exists in the repo. Revisit if the web client grows beyond a demo shell.
