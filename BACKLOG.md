# BACKLOG

Prioritized open action items. Top = next. Keep in sync with `docs/implementation-plan.md`
(phase/step numbers) at session end.

## Next — Platform-parity hardening (docs/parity-plan.md, Phase F10)

Closes the platform-capability deltas found against a commercial pharma-agent platform. Full
tickets + disposition table: `docs/parity-plan.md`.

- [x] **F10-E** per-task model routing: `build_chat_client(task)` consults `model_routes`
      (task→model) in the one provider seam; empty map = today's single model. Test:
      `test_llm_provider.py`, `test_config.py`.
- [x] **F10-C** per-tool authorization: `agents/authz.py::authorize_tool` (`tool_role_gates` +
      `tool_authz_default`) enforced by one middleware `agents/tool_authz.py::enforce_tool_authz`,
      wired into `build_agent` after audit; default-allow, active only under `entra_required`. The
      coarse expensive-trigger gate now shares `_has_required_role` (DRY). Tests:
      `test_tool_authz.py`, `test_agent.py`, `test_config.py`.
- [x] **F10-G1** tamper-evident audit hash-chain: migration `011_audit_hash_chain.sql`
      (`prev_hash`/`row_hash`), `audit_store.chain_hash` + advisory-lock-serialized chained insert,
      `scripts/verify_audit_chain.py` + `make audit-verify`. Tests: `test_audit_chain.py` (offline
      tamper/deletion detection; PG round-trip skips offline).
- [x] **F10-G2** bi-temporal note validation: `kg/note.py` rejects `valid_to < valid_from` (fields
      already existed); surfaced by the parser + `kg-validate`. Test: `test_note.py`.
- [x] **F10-A** hybrid retrieval (executes/extends F8-T2): embedding provider seam
      (`agents/embedding_provider.py`, `hash` offline / `openai_compatible` prod); derived
      `note_index` (`infra/sql/010`, `report/vector_index.py` — `NoteIndex` with in-memory +
      pgvector/FTS backends, `reindex_notes` + `make reindex`); `VectorRetriever` + `LexicalRetriever`
      attached via the F7 registry (`vector`/`lexical` keys — registry membership is the enable
      switch, D-018); RRF fusion (`report/hybrid.py`) under `retrieval_mode="hybrid"` in
      `gather_evidence`, graph flat-union default unchanged. Graph traversal stays the reasoning path
      (D-004). Config: `embedding_*`, `retrieval_top_k`/`_mode`/`_fusion_k`. Tests:
      `test_embedding_provider`, `test_vector_index`, `test_hybrid_retrieval`, `test_config`.
      Deferred (follow-up): a scheduled `background-jobs` reindex activity (today `make reindex` /
      the CLI populates the index); the enable-flag booleans were intentionally folded into registry
      membership rather than added.
- [x] **F10-B** answer verification + confidence routing: `agents/verifier.py` — `verify_answer`
      scores citation faithfulness, LLM-as-judge (structured output on the routed `verifier` model,
      F10-E) when `verifier_enabled`, else the deterministic `verify_claims` gate (DRY, offline).
      `verify_turn_answer` resolves an answer's `[[wikilink]]` citations to the notes it cites; the
      runner stamps `AnswerEvent.confidence` + `unsupported_claims` so a low-confidence answer routes
      to the existing D-032 human hold. Default-off = today's plain answer. Config:
      `verifier_enabled`, `verifier_confidence_threshold`. Tests: `test_verifier`, `test_runner`,
      `test_config`. (Durable report workflow verifies at citation level — no prose there; the
      conversational path gets the LLM faithfulness score.)
