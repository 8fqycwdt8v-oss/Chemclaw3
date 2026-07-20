# BACKLOG

Prioritized open action items. Top = next. Keep in sync with `docs/implementation-plan.md`
(phase/step numbers) at session end.

## Now — next capability phase (Phase 5b report harness, or Phase 6 identity/RBAC)

## Done — Phase 5: memory layers (episodic + semantic, no new infra — D-019)
- [x] 5.1/5.2/5.3 episodic: `memory/chains.py` (chain detection — product A = reactant B via the
      canonical-SMILES compound identity, Phase 3) + `memory/campaign.py` (`campaign` note citing each
      member reaction via wikilinks) + `memory/jobs.py::synthesize_campaigns` + Temporal workflow.
      `campaign-narrative-synthesis` skill (judgment; every claim cites a member reaction).
- [x] 5.4 semantic: `memory/playbook.py` (`find_playbook_candidates` — DRFP similarity across ≥2
      projects; `playbook_note` with mandatory evidence refs) + `distill_playbooks` job + workflow.
      `playbook-distillation` skill (transferable-only, process-chemist approval).
- [x] 5.5 user interaction as a 4th source: `memory/interaction.py` (`interaction` note via the same
      PR-gate). 5.6 retrieval separation: judgment in the playbook skill (evidenced vs analogy kept
      visibly separate; experiment outranks transferred analogy).
- [x] Jobs registered on the background worker; `project` field added to `OrdReaction`/adapter.
- [x] CHECKMATE 5 (G1–G7 + no-new-infra check confirmed): 3 findings fixed — (F1, G4) a degenerate
      reaction is skipped in `find_playbook_candidates` instead of aborting the whole distillation;
      (F2) a cyclic chain is flagged `ordered=False` and the campaign note says so, not a fake causal
      sequence; (F3) the merged-reaction-notes precondition for citations is documented (kg-validate
      enforces it). Also stabilized a pre-existing flaky BO test by seeding BoFire (`bo_seed` config).
      **Phase 5 complete.**

## Done — Phase 4: ELN ingestion (adapter pattern) — COMPLETE
- [x] 4.1 Stable ORD-subset schema (`eln/ord.py`: `OrdReaction`/`Component`/`Role`) — ELN-agnostic;
      `reaction_smiles()` for DRFP, role consistency validated.
- [x] 4.2 Adapter contract (`eln/adapter.py`: `RawEntry` + `ElnAdapter` Protocol —
      `fetch_new_entries`/`map_to_ord`). Only the contract is fixed (G6).
- [x] 4.3 One concrete adapter (`eln/json_adapter.py`, JSON-export ELN): structured mapping +
      deterministic free-text regex (temperature/time). No universal abstraction (D-018).
- [x] 4.4 `eln-reaction-extraction` skill (judgment: structured-first, per-field LLM fallback,
      validation gate) + `eln/validate.py` (RDKit parse + atom/mass balance) + `make eln-validate`
      / `scripts/validate_ord.py`. LLM-per-field wiring deferred (D-018).
- [x] 4.5 Durable ELN sync (`eln/sync.py` core + `workflows/eln_sync.py` activity/workflow on the
      background queue): fetch → map → validate → **index reaction+compound fingerprints** (Phase 3)
      + **PR-gated `reaction` note** (Phase 2). Reject-and-continue; idempotent. Registered on the
      bg worker. Seed corpus in `eln/exports/`. Server test in CI; full chain tested in-memory.
- [x] CHECKMATE 4 (G1–G7 + deep review over Phase 3+4): end-to-end chain sound; 3 real bugs fixed —
      (F1) mapping failures (unknown role / schema violation) now raise a contract-level
      `ElnMappingError` so the batch sync rejects-and-continues instead of aborting (also removes a
      G6 leak); (F2) structured `temperature_c`/`time_h` of `0` no longer discarded as falsy by the
      `or` fallthrough (ice-bath 0 °C preserved); (F3) temperature regex now requires the degree sign
      so `13C NMR`/`pH 7 C` can't fabricate a temperature; + dead-param cleanup. **Phase 4 complete.**

