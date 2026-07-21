# BACKLOG

Prioritized open action items. Top = next. Keep in sync with `docs/implementation-plan.md`
(phase/step numbers) at session end.

## Now — Foundation build (docs/foundation-plan.md + docs/implementation-tickets.md)

The target-stack foundation: MAF harness experience on OpenShift + HPC/Nextflow, internal
OpenAI-compatible LLM (generic credential), Entra everywhere with every backend workflow
user-specific, a generic data-source seam (first source ELN — a **custom Snowflake connector via
an internal data pipeline, no vendor**). Full ticket breakdown: `docs/implementation-tickets.md`.

### Phase F0 — LLM provider seam + tool-calling spike
- [x] **F0-T1** LLM provider config block (`llm_provider`/`llm_base_url`/`llm_model`/`llm_api_key`/
      `llm_tls_ca_bundle`/`llm_timeout_seconds`/`llm_max_retries`/`llm_temperature`/`llm_max_tokens`
      + `_llm_provider_config` validator). Test: `test_config.py`.
- [x] **F0-T2** Provider adapter `agents/llm_provider.py::build_chat_client` — the one place a client
      class is imported; `openai_compatible` → MAF `OpenAIChatClient` over an `AsyncOpenAI`
      (base_url + generic key + CA/timeout/retries), `anthropic` dev path retained. `build_agent`
      rewired off `_default_chat_client`. Dep added: `agent-framework-openai`. Test:
      `test_llm_provider.py`, `test_agent.py`.
- [x] **F0-T3** Streaming + generation params: `Agent(default_options=ChatOptions(temperature,
      max_tokens))` from config. Test: `test_agent.py::test_agent_applies_default_generation_options`.
- [ ] **F0-T4** Tool-calling capability spike (the H0 risk) — `scripts/spike_toolcalling.py` +
      `docs/spikes/f0-toolcalling.md` verdict. **Needs the live internal endpoint** (or a stand-in
      OpenAI-compatible server); run before building on the harness.

### Phase F1 — Harness backbone (autonomous plan/execute)
MAF ships the harness natively (`create_harness_agent` + `TodoProvider`/`AgentModeProvider`/
`todos_remaining`), so F1 is *wiring* it, not reimplementing providers.
- [x] **F1-T1** Harness config (`harness_enabled`/`harness_autonomy`/`harness_max_loop_iterations`).
      Test: `test_config.py`.
- [x] **F1-T2** `build_agent` branch → `_build_harness_agent` wires `create_harness_agent` over the
      full shared `_capability_tools()` + `RoleFilteredSkillsSource` + audit + shared
      `_compaction_strategy()`, generic batteries off. Classic path is the fallback. Test:
      `test_agent.py` (todo/mode providers added; full toolset kept; audit kept; classic has no
      harness providers).
- [x] **F1-T3** Plan→approve→execute: `AgentModeProvider(default_mode=plan|execute)` +
      `todos_remaining(looping_modes=["execute"])` → plan_only stops for approval, execute loops
      (capped). Test: `test_agent.py::test_harness_autonomy_sets_start_mode`.
- [ ] ADR **D-020** finalized + **D-A1** (F0) — write in DECISIONS.md (F9 running-log).

### Phase F2 — Front door + run service (the agent finally runs)
- [x] **F2-T1** `service/app.py::create_app` (FastAPI) + `service/runner.py::run_turn` — builds/holds
      one agent, per-session `AgentSession`, opens the MCP lifecycle once per turn (`AsyncExitStack`
      over `agent.mcp_tools`), runs `agent.run(stream=True)`, streams events. Routes: `/healthz`,
      `/readyz`, `POST /sessions`, `POST /sessions/{id}/messages` (SSE). Config: `service_host`/
      `service_port`/`service_cors_origins`. Test: `test_service.py`.
- [x] **F2-T2** Thin web chat surface `service/static/{index.html,app.js}` (vanilla + fetch-stream SSE;
      renders plan/tool-trace/tokens/approval/answer). Served at `/`. Test: `test_service.py`.
- [x] **F2-T3** Typed event contract `service/events.py` (discriminated union on `type`:
      plan/tool_call/token/job_started/approval_request/answer/error). Test: `test_service_events.py`.
- [ ] Deferred within F2: emit `PlanEvent` from harness todo state, and real `JobStartedEvent` when a
      tool starts a Temporal job (wired in F3 with job→session push-back). ADR **D-A2** (front door).

### Next — F3 durable session + job→session push-back (docs/implementation-tickets.md F3-T1..T3)
Replaces the in-memory session map with a Postgres store; a finished job wakes the session
(`awaiting→completed`) instead of polling.

## Later — Phase 6 identity/RBAC & hardening (folded into F4; needs live Entra/Temporal)

## Deep-review follow-ups (D-030)

### Done — robustness/correctness fixes (D-030)
- [x] Bounded `BAD_DATA_RETRY` (`maximum_attempts=CHEMCLAW_ACTIVITY_MAX_ATTEMPTS`) so an
      unclassified deterministic failure gives up instead of retrying forever; added
      `ValidationError`/`OrdFormatError`/`EvalCaseError` to the non-retryable names; shared the
      list with `note_publish_retry`. Test: `test_publish.py`.