- [x] **F10-F** quality metrics — P/R/F1 + drift: `evals/metrics.py` adds `precision`/`recall`/`f1`
      (pure `precision_recall_f1` over predicted vs `expected_note_ids`; report/drift metrics, no
      per-case gate); `evals/retrieval.py` scores a live retriever's P/R/F1 (`run_retrieval_eval`,
      reuses `run_eval`); `evals/baseline.py` (`aggregate_metrics`/`detect_drift`, committed
      `evals/baseline.json`) + `workflows/eval_drift.py` (`EvalDriftWorkflow` on background-jobs,
      alerts via the notify seam) + a `scripts/schedules.py` opt-in Schedule. Config:
      `eval_drift_enabled`/`_schedule_minutes`/`_epsilon`, `eval_baseline_path`. Committed pinned
      case `retrieval-precision-recall.md`. Tests: `test_metrics_classification`, `test_retrieval_eval`,
      `test_eval_drift` (incl. a baseline-matches-case-set guard), `test_schedules`, `test_config`.
      (Live retrieval cases are deployment-local — the shipped graph is empty — so only the pinned
      metric-regression case is committed; the driver scores a deployment's own retriever+corpus.)
- [ ] **F10-D** Temporal child-workflow orchestration — the last F10 ticket. OCR/vision, vendor
      connectors, GAMP-5 artifacts stay gate-until-trigger.

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
- [x] ADR **D-020** finalized + **D-A1** (F0) — written in DECISIONS.md (D-020, and D-039 = foundation
      D-A1, D-040 = foundation D-020). Checkbox was stale; confirmed present.

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

### Phase F3 — Durable session + job→session push-back
- [x] **F3-T1** Postgres session history: `agents/session_store.py::PostgresHistoryProvider`
      (overrides get/save_messages, `Message.to_dict/from_dict` → `session_messages`), migration
      `infra/sql/008_sessions.sql`, config `session_store`/`session_store_dsn`, `build_agent` selects
      via `_history_provider()`. Tests: `test_session_store.py` (unit selection + PG round-trip that
      skips offline), `test_config.py`.
- [x] **F3-T2** Session-events push-back channel: `infra/sql/009_session_events.sql`,
      `agents/session_events.py` (`SessionEvent` + `record_session_event`/`fetch_unconsumed`/
      `mark_consumed` + dependency-injected `stream_new_events` tailer), `workflows/notify.py`
      (`record_session_event_activity` + `SessionEventInput`), config `session_event_poll_seconds`.
      Tests: `test_session_events.py` (tailer loop + model + activity forwarding as unit; PG
      round-trip skips offline).
- [x] **F3-T3** job→session push-back wiring: ambient session id (`agents/session_context.py`
      contextvar, stamped by the runner); `QMJobInput.session_id` (excluded from `qm_job_key`);
      `submit_qm_job` stamps it; QM workflow calls `notify_session_best_effort` on completion (activity
      on the background queue, registered on the worker); front-door `GET /sessions/{id}/events` SSE
      streams `job_completed` push-back (`JobCompletedEvent`). Tests: `test_session_context.py`,
      `test_service.py` (all offline with fakes); the workflow-emit + DB round-trip prove live.
- [ ] Deferred within F3-T3: flipping the harness `awaiting` todo on completion (needs MAF
      TodoProvider store mutation — best done when the harness loop runs live); `PlanEvent`/live
      `JobStartedEvent` emission. ADR **D-042** written.

### Phase F4 — Entra ID identity & RBAC (system-wide)
- [x] **F4-T1** Front-door user auth (Entra OIDC): `service/auth.py` (`Principal`, `validate_token`
      with RS256 + audience + issuer checks, `require_principal` FastAPI dep), config
      `entra_required`/`entra_tenant_id`/`entra_client_id`/`entra_audience` + derived
      `entra_jwks_endpoint`/`entra_issuer_url`; guards all non-health routes; dev stand-in when
      `entra_required` is off. Dep `pyjwt[crypto]`; ruff allows `fastapi.Depends` (B008). Tests:
      `test_auth.py` (local-RSA token validation, 401 gate, dev mode), `test_config.py`.
- [x] **F4-T3** The core rule as one reusable guard: `agents/authz.py::require_actor()` returns the
      turn's ambient Entra oid and, under `entra_required`, **rejects** a user-triggered workflow with
      no user before any durable work (dev → `service_actor_id`). Wired into `submit_qm_job`
      (`requested_by = require_actor()`); `requested_by` stays out of `qm_job_key` (D-011). BO/report
      inputs adopt the same guard when they gain live triggers (no dead field now); scheduled
      ELN-sync/memory jobs run as the service by design. ADR D-044. Tests: `test_authz.py`.
- [x] **F4-T5** Authorize at one point + actor into audit: `agents/authz.py::authorize_trigger`
      (config `entra_expensive_actions`/`entra_privileged_roles`) called by `submit_qm_job` before the
      durable job; ambient identity via `agents/identity_context.py` (contextvar, stamped by the
      runner from the `Principal`); `make_audit_middleware` records the ambient Entra oid over its
      build-time default. Tests: `test_authz.py`, `test_audit.py`. Remaining in T5:
      roles→`RoleFilteredSkillsSource` per request (needs per-user agent or an ambient skills filter).
- [x] **F4-T2** Workload identity federation: `agents/identity/workload.py::WorkloadTokenProvider`
      (SA-JWT→Entra client-credentials exchange, per-scope cache). ADR D-045. `test_workload_identity.py`.
- [x] **F4-T4** OBO exchange: `agents/identity/obo.py::exchange_obo` (wired, dormant). ADR D-046.
      `test_obo.py`.
- [x] **F4-T6** Non-Entra bridges: `chemclaw/temporal_client.py::connect_options` (mTLS/api-key) +
      `agents/identity/hpc_bridge.py::map_to_hpc_identity` (logs every mapping). ADR D-047.
      `test_hpc_bridge.py`.
- [ ] **F4 live edges** (need a real tenant/broker/cluster; code + fake-endpoint tests already green):
      real Entra token validation against a live JWKS, real federation/OBO exchanges, live Temporal
      mTLS handshake. Also open: per-request role→`RoleFilteredSkillsSource` scoping.
- [x] **F5** Real HPC path behind the QM activities: `workflows/hpc/nextflow.py` (Tower REST adapter
      `launch_run`/`poll_run`/`fetch_artifacts`, fake-HTTP tested), dispatched by `hpc_launch_interface`
      (mock kept for CI). `hpc_pipeline_version` in the cache key when set (F5-T3). Worker unchanged
      (F5-T4). ADR D-048. `test_nextflow_adapter.py`.
- [ ] **F5 deferred**: `QMJobWorkflow→CalculationWorkflow` rename (cosmetic, high-churn); real `cclib`
      parsing once a live QM output format is fixed; live-cluster durability spike (needs a cluster).
- [x] **F6** OpenShift delivery: one rootless multi-target image (`deploy/Containerfile` +
      `entrypoint.sh`), Helm chart (`deploy/helm/chemclaw/`: ConfigMap/Secret, SA with federation,
      service/route/HPA, both workers, MCP, NetworkPolicy, pre-deploy migrate hook), `deploy.yml` CI
      (build + `helm template | kubeconform`), `deploy/README.md`. Config `otel_endpoint`. ADR D-049
      (D-A6/D-A6a: Temporal self-hosted). Offline-verified: YAML parse + brace-balance + Settings map.
- [ ] **F6 live edges** (CI/cluster-gated): actual image build+push, `helm template`/`kubeconform`,
      dry-run rollout to a dev namespace, OTel collector wiring, ExternalSecret wiring.
- [x] **F7** Generic data-source seam: `sources/base.py` (`DataSource` composes the existing
      `ElnAdapter`+`SourceRetriever` halves, `SourceSpec` rejects neither-half), `sources/registry.py`
      (`data_sources` config → `active_ingest_sources()`/`active_retrieve_sources()`). Re-hosted with
      no behavior change: `gather_evidence` fans out over the registry; `eln_sync` ingests active
      sources. All existing ELN/research tests pass unchanged. ADR D-050. `test_datasource_seam.py`.
- [ ] **F7 deferred (the first live connector)**: custom Snowflake ELN source — one registry entry
      (ingest half over the internal data pipeline) + per-source pipeline cursor over Snowflake's
      load-timestamp; Snowflake specifics stay inside that one adapter, nothing Snowflake-shaped above
      the seam. Also: LIMS/MES/analytical/literature adapters.

## Later — Phase 6 items now folded into F4 above (infra-gated pieces need live Entra/Temporal)

### Done — role-scoped skill visibility (D-052)
- [x] `RoleScopedSkillsSource` + `settings.skill_role_gates` gate advertised skills by the turn's
      ambient Entra roles, replacing F4's dead `allowed_skills` placeholder. Salvaged (the one
      superior, non-redundant piece) from the parallel `phase6-authz` branch; its duplicate
      `Principal` and second tool-authz path were dropped as already covered better by F4.
      `test_skill_access.py`.