## Done — Phase 3: fingerprint search (molecules + reactions) — COMPLETE
- [x] 3.1 `mcp-molfp` capability: ECFP4 (Morgan r2, 2048-bit) via RDKit (`mcp_servers/molfp/
      fingerprint.py`), config-sized, deterministic. Thin FastMCP `server.py` advertises the tools.
      (Dir is `mcp_servers/`, not `mcp/` — the `mcp` name is the SDK's, D-016.)
- [x] 3.2 Postgres `bit(2048)` table + HNSW `bit_jaccard_ops` index (`infra/sql/002_...sql`) +
      `PostgresFingerprintStore` (Tanimoto in SQL). In-memory backend proves the ranking everywhere.
- [x] 3.3 `find_similar_molecules(smiles, top_k)` (Tanimoto, threshold+top_k from config) +
      `find_substructure_matches` (exact RDKit match), backend-agnostic (`mcp_servers/molfp/search.py`).
- [x] 3.5 `reaction-search` skill: the judgment (similarity vs substructure, what Tanimoto counts as
      precedent, combine with metadata/graph) — thresholds in config, not code (G6).
- [x] 3.4 `mcp-rxnfp` (DRFP reaction fingerprints, `mcp_servers/rxnfp/`) + `find_similar_reactions`
      + thin FastMCP server + `infra/sql/003`. Reactions are the 2nd fingerprint domain, so the
      Tanimoto store is now the **generic** `mcp_servers/fpstore.py` shared by molfp+rxnfp (D-017,
      DRY); molfp refactored onto it (molecule tests still green = no regression). `reaction-search`
      skill covers both molecule and reaction search.
- [x] CHECKMATE 3 (G1–G7 + deep review): core correct, MCP/skill split clean, threshold configurable.
      4 fixes — (F1) docstrings no longer overclaim exact HNSW ordering (approximate NN, up to recall);
      (F2) `bit(N)` width derived from `ecfp_bits` (single source; mismatch fails loud, not silent pad);
      (F3) substructure docstring clarified (SMARTS-first); (F4) all-zero-fp guard noted. **Molecule
      path complete.**



## Done — Phase 2b: evaluation & metric layer (cross-cutting)
- [x] 2b.1 Metric interface: pure `Metric = (EvalCase) -> MetricResult` + registry
      (`evals/metric.py`, `@metric` decorator = the 2b.5 extension seam). Thresholds from config (G3).
- [x] 2b.2 Eval harness (`evals/harness.py`): `run_eval` over a versioned case-set +
      `render_report` (citable Markdown, case id + provenance per row) + `load_eval_cases`
      (frontmatter files) + `make eval` CLI. Cases versioned in `evals/cases/` (D-014).
- [x] 2b.3 Seed metrics (`evals/metrics.py`): green-chemistry **E-factor** + **PMI** (mass balance),
      **prediction_error** (vs held-out reference), **bo_regret** (1d.6). All pure, config-gated.
- [x] 2b.4 Per-task tool-utility A/B (`evals/ab.py`): direction-aware delta, buckets help/hurt/
      no-effect over a task set — proves ≥1 case where tooling does NOT help (F8/F9 steering).
- [x] 2b.5 Wiring: each later capability phase registers ≥1 metric via `@metric`; regressions are
      pinned by the test suite (expected pass/fail per case), not a CI hard-gate (the seed set
      deliberately holds a failing case to prove gating).
- [x] CHECKMATE 2b (G1–G7 + deep review): 5 robustness findings fixed — (F1) `EvalCase`
      `extra="forbid"` so a misspelled frontmatter key can't silently drop and mis-score;
      (F2) unknown metric name wrapped as case-named `EvalCaseError`, not a raw traceback;
      (F3) mass coercion routes through the guarded `_scalar` (no escaping `TypeError`);
      (F4) mass-balance violation (product > input) rejected, not a negative-E gate pass;
      (F5) `bo_regret` provenance/docstring corrected (signed, not `|abs|`). **Phase 2b complete.**

## Prior — Phase 2: knowledge graph + PR-gate
- [x] 2.1 Note schema (`kg/note.py`, one pydantic model); 2.2 parser (frontmatter → Note, clear errors).
- [x] 2.3 Wikilink extraction + NetworkX indexer (`kg/graph.py`, `neighborhood` 1–2 hop traversal).
- [x] 2.4 Validation CLI (`kg/validate.py`, `make kg-validate`) — broken links / dup ids / bad notes; in CI.
- [x] 2.5/2.6 skills `knowledge-graph-query` + `knowledge-graph-write` (judgment).
- [x] 2.7 **PR-gate** built once (`kg/pr_gate.py` `propose_note` + `NoteSubmitter` seam + `kg/render.py`);
      agent-only, notes land at `<knowledge_dir>/<type>/<id>.md` on a per-note branch. Tested with a fake.
- [x] 2.6b real `NoteSubmitter`: `kg/git_submitter.py` `GitNoteSubmitter` (branch off base, write, commit,
      push) — tested against a local bare remote. PR-object creation is the git platform's step.
- [x] 2.8 Temporal activity `write_knowledge_node` (`workflows/knowledge.py`): QM result → agent
      `job-result` note (links to a method-independent compound id) → PR-gate. Registered on the bg worker.
- [x] Agent tools for graph query/write (`agents/graph_tools.py`: find_notes, expand_note,
      propose_knowledge_note) registered on the MAF agent; shared `default_submitter` (DRY).
- [x] Wire `write_knowledge_node` into a workflow caller: `QMJobWorkflow` gains opt-in
      `publish_to_graph`, routing the note write to the background-jobs queue (best-effort). Server test.
- [x] CHECKMATE 2 (G1–G7 + deep review over Phase 1+2): 5 findings fixed — (F1) bounded retry so
      best-effort publish gives up instead of hanging; (F2) job-result note no longer dangling-links a
      non-existent compound note (would fail kg-validate); (F3) git submitter idempotent on identical
      re-submit; (F4) stray `body:` frontmatter key no longer crashes the parser; (F5) dedicated
      note-write timeout/attempts config. **Phase 2 complete.**

## Later compute items (reprioritized; HPC/DFT deferred — D-010)

### Phase 1b — Result store / calc cache (first-class; "never compute twice") — DONE
- [x] 1b.1 Store interface `get/put` (Protocol); 1b.2 versioned key `(calc_type, calc_version, input_hash, params_hash)`.
- [x] 1b.3 In-memory backend (tests) + Postgres backend (`calculation_results` table) + `make db-migrate` + CI DB.
- [x] 1b.4 One `cached_compute()` path (lookup-before-compute, DRY); returns was_cached for hit/miss metric.
- [ ] 1b.5 Temporal lookup/persist activities — fold into 1c.5 (generic CalculationWorkflow) to avoid a stub.

### Phase 1c — Fast predictors + semiempirical (first *real* calculations)
- [x] 1c.2 **xTB / GFN2** calculator via `tblite` (real single-point energy, RDKit 3D embed, CPU) —
      `calc/xtb.py`, cached through the store (`run_cached_xtb`). Real GFN2 tests run everywhere.
- [x] 1c.1 Calculator **contract**: `calc.store.run_cached` (offload blocking compute → store dict →
      reconstruct typed model) — each `run_cached_*` now only derives its key and delegates (DRY,
      Rule of Three across xTB/solubility/pKa). Name→calculator **registry deferred** (no dispatch
      consumer yet; would be a one-caller abstraction — D-015).
- [ ] 1c.3 GNN solubility model (inference only; value + uncertainty) — **needs model choice** (see open Qs).
      **Blocked on user input** (which GNN + weights/license); the calculator contract makes the swap cheap.
- [x] 1c.4 **pKa via xTB** (`calc/pka.py`): GFN2-xTB ALPB-solvated deprotonation energy of the most
      acidic O-H/S-H site + linear calibration (R²0.93 over 10 acids). Agent tool `predict_pka`. Real tests.
- [x] 1c.5/1c.6 xTB exposed to the MAF agent as tool `compute_xtb_energy` + `calculation-selection` skill.
- [x] 1c.5b calculator contract landed (see 1c.1); name-registry consciously deferred (D-015).
- [ ] 1c.7 optional graph note via PR-gate for a *fast* calc result — deferred: the QM path already
      publishes (2.8) and BO recommendations now publish (1d.5); a fast-calc publish waits for a real
      need (avoids a third near-identical mapper before it is asked for). CHECKMATE 1c: G1–G7 met.
- Note: fast calcs run **without** a Temporal workflow (sub-second) — the store gives "never twice";
  durability (Temporal) is reserved for long jobs (BO campaigns 1d, later HPC).

### Phase 1d — Bayesian optimization (BoFire, pulled forward)
- [x] 1d.1 Domain adapter (`bo/engine.py`, BoFire fully encapsulated behind neutral `bo/problem.py` types).
- [x] 1d.2 ask/tell: `initial_candidates` (random seed) + `propose_candidates` (SOBO); `optimize()` loop
      (`bo/campaign.py`) — convergence-tested on known minima/maxima (CHECKMATE 1d spike met).
- [x] 1d.2b categorical BO support (`CategoricalParameter`) + real reaction benchmark:
      **Reizman Suzuki–Miyaura** (`bo/benchmarks/reizman_suzuki.py`, data vendored from Summit/MIT),
      RandomForest yield surrogate → BoFire mixed categorical+continuous campaign beats dataset median.
- [x] 1d.4 **durable BO campaign**: `BoCampaignWorkflow` (Temporal) + activities (heavy BoFire work
      isolated) + `bo/objectives.py` name→objective registry + **`workers/background_worker.py`**
      (first real background-jobs job — retro-satisfies 1.8, no empty stub). Server test runs in CI.
- [x] 1d.3 **calculator-backed objective**: `solubility_objective(store)` (cached solubility via the
      store) registered as `solubility_max`, plus `molecule_library_problem`. **Candidate-set BO works**:
      BoFire drives a pure-categorical domain by exhaustive-discrete acquisition — finds a top molecule
      without evaluating the whole library (test: best found evaluating 9/14). Constraint: evaluation
      budget must be < library size, else the unique-candidate pool exhausts.
- [x] Robustness: `optimize` and the durable BO workflow stop gracefully when a discrete candidate
      set is exhausted (`discrete_candidate_count`/`distinct_candidate_count` guard) instead of crashing
      inside BoFire. Tests: budget 2+10 over a 4-molecule library returns cleanly.
- [x] 1d.5 recommendation PR-gated: `workflows/bo_knowledge.py` (`note_from_campaign_result` +
      `write_campaign_node`) maps a campaign's best point to an agent `bo-candidate` note through the
      **same** PR-gate the QM path uses (DRY: reuses `propose_note`/`default_submitter`). Opt-in
      `CampaignSpec.publish_to_graph` routes it to the background queue, best-effort with bounded
      retry (mirrors QM 2.8). Registered on the bg worker. Pure mapper + PR-gate tests; server test in CI.
- [x] 1d.6 progress/regret metric: `bo_regret` registered in the Phase 2b metric layer
      (`evals/metrics.py`, direction-aware, non-negative) — Phase 1d's registered scientific metric.
- [x] CHECKMATE 1d: G1–G7 met (recommendation publish mirrors the deep-reviewed QM path; best-effort
      + bounded retry; no dangling wikilink; idempotent note id). **Phase 1d complete.**

## Done
- [x] **Phase 0** — foundation (tooling, config, infra compose, CI, ADR-0001, layer READMEs). CHECKMATE 0 green.
- [x] **Phase 1 spine (1.1–1.6, 1.9)** — hpc worker; `QMJobWorkflow` + activities (mock HPC, heartbeat poll,
      parse); agent tools `submit_qm_job`/`get_qm_job_status`; MAF agent + `qm-job-submission` skill;
      `requested_by` audit field; shared Temporal client + result models. Server-backed tests run in CI.
- [x] **Orchestrator** — reconsidered MAF vs LangGraph → keep MAF (D-013).
- Folded/deferred Phase-1 tails: **1.7** notify callback (defer until an async result must reach a live
  session); **1.8** background-jobs worker — **DONE** (`workers/background_worker.py`, hosts the BO
  campaign); **1.10** → generalized into **Phase 1b**. **CHECKMATE 1** (worker-restart durability spike)
  runs against a live Temporal (`make up`) — pending, needs a live cluster (not runnable in sandbox).

## Capability gaps to triage (from `docs/research-review.md`) — decide per item
- [x] **Evaluation / scientific-output metrics layer** → promoted to first-class **Phase 2b**
      (see plan + D-009). No longer a backlog decision.
- [ ] **Chemical/biological safety layer** — distinct from Entra-ID/RBAC (IT security).
      GxP / data-integrity + hazard checks. **Kept in backlog** (user decision); decide scope
      before any capability phase that could propose a hazardous route/procedure.
- [ ] Retrosynthesis + reaction prediction · DoE/Bayesian optimization · lab automation/SiLA2
      closed-loop · process flowsheet synthesis · multimodal analytical data · domain foundation
      models — all currently in `DEFERRED.md` with triggers; confirm or pull forward.
- [ ] Design cautions to bake in: apply Skills/tools **selectively + measured per task** (not
      universally); design the CoALA memory layer against DMR/LongMemEval, not by assumption.

## Open questions / awaiting input (see `docs/research-review.md`)
- [ ] **"pKs models"** — interpreted as **pKa** prediction; confirm (could mean PK/ADMET). The
      pluggable calculator registry (1c.1) makes a rename/swap cheap.
- [ ] **Which models** for solubility (GNN weights + license?) and pKa (tool/model)? xTB binary
      availability + license in the target runtime.
- [ ] BoFire scope for v1: which problem (reaction-condition? formulation?) is the first real BO case?
- [ ] Temporal vs. Restate/DBOS/Prefect/Dapr — no head-to-head source found; our choice stands
      on maturity/fit. Revisit if operability/cost becomes a concern.
- [ ] When does Markdown+NetworkX tip to Neo4j/Memgraph + GraphRAG? (deterministic traversal
      sidesteps the NL-query risk for now.)
- [ ] Concrete lab-automation/SiLA2 + DoE + retrosynthesis integration wiring.
- [ ] Domain safety/compliance layer design beyond RBAC.

## Later
- [ ] Phase 2 knowledge-graph core + PR-gate · Phase 3 fingerprint search · Phase 4 ELN
      ingestion · Phase 5 memory layers · Phase 5b report harness · Phase 6 identity/RBAC.