- [x] Slug rejects trailing `.` and `.lock` (git-invalid `note/<id>` refs). Test: `test_note.py`.
- [x] Git subprocess timeout + kill (`CHEMCLAW_GIT_COMMAND_TIMEOUT_SECONDS`). Test:
      `test_knowledge.py::test_git_command_timeout_kills_the_child_and_raises`.
- [x] Solubility/pKa cache keys version on the reported uncertainty.
- [x] `test_mcp_transport.py` skip narrowed to a missing toolchain (won't mask a CI regression).

### Done — deferred items worked off (D-031)
- [x] Fingerprint-definition guard: each `*_fingerprints` row records its definition
      (`ecfp:r{radius}:b{bits}` / `drfp:b{bits}`); similarity search filters to the store's
      current definition so a changed radius/width + re-index can't rank incomparable bits.
      Migration `004`; runbook (vi). Guard tested in-sandbox via the in-memory store.
- [x] ELN reject re-drive: `RejectedEntry.created_at` + the WARNING log give the exact `since`
      to re-run the (idempotent) sync from after fixing a source record. Runbook (v). No
      automatic dead-letter by design (KISS).
- [x] KISS cleanups: inlined the `SolubilityModel` seam (removed Protocol + dead `model=` param);
      deleted `report.harness.gather_report` (tests assemble via `gather_section`); wired
      `note_from_confirmed_answer` into the `record_confirmed_answer` agent tool (completes plan
      5.5). Kept `StoredResult.provenance` as GxP audit metadata (docstring clarified — not read
      into logic, but a legitimate audit column + the `measured` seam).

## Admin-experience audit (configurability / error-handling / logging)

### Done — P0 observability floor (D-026)
- [x] Config-driven logging: `chemclaw/logging.py::configure_logging()` + `CHEMCLAW_LOG_LEVEL`/
      `_LOG_FORMAT`, called at both workers' entrypoints. Worker startup logs (address/namespace/
      queue/registered workflows). ELN sync logs `ingested/rejected` + a WARNING per rejection;
      both adapters log skipped broken files. Shared `chemclaw/db.py::connect` → `ConnectionError`
      "Postgres unreachable at <host>" with the DSN password redacted (not a retry-blocking
      `ChemclawError`). Tests: `test_logging.py`, `test_db.py`, ELN caplog assertions.

### Done — P1 pluggability & docs (D-028)
- [x] Cache hit-vs-compute log at the `calc/store.py` decision point (DEBUG) — the "why did this
      recompute?" trail, behind the D-026 log-level switch.
- [x] ELN adapter registry (`eln/registry.py`): `CHEMCLAW_ELN_SYNC_ADAPTER` selects the durable
      sync's source; memory jobs read `all_eln_adapters()`. Replaced the hardcoded adapter classes
      in `eln_sync.py` and `memory_jobs.py`.
- [x] `skills_dir` → OS-path-separator list via the `skills_dirs` property (add a second skills
      directory with no code change) + SKILL.md front-matter schema/template in `skills/README.md`.
- [x] MCP-attach the agent's fingerprint search (D-029): `build_agent` attaches config-driven
      `MCPStdioTool` servers (`CHEMCLAW_MCP_SERVERS`), so structural search runs over MCP and
      adding a capability is a config entry. `allowed_tools` keeps write/index tools off the
      agent. Transport verified in-sandbox (`test_mcp_transport.py`). `docs/runbook.md` (iv)
      rewritten for the MCP procedure.
- [x] `make skill-validate` (D-037): `scripts/validate_skills.py` checks every SKILL.md's
      frontmatter (name/description present, name matches directory) and gates in CI, like
      kg-validate. Migrating the in-process agent tools (calculators/graph/BO) to MCP stays
      unplanned — local RDKit/BoFire functions are simpler in-process (KISS).

### Open — P2 polish
- [x] `docs/runbook.md`: the four admin tasks (add skill / add-repoint DB / add-or-switch ELN
      source / add capability), the log switch, the Temporal UI at :8080, DB-unreachable message.
- [x] Startup preflight for `ANTHROPIC_API_KEY` presence (D-037): `_default_chat_client` fails
      with a clear message at agent build, not on the first turn.
- [x] Migration-status visibility (D-034): `schema_migrations` ledger records each applied file
      by name + checksum; an edited applied file is flagged as drift.
- [ ] Coverage threshold in CI (D-037): `pytest-cov` + `make cov` + `[tool.coverage]` config are
      in place (no hard `--cov-fail-under` yet). Set a `--cov-fail-under` once a CI run
      establishes the real baseline, then ratchet.

### MAF out-of-the-box features (analysis done)
- [x] **Function middleware** (`@function_middleware`) — one DRY GxP tool-audit trail
      (`agents/audit.py::audit_tool_calls`: name/args/outcome/latency, observe-only) over all
      agent tools, on the logging floor. Attached via `Agent(..., middleware=[...])` (D-027).
