# BACKLOG

Prioritized open action items. Top = next. Keep in sync with `docs/implementation-plan.md`
(phase/step numbers) at session end.

## Now — early compute focus (reprioritized; HPC/DFT deferred — D-010)

### Phase 1b — Result store / calc cache (first-class; "never compute twice")
- [ ] 1b.1 Store interface `get/put` (Protocol); 1b.2 versioned key `(calc_type, calc_version, input_hash, params_hash)`.
- [ ] 1b.3 In-memory backend (tests) + Postgres backend (`calculation_results` table).
- [ ] 1b.4 One `cached(calculator)` wrapper (lookup-before-compute, DRY); hit/miss counter.
- [ ] 1b.5 Temporal: lookup activity before compute, persist activity after. CHECKMATE 1b.

### Phase 1c — Fast predictors + semiempirical (first *real* calculations)
- [ ] 1c.1 Calculator contract + registry (no hardcoded branches).
- [ ] 1c.2 xTB / **GFN2** MCP calculator (SMILES → energy/geometry; CPU, no HPC).
- [ ] 1c.3 GNN solubility model (inference only; value + uncertainty).
- [ ] 1c.4 pKa/property model(s) (the user's "pKs" — interpreted as pKa; confirm).
- [ ] 1c.5 generic `CalculationWorkflow` + `submit_calculation`/`get_calculation_status`.
- [ ] 1c.6 skill `calculation-selection`; 1c.7 optional graph note via PR-gate. CHECKMATE 1c.

### Phase 1d — Bayesian optimization (BoFire, pulled forward)
- [ ] 1d.1 Domain adapter (config → BoFire `Domain`, encapsulated).
- [ ] 1d.2 ask/tell `propose_candidates`; 1d.3 objective eval via 1c calculators + store.
- [ ] 1d.4 BO campaign as durable Temporal workflow; 1d.5 candidates PR-gated; 1d.6 progress metric. CHECKMATE 1d.

## Done
- [x] **Phase 0** — foundation (tooling, config, infra compose, CI, ADR-0001, layer READMEs). CHECKMATE 0 green.
- [x] **Phase 1 spine (1.1–1.6, 1.9)** — hpc worker; `QMJobWorkflow` + activities (mock HPC, heartbeat poll,
      parse); agent tools `submit_qm_job`/`get_qm_job_status`; MAF agent + `qm-job-submission` skill;
      `requested_by` audit field; shared Temporal client + result models. Server-backed tests run in CI.
- [x] **Orchestrator** — reconsidered MAF vs LangGraph → keep MAF (D-013).
- Folded/deferred Phase-1 tails: **1.7** notify callback (defer until an async result must reach a live
  session), **1.8** background-jobs worker (defer until a real bg job exists — no empty stub), **1.10** →
  generalized into **Phase 1b**. **CHECKMATE 1** (worker-restart durability spike) runs against a live
  Temporal (`make up`) — pending, do at end of the 1b–1d cluster.

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