- [x] Testing CLI (`agents/cli.py`, `make chat` / `uv run chemclaw`): interactive REPL + `-m`
      one-shot over the same `build_agent`. Identity is the Phase-6 seam — `resolve_identity`
      returns `(actor, allowed_skills)`; `--admin` bypasses the (unimplemented) Entra auth
      (all skills, `CHEMCLAW_CLI_ADMIN_ACTOR`), and the non-admin branch (Entra resolution)
      raises until 6.1/6.2 land. When Entra auth is built, wire it as that branch and gate the
      admin bypass off in hardened deployments. Tests: `test_cli.py`.

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
- [x] Coverage threshold in CI (D-037): `[tool.coverage.report] fail_under = 80` and CI runs
      `make lint type cov` as its gate. Floor set safely below the measured offline baseline (86%,
      Postgres/Temporal skipped; CI runs those and is higher). Ratchet upward as coverage climbs.

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

## Done — Agent-harness backbone core (MAF Agent Harness — D-038, docs/harness-konzept.md)
- [x] H0 spike: verified `create_harness_agent` in the installed `agent-framework-core` 1.11
      constructs with no LLM call; providers reduce to `TodoProvider`+`AgentModeProvider` when the
      generic batteries are off; default modes are `plan`/`execute`; `todos_remaining(looping_modes=
      ["execute"])` binds the loop to execute mode natively.