- [x] **OpenTelemetry** — opt-in `chemclaw.logging.configure_telemetry()` gated on
      `CHEMCLAW_OTEL_ENABLED`; calls MAF's `configure_otel_providers` at each worker's entrypoint.
      Ships as a config toggle (default off) because the OTel SDK/OTLP exporter extras are not
      installed and are only useful with a collector — enabling it requires adding those extras
      (D-027).
- [ ] **Structured outputs** (`response_format` + `resp.value`) — force validated pydantic
      payloads for agent proposals instead of parsing prose. Deferred to the first call site that
      needs a validated payload (changes call sites, not startup wiring).
- Do-not-adopt / defer: Redis/mem0 history (durability belongs to Temporal, and neither extra is
      installed), the MAF `_harness` providers (duplicate the memory layer + background queue),
      the wholesale MAF eval harness (have `evals/`; cherry-pick only its tool-call checks). FIDES
      security layer is `@experimental` → a DEFERRED candidate for untrusted ELN/literature text.


## Done — Whole-repo production-readiness review (post-5b; commit d51f0b5, D-021)
- [x] 4 adversarial review agents over all packages; ~45 verified findings fixed with regression
      tests (134 → 169 passing). Criticals: PR-gate submitter concurrency/checkout corruption
      (lock + `note_repo_dir` config + slug-validated note ids + path containment + fetch before
      `--force-with-lease`); ELN sync poison pill (one `ChemclawError` bad-data base, sync
      catches it → reject-and-continue actually holds). Majors: temperature range mis-parse
      (`60-80 °C` → -80), stoichiometry-unsound mass balance → element subsumption, per-file
      fetch robustness, BoFire off-thread, pKa cache key engine-versioned, QM tool no longer
      recomputes completed jobs, report publish got the bounded retry discipline
      (`workflows/publish.py`), vacuous-green eval gate fails loudly. Cross-cutting: CLAUDE.md
      status un-falsified, `.env.example` complete, CI runs eval+eln-validate, dependency hygiene.
- [x] Test-helper dedup pass: one `FakeSubmitter` in conftest (replaced ~10 local fakes),
      QM tests use `tests/temporal_env.py` (inline copies + cross-test private imports gone),
      shared `tests/pg.py` Postgres bootstrap, redundant `fast_mock` fixtures deleted.
- [ ] Multi-process note-submit serialization (lock is per-process; per-submission worktrees or
      a distributed lock) — revisit when >1 background worker replica exists.

## Done — Phase 5b: report / deep-research harness (no new store — D-020)
- [x] 5b.1/5b.2 Source-agnostic harness core (`report/harness.py`) over the `SourceRetriever`
      contract + mandatory-citation `EvidenceChunk` (`report/evidence.py`).
- [x] 5b.3 Two concrete retrievers (`report/retrievers.py`): `GraphRetriever` (Phase 2) +
      `FingerprintReactionRetriever` (Phase 3) — thin adapters, no new store.
- [x] 5b.4 Adversarial verify (`verify_claims`): a claim survives only if it cites retrieved
      evidence; uncited/fabricated claims discarded. Unsupported sections marked, not invented.
- [x] 5b.5/5b.6 Durable `DevelopmentReportWorkflow` (per-section activities = resumable long runs),
      each section declares its memory layer (structural provenance separation). Registered on bg worker.
- [x] 5b.7 Draft is a PR-gated `report` note citing every source. `development-report` skill (judgment:
      decompose, write only what evidence supports, keep evidenced vs analogy apart).
- [x] CHECKMATE 5b (G1–G7 + citation fidelity): core correct (verify_claims guards the `all([])`
      trap; every chunk cited), no new store. 4 fixes — (F1/F2) report id is now ref-safe + unique
      (slug + title hash) instead of a raw slug that broke git branches and collided across titles;
      (F3) fingerprint-retriever citation honesty documented (PR-gate catches a pending-note link);
      (F4) `load_notes` resilient to a malformed note (no longer aborts retrieval); + docstring
      honesty on substring matching and the verify gate. **Phase 5b complete.**

## Done — Phase 5: memory layers (episodic + semantic, no new infra — D-019)
- [x] 5.1/5.2/5.3 episodic: `memory/chains.py` (chain detection — product A = reactant B via the
      canonical-SMILES compound identity, Phase 3) + `memory/campaign.py` (`campaign` note citing each
      member reaction via wikilinks) + `memory/jobs.py::synthesize_campaigns` + Temporal workflow.
      `campaign-narrative-synthesis` skill (judgment; every claim cites a member reaction).
- [x] 5.4 semantic: `memory/playbook.py` (`find_playbook_candidates` — DRFP similarity across ≥2
      projects; `playbook_note` with mandatory evidence refs) + `distill_playbooks` job + workflow.
      `playbook-distillation` skill (transferable-only, process-chemist approval).
- [x] 5.5 user interaction as a 4th source: `memory/interaction.py` (`interaction` note via the same
      PR-gate); reachable via the `record_confirmed_answer` agent tool (synchronous) and the durable
      `InteractionApprovalWorkflow` (async Yes/No hold — D-032). 5.6 retrieval separation: judgment in
      the playbook skill (evidenced vs analogy kept visibly separate; experiment outranks analogy).
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