- [x] H1/H2/H3(loop): `build_agent` wires the harness behind `harness_enabled` over the *same*
      tools/skills, classic `Agent` fallback stays default; file-memory/file-access/shell/web
      batteries disabled (§6, G6); `harness_autonomy` gates the loop (`plan_only` interactive /
      `execute` looped-in-execute-mode), hard-capped by `harness_max_loop_iterations`. Config in
      `chemclaw/config.py` + `.env.example`; 8 tests in `tests/test_agent.py` (backbone select,
      provider set, same tools, batteries off, loop present/absent + bounded). `make lint type test`
      green (133 passed, 15 offline-skipped).
- [x] Evaluation: the agent harness does **not** replace Temporal or graph-based flows — Phase 5b's
      report pipeline is a deterministic core + Temporal workflow, no MAF graph-workflow code exists;
      complementary third backbone (see D-038, harness-konzept §11).
- [x] Re-integrated onto the post-5b/D-037 main: harness branch now reuses main's history,
      deterministic compaction (D-025, passed as last context provider), GxP audit middleware
      (D-027), role-filtered skills, and MCP capability tools (D-029). ADR renumbered D-020→D-038
      (D-020 was taken by the report harness on main).
- [ ] **Follow-ups (open):** `awaiting`-state resume via the durable-approval seam (D-032/D-035) ·
      plan/loop metrics for Phase 2b · plan-mode approval + finer autonomy behind RBAC (Phase 6,
      authz in the MCP server) · agent-harness ↔ report-pipeline interplay (open research per
      section vs. fixed synthesis flow).

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
