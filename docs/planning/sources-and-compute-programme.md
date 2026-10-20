# Sources and compute programme (W9–W15)

Plan to close the gaps found in the 2026-10-19 critical analysis of how Chemclaw reaches data
sources and long-running compute. It continues the numbering of the architecture programme in
`tasks/todo.md` (W0–W8) and depends on several of its items. Each repo's change is its own branch
and PR; the cross-cutting rules of `tasks/todo.md` (one concern per PR, gate green with
infrastructure up, two-phase migrations, flags with an owner and a removal item) apply unchanged.

## The gaps this plan closes

| # | Gap | Evidence in the tree |
| --- | --- | --- |
| G1 | A data source cannot contribute tools, skills or profiles; only three fixed halves | `ingest/sources/manifest.py` has no `tools:`/`skills:`; `tests/test_layering.py` has no `ingest→agent` edge |
| G2 | Question-time access is one free-text string to every source | `SourceRetriever.retrieve(query, filters)`; `gather_evidence` has no source selection; ingested ELN rows reachable only by text sweep, DRFP and `expand_note` |
| G3 | Ingest is reaction-shaped; every new data shape is a core change | `ElnAdapter.map_to_ord -> OrdReaction`; commitments touched ~25 files across 6 packages; documents needed their own durable sync |
| G4 | Entitlement is per half, not at the seam | `ShareDocumentRetriever._entitled()` self-gates; no other half checks roles |
| G5 | The prompt prefix is the scaling limit; there is no on-demand binding | `CEILINGS={"__default__": 73_450}`, `PREFIX_BOUND=83_700`; five bundles and `pyexec` are `default_enabled: false` to stay under it; `review_commitments` is always bound |
| G6 | A long-running backend means bespoke workflow code in core; none can live in the fleet | `JobSpec.workflow` names hand-written workflows; fleet forbids state and egress; the deleted `qm` tier was one launcher, not a seam |
| G7 | The D-011 key contract exists only for `calc` | `CalculationIdentity` and `engine/key.py` are calc's; generic jobs key on `stable_hash(payload)` only |
| G8 | Composition is code-bound or linear | BO objectives are a static `_REGISTRY` with a `LogSFor`-only factory; templates have no fan-out or selection |
| G9 | No compute accounting | `spend/` tracks tokens and turns; `job_records.runtime_seconds` is the only compute figure |
| G10 | A job's ending cannot wake reasoning; failure recovery has no home | No path starts a turn without a human message; `check_in.py` runs no model |
| G11 | One global job ceiling | `connector_job_timeout_seconds` (25,200 s) bounds every job; a day-long job forces a global change |
| G12 | UI has no progress, cost or backend for a job, and no structured-result affordance | `DurableJobStatus` lacks progress/usage/timestamps; no `job_progress` event |
| D1 | **Defect (latent):** a template job step drops a job's declared `effect` | `durable/template_job.py` builds `ConnectorJobInput` without `effect_*`; `tests/test_template_job_step.py` intersects field *names* and misses `effect` vs `effect_*` |

## Target architecture in one picture

```
                       ┌─────────────── LangGraph turn (one governed agent) ───────────────┐
 chemist ── UI/BFF ──► │ eager surface  +  on-demand groups (use_capability / skill pulls) │
                       │   • connector tools   • source query tools   • job launchers      │
                       │   • `task` helpers on per-source / computation profiles           │
                       └────────┬───────────────────────┬──────────────────────┬────────────┘
                                │ read                  │ launch               ▲ follow-up turn
                     ┌──────────▼─────────┐   ┌─────────▼──────────┐   ┌───────┴──────────┐
                     │ data sources       │   │ Temporal            │──►│ job events        │
                     │ halves: ingest     │   │ ConnectorJobWorkflow│   │ (failure / done)  │
                     │  (record kinds),   │   │  └ ExternalJobWF ───┼─┐ └──────────────────┘
                     │  retrieve, query   │   │  └ bundle workflows │ │
                     │ + tools/skills     │   └─────────────────────┘ │ submit/poll/fetch/cancel
                     │ + access policy    │                           ▼
                     └────────────────────┘          compute backends (drivers in core workers):
                                                     fleet-local · k8s-job · slurm-rest · tower · databricks
                     Chemclaw3-mcp (stateless, no egress): primitives with calculation_key,
                     prepare_input / parse_output for engines that run on a backend (qm)
```

Three rules survive unchanged: durability lives only in Temporal; physics lives in the fleet as
keyed, stateless primitives; every agent is compiled by `build_langgraph_agent`.

## Decisions the owner takes first (W9)

Each is an ADR under `docs/decisions/` from `TEMPLATE.md`, recommendation first. Nothing in W10+
starts before its ADR is accepted.

| ADR | Choice | Options | Recommendation | Supersedes / touches |
| --- | --- | --- | --- | --- |
| A | How a data source contributes agent surface | (1) `tools:`/`skills:`/`profiles:` on `datasource.yaml`; (2) merge sources into connectors; (3) source tools as fleet servers | **(1)**, with one shared `SurfaceSpec` model used by both manifests. Sources stay in-process and read-only; fleet servers cannot reach site systems | D-120, D-2026-08-08 (folder naming) |
| B | On-demand tool binding | (1) keep static binding + `default_enabled`; (2) per-turn recompilation from a router; (3) compile the eligible surface, show the model only active groups (`wrap_model_call` filters `request.tools`) | **(3)**: execution path and authz unchanged, prefix bound becomes eager floor + bounded active groups | D-2026-09-20 (declaring vs binding), `test_context_floor` |
| C | Structured source queries | (1) text-to-SQL by the model; (2) typed query tools generated from a declared `query:` binding; (3) only retrieve | **(2)**: bound parameters only, declared columns and operators, row caps; no model-authored SQL | D-2026-08-26 (driver signature is the schema) |
| D | Generalised ingest | (1) one half per new shape (status quo); (2) **record kinds**: `ingest:` declares a `kind`, each kind owns model, store, evidence projection, query tool | **(2)**; reaction, commitment, document migrate onto it; measurement and plate are new | D-050, D-120, commitments ADR |
| E | Where entitlement lives | (1) each half; (2) an `access:` block enforced by the registry, query tools and fan-out | **(2)**, the half's own check kept only for `read_document` | documents binding |
| F | Long-running and cluster compute | (1) stay semiempirical-only; (2) per-engine bundles (old `qm` shape); (3) a **compute backend seam** (driver manifest + one `ExternalJobWorkflow`) with engine prepare/parse primitives in the fleet | **(3)** | D-2026-08-26-semiempirical-is-the-whole-tier (in part), CLAUDE.md "Not in this system", fleet "No DFT tier" |
| G | Compute key contract | (1) calc-only; (2) every compute primitive serves `calculation_key`, the kit owns key derivation, `calc_version` includes engine version and backend image digest | **(2)** | D-011, fleet `engine/key.py` |
| H | Template fan-out | (1) composites in code only; (2) bounded `map` and `select` steps; (3) a general workflow language | **(2)**; until-converged loops stay in job specs | D-2026-08-25-the-loop-is-a-composite-not-a-template (in part) |
| I | BO objectives | (1) static registry; (2) declarative `ObjectiveSpec` over any keyed primitive or `measured` | **(2)** | `science/bo/objectives.py` |
| J | Compute budget | (1) admission ceilings only; (2) a compute ledger with reservations and per-actor/project limits in backend units | **(2)**, inside the W5.6 `spend/` module | W5.6 |
| K | Event-woken reasoning | (1) none (status quo); (2) **follow-up turns**: opted into at launch, run under the launcher's pinned identity, read-only plus `propose_resubmission` | **(2)**; the name "supervisor" stays retired (D-2026-08-12) | `durable/check_in.py` stance (in part) |
| L | Remote per-source / per-backend agents over MCP | (1) adopt as the integration pattern; (2) decline | **Decline.** `Revisit when:` a source owned by another team must be reached and that team ships an agent endpoint returning `EvidenceChunk`-shaped citations with verified actor propagation | — |

Fleet-side ADRs (in `Chemclaw3-mcp/docs/decisions/`): F′ "engine prepare/parse primitives are not
a DFT tier in the fleet" (no binary executed, no egress), G′ "every compute server serves
`calculation_key`".

---

## W9 — Decisions, the defect, baselines

Entry: none. Size S–M. Repos: all three.

- [ ] **W9.1** ADRs A–L (core) and F′, G′ (fleet), each with `## Options` and, for L, `Revisit
      when:`. Update `docs/decisions/CURRENT.md`. The owner signs off each before its wave.
- [ ] **W9.2 Fix D1.** `durable/template_job.py::_run_job_step` passes `effect_system`,
      `effect_reversal`, `effect_approver`, `effect_approval_days`; `ResolvedJob`
      (`durable/template_activities.py:197`) carries them, filled by `authorize_job_step` exactly
      as `connectors/jobs.py:382-389` fills them. Fix the test, not just the code:
      `tests/test_template_job_step.py` derives the required set from what
      `connectors/jobs.py` passes to `ConnectorJobInput` (AST of the chat-path literal), so a
      field renamed between manifest and wire is still caught. Add a test with a fixture job
      declaring `effect: {reversal: irreversible}` run as a template step: it must suspend on
      `{job_id}:approval`. Also make `templates/composed.py::authored_problems` refuse a `job`
      step whose job declares any `effect` (an agent-authored document must not launch one).
- [ ] **W9.3 Baselines** (extends W0.1): committed to `docs/planning/`:
      - prefix tokens per profile (current `_floor` output), TTFT with all bundles bound;
      - a **source eval** `data/evals/probes/sources.yaml`: 20 questions needing structured
        filters over ingested ELN rows (yield ranges, solvent, reagent, date, project) and 10
        needing a named source; scored by the judge route (not self-graded), baseline = today's
        `gather_evidence`-only surface;
      - a **compute eval** `data/evals/probes/compute.yaml`: 10 tasks needing a conformer-then-
        refine funnel or a recovery from a failed job (mock backend), baseline = today;
      - the number of files touched to add a source of a new shape (commitments: count from git).
- [ ] **W9.4 Mock services** (`Chemclaw3_mock`): a `slurmrestd`-compatible mock (submit, status
      with a configurable failure mode, accounting, cancel, artifact directory) and a DuckDB
      warehouse fixture with a `query:`-able reactions view. Both used by the live lane.

Exit: ADRs accepted; D1 fixed with a failing-first test; baselines committed.

---

## W10 — Data sources get a surface (G1, G2, G4, G5)

Entry: W9; ADRs A, B, C, E. Track C needs W5.4 (build once, bind per turn) and W5.7 (one
`context/` module) merged, or lands inside W5.7. Size L. Repos: core, UI, mock.

### Track A — `SurfaceSpec` shared by both manifests
- [ ] **W10.1** New `core/manifest_surface.py`: `SurfaceSpec{tools: list[ToolRef], skills: bool,
      profiles: bool, bind: Literal["eager", "on_demand"], summary: str}` where `ToolRef` is a
      `module:callable` (source) or a served tool name (connector). `extra="forbid"`, bounded text
      via `core/manifest_io`. Connector manifests get `bind:` (replacing the binding meaning of
      `default_enabled`, which keeps only "declared but not enabled"); data-source manifests get
      `surface:`.
- [ ] **W10.2** `DataSourceManifest.surface`: validator refuses a source tool not classified
      read-only — **a source tool can never write** (the rule in `ingest/sources/README.md`
      becomes a validator). `cli/validate_datasources.py` resolves each `ToolRef` and binds its
      signature, like halves.
- [ ] **W10.3** Registration without breaking layering: new `agent/source_tools.py` (agent may
      import ingest) loads enabled sources' tool callables at process start, registers them through
      `core.tool_registry.register_tool` under `<source_snake>_<tool>` names, and contributes
      them to `authz.read_only_tool_names()` via a new `source_tool_names()` in
      `ingest/sources/registry.py`. `agent/tool_modules.py` imports it. `tests/test_authz.py:222`
      keeps failing on any unclassified tool; collisions with core or connector names are a
      startup error naming both.
- [ ] **W10.4** Source skills and profiles: `ingest/sources/<name>/skills/` and `profiles/`
      discovered by `source_skills_dirs()` / `source_profiles_dirs()` beside
      `connectors.registry.skills_dirs()`; `langgraph_agent._skill_dirs` labels them
      `source:<name>`. `skill-validate` and `profile` validation read the declared set (not only
      enabled), as for connectors. Move the operator knowledge now in YAML comments
      (`pistachio`, `eln-databricks`, `sharedrive`) into shipped `SKILL.md` files.

### Track B — Structured queries (G2)
- [ ] **W10.5** `query:` block in `WarehouseBinding` (`ingest/eln/warehouse/binding.py`):
      `QueryBinding{relation, key, citation: FieldBinding, columns: dict[str, QueryColumn],
      default_order, max_rows ≤ 200}`, `QueryColumn{column, type: string|number|date|enum|smiles,
      ops: list[eq|in|gte|lte|between|contains], unit, description, enum_values}`. Identifiers
      through `_check_identifier`; `contains` compiles to a bound `LIKE` with escaping.
- [ ] **W10.6** `sql.query_statement(binding, spec)` (`ingest/eln/warehouse/sql.py`): `WHERE`
      from declared columns and operators only, every value a positional bind via
      `warehouse.placeholder`, deterministic `ORDER BY` ending in the key, `LIMIT max_rows + 1` to
      report truncation. Dialect differences (`ILIKE`, date literals) go on `Warehouse` beside
      `vector_dialect`. Tests: exact-statement assertions per dialect (Snowflake, Databricks,
      DuckDB), injection attempts in every value position, an undeclared column refused.
- [ ] **W10.7** Generated query tool: for each enabled source with a `query:` block,
      `agent/source_tools.py` builds `query_<source>(filters: <pydantic model generated from the
      columns>, order_by, limit)` with the column descriptions and units as field descriptions,
      returning `SourceQueryResult{rows, truncated, source, citation per row, unit per column}`.
      Each row's citation names the source it was found in
      (D-2026-09-13-a-citation-names-the-source-it-was-found-in). Schema ≤ `MAX_SINGLE_TOOL_TOKENS`.
- [ ] **W10.8** `query_reaction_records` — the same generated shape over core's own
      `reaction_records` (Postgres), so **already ingested** ELN data is filterable: project,
      performed_at, yield (from `conditions`), solvent, reagent and product SMILES (from
      `species`), `ingest_source`, tier; retracted rows excluded and counted. Migration
      `infra/sql/1xx_reaction_records_query.sql`: generated columns or GIN indexes on the JSONB
      paths used, `tenant_id` + RLS per W7.5. Hits carry note ids accepted by `expand_note`.
      Structure filters reuse `molfp`/`rxnfp` (the tool takes an optional `similar_to` reaction
      SMILES and intersects with the DRFP hits) instead of re-implementing search in SQL.
- [ ] **W10.9** `gather_evidence` gains `sources: list[str] | None` (validated against the
      active retrieve sources; empty means all) and returns `structured_alternatives`: the query
      tools that cover the sources that answered, so the model learns to switch from the sweep to
      a filter. `retrieval/fanout.py` takes the narrowed list unchanged.
- [ ] **W10.10** Mock and live lane: the DuckDB fixture source (W9.4) ships a `query:` block; a
      live-lane probe answers a filtered question end to end.

### Track C — On-demand binding (G5)
- [ ] **W10.11** State: `ChemclawState.active_capabilities: Annotated[list[str], union]`,
      checkpointed, capped at `agent_active_capability_max` (config, default 3, LRU eviction with
      the evicted name reported in the next tool result).
- [ ] **W10.12** Middleware in the W5.7 `context/` module: `wrap_model_call` sets
      `request.tools = eager + tools of active groups`, in a stable order (eager first, then
      groups in activation order) so the cached prefix breaks once per activation, not per
      turn. The `ToolNode` is still built with the whole eligible surface, so execution, audit
      and authz are unchanged; a call to a tool of an inactive group is refused with a message
      naming `use_capability` (it would otherwise be a hallucinated call that happens to work).
- [ ] **W10.13** `use_capability(name, reason)` tool (read-only): its description is a generated
      catalogue of on-demand groups, one `summary` line each (bounded by a test). A skill read
      whose `tools:` names a group's tools activates that group (`skills/backends.py`
      `ReloadingSkillsMiddleware` hook), so "a skill pulls its tools".
- [ ] **W10.14** `tests/test_context_floor.py`: `CEILINGS` becomes the **eager** floor;
      `ON_DEMAND_GROUP_MAX_TOKENS` bounds each group; `PREFIX_BOUND = eager +
      agent_active_capability_max × ON_DEMAND_GROUP_MAX_TOKENS + allowances`. With W5.7 it is
      measured into `data/` by `make measure-prefix`. The identical-prefix tests assert per
      activation set. `core/config/agent.py` derivations follow.
- [ ] **W10.15** Flip `props`, `thermalsafety`, `kinetics`, `unitops`, `suitability`, `pyexec`
      (fleet manifests: `bind: on_demand`, contracts minor bump) and `review_commitments`, source
      query tools, `bo` read tools to on-demand. Ratchet the eager `CEILINGS` down to the
      measurement.

### Track D — Entitlement at the seam (G4)
- [ ] **W10.16** `access:` block on `datasource.yaml` (`public: true` xor `roles: [...]`, same
      shape as the share binding). `active_retrieve_sources()` and `source_tool_names()` filter by
      `get_current_roles()`; an unentitled actor sees neither the source's chunks nor its tools
      (the tool is withheld, not refused, so its schema costs nothing). `ShareDocumentRetriever`
      keeps its check only on `read_document`. Test: a route-wide leak test over every source and
      tool for an actor with no roles.

### Track E — UI
- [ ] **W10.17** `Chemclaw3_ui`: a `source-query` renderer above the generic `table` in
      `src/results/renderers.tsx` (column units, truncation banner, per-row citation link opening
      the note or source record, CSV via `DownloadCsv`); a chip in the trace for
      `use_capability` activations. Contract bump per `docs/api-contract.md` if the result model is
      published in the OpenAPI document.

Exit (measured against W9.3):
- adding a SQL source with a working typed query tool and a skill is **one folder, zero Python**
  (a test adds a fixture folder and calls the tool, extending `test_datasource_seam`);
- source eval: correct answers up by ≥ 25 points over the `gather_evidence`-only baseline, judged
  by a model other than the one under test, with no regression on the existing probe sets;
- eager prefix below today's 73,450 with all fleet bundles reachable on demand; TTFT not worse.

Rollback: `bind: on_demand` → `eager` per group; query tools are additive; `access:` defaults to
the source's current behaviour during one release.

---

## W11 — Record kinds: ingest beyond reactions (G3)

Entry: W10 Track A; ADR D; W4 (KG in Postgres) merged, because the reaction kind writes through
`kg/record.py`'s Postgres store. Size L. Repos: core, mock.

- [ ] **W11.1** `ingest/records/` (new subpackage; `ARCHITECTURE.md` row and README): `RecordKind`
      protocol `{name, model: type[BaseModel], store(), to_evidence(record) -> EvidenceChunk |
      None, query_columns() -> dict[str, QueryColumn]}` and a registry of shipped kinds. The ingest
      half becomes `IngestHalf[K]`: `fetch_new_entries(since) -> list[RawEntry]` and
      `map(raw) -> K`.
- [ ] **W11.2** Manifest: `ingest:` accepts `{adapter: module:callable, kind: <name>}`; a bare
      string means `kind: reaction` for one release (logged deprecation), then the bare form is
      refused. `labels:` is valid only for kinds that carry reactions.
- [ ] **W11.3** Kind `reaction`: wraps the current path unchanged — `ElnAdapter.map_to_ord` is
      `map` for this kind; `record_from_ord_reaction` and `reaction_records` untouched.
      Behaviour-neutral, proven by the existing ELN tests and a byte-identical sync run.
- [ ] **W11.4** One `durable/record_sync.py` workflow parameterised by `(source, kind)`, replacing
      `durable/eln_sync.py` and `durable/commitment_sync.py`; per-source cursors (D-054) and the
      rejection ledger reused; `durable/schedules.py` asks the manifests for kinds. Replay test for
      in-flight runs of the old workflows (patch marker; old types kept registered for one release).
- [ ] **W11.5** Migrate `commitments` onto the seam as kind `commitment` (its half protocol becomes
      a kind); delete the third-half special cases in `base.py`, `manifest.py`, `registry.py`,
      `validate_datasources.py`. `review_commitments` becomes the kind's query tool, on demand.
- [ ] **W11.6** Migrate `documents`' indexing onto kind `document` (`fetch_new_entries` = crawl
      diff, `map` = parse + chunk), so `durable/document_sync.py` is the generic sync with the
      document store. The retrieve half is unchanged.
- [ ] **W11.7** New kind `measurement` for LIMS/analytical data: `AnalyticalResult{sample_id,
      lot, method, analyte, value: core.units.Measurement, instrument, acquired_at, raw_ref,
      source}`; table `analytical_results` (tenant_id + RLS); feeds `analytical/` verdicts and
      stability trending, which today have no ingested input; generated `query_measurements`.
      Warehouse binding: `IngestBinding` generalised so `_MAPPABLE_FIELDS` derives from the kind's
      model, making a LIMS view attachable by manifest.
- [ ] **W11.8** New kind `plate` (HTE): plate, wells, conditions and outcomes, linked to the
      `protocols/` HTE envelope; each well also yields a `reaction` record so DRFP and the sweep
      reach it.
- [ ] **W11.9** Mock: a LIMS export and an HTE plate export in `Chemclaw3_mock`; live-lane probes.

Exit: adding a source of an existing kind is a manifest; adding a **new kind** touches only
`ingest/records/<kind>.py`, one migration and a manifest (measured: files touched vs the
commitments baseline from W9.3); every kind's rows are filterable through a generated tool.

Rollback: kinds are additive; the reaction kind is the current code path; old sync workflows stay
registered for one release.

---

## W12 — Compute backends and the key contract (G6, G7, G11)

Entry: W9; ADRs F, G, F′, G′; W6.2–W6.4 (backends speak `/rpc`) and W2 (contracts versioning)
merged. Size L. Repos: mcp, core, UI, mock.

### Track A — Key contract (fleet + core)
- [ ] **W12.1** Fleet: move `CalculationKey.build` and `stable_hash` from
      `servers/calc/engine/key.py` into `mcp_server_kit/keys.py`; `calc` imports it (no key
      changes — `tests/test_key_contract.py` proves byte-identical keys).
- [ ] **W12.2** Contracts: `chemclaw_contracts/compute.py` with `ComputeIdentity` (today's
      `CalculationIdentity`, kept as an alias for one minor) and `KeyedResult`. A manifest flag
      `compute: true` obliges a server to serve `calculation_key` over `/rpc` for each compute tool;
      fleet test `tests/test_fleet_compute_keys.py` calls it for every such tool and refuses a
      null key without a stated caveat.
- [ ] **W12.3** `calc_version` composition rule (written in the kit): `method + engine +
      engine_version + image_digest` for any engine that runs off the serving pod (the digest is
      the backend image's, declared in the backend manifest), so two clusters with different
      builds never share a cache row.
- [ ] **W12.4** Core: `connectors/calc/remote.py::cached_remote` generalised into
      `compute/remote.py::cached_primitive(backend_name, request)`; `calc` is its first caller.
      `science/calc/store.py::cached_compute` is already key-generic and stays; `calculation_results`
      gains `backend` (expand/contract migration). `CALCULATION_EPOCH` composition unchanged.

### Track B — The backend seam (core)
- [ ] **W12.5** New subpackage `src/chemclaw/compute/` (row in `ARCHITECTURE.md`, README,
      `tests/test_layering.py` edges: `compute→core`, `connectors→compute`, `durable→compute`):
      - `backend.py`: `ExecutionBackend` protocol — `submit(JobBundle) -> Handle`,
        `status(Handle) -> BackendStatus{state, phase, progress: float | None, usage: Usage | None,
        message}`, `fetch(Handle, dest) -> ArtifactSet`, `cancel(Handle)`, `estimate(JobBundle)
        -> Usage`;
      - `manifest.py`: `compute/backends/<name>/backend.yaml` — `name`, `driver: module:callable`,
        `config` (bound against the driver signature, `*_env` for secrets, as D-2026-08-26 drivers),
        `units` (e.g. `core_hours`), `max_walltime_seconds`, `poll_seconds`, `engines: {orca:
        {version, image_digest}}`, `egress: [host:port]`;
      - `registry.py`: discovery on `CHEMCLAW_COMPUTE_BACKENDS_DIR`, enablement by
        `CHEMCLAW_COMPUTE_BACKENDS`, `make backend-validate`.
- [ ] **W12.6** Drivers, each with offline lifecycle tests against an injectable transport:
      `fleet` (runs a fleet primitive over `/rpc`; today's `calc` path expressed as a backend),
      `k8s_job` (OpenShift/Kubernetes Job API, in-cluster service account), `slurm_rest`
      (`slurmrestd`), `tower` (Seqera, the D-048 adapter rebuilt on the seam), `databricks_jobs`.
      Ship `fleet` and `slurm_rest` first; the rest are one PR each.
- [ ] **W12.7** `durable/compute_job.py::ExternalJobWorkflow`, the one workflow every backend job
      runs, as the child of `ConnectorJobWorkflow`:
      1. `prepare` activity → fleet `prepare_input` over `/rpc` → files + key;
      2. cache lookup by key (a hit returns without submitting);
      3. budget reservation (W13 Track C; a no-op until then);
      4. `submit` activity (idempotent: the handle is derived from the workflow id and checked
         before resubmission);
      5. poll with `workflow.sleep(poll_seconds)` and a short `status` activity per tick —
         durable timers, not a heartbeating activity, so a worker restart costs nothing; each
         tick's status becomes a `job_progress` session event when it changes;
         `continue_as_new` after N ticks;
      6. `fetch` to the artifact store; `parse` via fleet `parse_output`; persist through
         `cached_compute` under the key from step 1;
      7. settle usage; return a `ConnectorJobResult` with `calc_refs`.
      Cancellation runs the `cancel` activity as compensation (`cancel_job` reaches the
      scheduler). Failures carry the scheduler's reason and the parsed engine error (W14 uses it).
- [ ] **W12.8** `JobSpec.compute:` block — `{primitive: <fleet tool>, backend: <name> | selector,
      resources: {cores, memory_gb, walltime_seconds}}` with `workflow` defaulting to
      `ExternalJobWorkflow`. **A new HPC capability is a fleet primitive plus a manifest job; no
      workflow code in core.** `connectors/jobs.py` validates the backend is enabled and the
      walltime is within its `max_walltime_seconds`.
- [ ] **W12.9** Per-job ceilings (G11): `timeout_seconds` may *raise* the ceiling when the job is
      listed in `connector_jobs_long_running_list` (operator grant, like
      `connector_jobs_awaiting_answer_list`); `_the_job_ceiling_covers_the_activity_it_bounds`
      becomes per job; the global default can then drop back to the longest *inline* bundle need.
- [ ] **W12.10** Deployment: component `compute-worker-<backend>` (entrypoint glob, chart entry
      `computeBackends.<name>` with NetworkPolicy egress only to the manifest's `egress:` hosts),
      core netguard allowlist derived from enabled backends; `tests/test_no_egress.py` gains the
      sanctioned row with the argument the ELN sources already make (the deployment's own system);
      `tests/test_deploy_chart.py` both-direction checks extended.
- [ ] **W12.11** Identity: backend service credential per backend; the actor, session and
      correlation ids go into the scheduler job's comment/labels; optional `account_map` from actor
      to cluster account for sites that charge per user.

### Track C — The DFT capability, as the first real backend user
- [ ] **W12.12** Fleet `servers/qm` (port claimed in `MODULES.md`, e.g. 8866): `prepare_input`
      (task: single point / optimisation / frequencies; method, basis, dispersion, solvation;
      structure by `structure_id` from `calc` or XYZ; charge, multiplicity) → engine input files
      plus `calculation_key`; `parse_output` → typed results (energy, gradient norms, frequencies,
      thermochemistry, convergence flags, engine error class). No binary runs in the pod; egress
      guard and static scan unchanged. Parsers are tested on committed engine outputs.
- [ ] **W12.13** Core bundle `connectors/qm/`: manifest only — jobs `compute_dft_energy`,
      `optimize_geometry_dft`, `compute_dft_frequencies` with `compute:` blocks; skill
      `dft-method-selection` (when DFT beats xTB's error bar, functional/basis defaults, cost);
      `calc/skills/calculation-selection` updated to tier xTB → DFT.
- [ ] **W12.14** `publish/project.py` `dft` projector re-attached to the new payload kind.
- [ ] **W12.15** UI: `job_progress` event (`{job_id, phase: queued|running|fetching|parsing,
      progress, usage, backend}`) and `DurableJobStatus` gains `phase`, `progress`, `usage`,
      `backend`, `started_at`, `estimate` (core OpenAPI → lock bump → `npm run generate:api` →
      `shared/events.ts` refinement → `chatStore`/`JobsPanel`); cancel already exists.

Exit: with the slurm mock, a DFT job survives a worker kill mid-poll, a repeated request is a cache
hit with no submission, `cancel_job` reaches the scheduler within one poll interval, and adding a
second engine is a fleet primitive plus a manifest (test with a fixture engine).

Rollback: no backend enabled = today's system; `qm` is off until `CHEMCLAW_COMPUTE_BACKENDS` names one.

---

## W13 — Composition and compute budget (G8, G9)

Entry: W12 Track A/B; ADRs H, I, J; W5.6 merged for Track C. Size M–L. Repo: core (+ UI).

### Track A — Template `map` and `select`
- [ ] **W13.1** `templates/manifest.py`: `MapStep{over: ref to a list, as: str, step: ToolStep |
      JobStep, max_items ≤ template_map_max_items, concurrency}` and `SelectStep{from: ref,
      by: field path, order: asc|desc, top: int, where: list of {field, op, value}}`
      (deterministic, no expressions). Update `_step_value`, `step_references`,
      `templates/resolve.py` (the item variable), `templates/schedule.py`.
- [ ] **W13.2** `durable/template_job.py`: dispatch for both kinds; `map` uses
      `durable/orchestrator.py::fan_out` (bounded, poison-tolerant) with child ids
      `child_workflow_id(f"{step.id}-{index}")`; patch marker and `tests/test_workflow_replay.py`
      coverage; `template_step_ceilings()` and `agent/template_surface.py::run_ceiling_problems`
      know the new kinds (ceiling = per-item ceiling × ⌈items / concurrency⌉).
- [ ] **W13.3** `templates/composed.py::authored_problems`: an agent-authored `map` may wrap only
      read-only tool steps or jobs already allowed for composed workflows; `select` is always
      allowed; `agent/workflow_tools.py::compose_workflow` learns both.
- [ ] **W13.4** Shipped template `data/templates/screen-then-refine.yaml`: map xTB properties over
      candidates → select top-k → map DFT single points (W12) → select top-3 → agent write-up.
      Live-lane probe with the mock backend.

### Track B — Declarative BO objectives
- [ ] **W13.5** `science/bo/objectives.py`: `ObjectiveSpec = Computed{primitive, field, direction,
      transform: none|log10|negate, backend?} | Measured{unit}`; the static `_REGISTRY` is replaced
      by specs in `data/objectives/*.yaml` (`reizman_suzuki`, `solubility_max` ported verbatim and
      proven by `test_bo_campaign`). The factory takes a generic `Evaluate` callable built in
      `connectors/bo/calculators.py` from `cached_primitive`, so any keyed primitive is an
      objective.
- [ ] **W13.6** `BoCampaignWorkflow._evaluate` gains a third branch: an objective on a
      long-running backend evaluates each batch as child `ExternalJobWorkflow`s via `fan_out`;
      `_cannot_afford_another_dispatch` consults the compute budget (Track C).

### Track C — Compute budget
- [ ] **W13.7** In the W5.6 `spend/` module: `ComputeUsage{backend, unit, amount}`, table
      `compute_usage` (tenant_id + RLS: actor, project, backend, unit, reserved, used, job_id,
      window_start), `ComputePolicy` limits per actor, project and tenant per window.
- [ ] **W13.8** Reservation at launch (`connectors/jobs.py::prepare_job_launch` → `estimate` →
      `reserve`, refused with the remaining balance in the message), settlement from
      `BackendStatus.usage` at completion, release on failure or cancel. `fleet` backend reports
      CPU seconds; the xTB composites settle from `job_records.runtime_seconds`.
- [ ] **W13.9** Agent tool `compute_budget_status` (read-only, on demand); job launcher docstrings
      state the estimate; `operations/` and the evidence pack show compute spend; UI shows estimate
      and actual on the job card.

Exit: the funnel template runs end to end on the mock; a BO campaign optimises a DFT-backed
objective on the mock; an over-budget launch is refused before submission; no existing template or
campaign changes behaviour (replay and existing tests).

---

## W14 — Follow-up turns and specialists (G10)

Entry: W12; ADR K; W5.9 (one authz decision) and W5.13 (runner stages) merged. Size M. Repos: core, UI.

- [ ] **W14.1** Launch-time opt-in: job launchers take `follow_up: none | on_failure |
      on_completion` (default `on_failure` for `compute:` jobs, `none` otherwise), recorded in
      `ConnectorJobInput` with a `StepIdentity` snapshot (actor, roles, session, correlation) —
      the consent is the launch, made under the plan gate, not a timer.
- [ ] **W14.2** `durable/follow_up.py::FollowUpWorkflow`, started by `ConnectorJobWorkflow._finish`
      / `_notify_failure`: runs one agent turn through the `run_agent_step` machinery
      (`build_langgraph_agent`, `_acting_as`, `turn_caps`) under the `computation` profile,
      read-only narrowed, with the job's failure (scheduler reason, parsed engine error, inputs)
      as the brief. Capped per job (`follow_up_max_per_job`, default 2) and charged to spend.
- [ ] **W14.3** `propose_resubmission(job_id, changes, rationale)`: creates a pending action
      (existing `pending_tools` path) that the chemist approves in the UI; approval relaunches
      through the normal launcher with a new idempotency key. The follow-up turn can never launch.
- [ ] **W14.4** Transcript and stream: the follow-up's answer lands in the session as an agent
      message with `origin: follow_up` and a `follow_up` SSE event; UI renders it distinctly with
      the pending resubmission's approve/decline.
- [ ] **W14.5** Specialist profiles: sources ship profiles (e.g. `eln-explorer` with the source's
      query tools and skill); `data/profiles/computation.yaml` gains DFT jobs and the budget tool.
      They are reached by `task` for isolation, not routing (D-2026-08-13).
- [ ] **W14.6** Re-measure delegation (with W5.14) on the new probe sets (multi-source structured
      questions, compute funnels, recovery) with a judge route distinct from the agent's model;
      decide on the helper roster from that run only.

Exit: on the mock, a job failing with an SCF non-convergence produces a follow-up message with a
correct resubmission proposal in ≥ 8/10 probes; no follow-up turn ever launches work (test over
the bound surface); the compute eval improves over its W9.3 baseline.

---

## W15 — Documents, cleanup, measurement

- [ ] **W15.1** `Chemclaw3/CLAUDE.md`: remove HPC/DFT/cluster from "Not in this system"; replace
      "Every calculation is GFN2-xTB or CREST" with the tiering rule; add the compute-backend
      seam and record kinds to "Seams"; `ARCHITECTURE.md` rows for `compute/` and
      `ingest/records/`. `Chemclaw3-mcp/CLAUDE.md`: the `qm` prepare/parse exception to "No DFT
      tier" and the `compute: true` key obligation. UI `CLAUDE.md` unchanged unless routes moved.
- [ ] **W15.2** Remove the one-release shims: bare `ingest:` strings, `CalculationIdentity` alias,
      old sync workflow registrations, `default_enabled`'s binding meaning.
- [ ] **W15.3** `docs/decisions/CURRENT.md`, `docs/planning/BACKLOG.md` (rows closed are deleted),
      `tasks/lessons.md` if any correction happened.
- [ ] **W15.4** Final measurement against W9.3: prefix, TTFT, source and compute evals, files
      touched per new source and per new engine, and the scale numbers of W3 unchanged.

## Sequencing against the running programme

| This plan | Needs from `tasks/todo.md` | Can run beside |
| --- | --- | --- |
| W9 | — | anything |
| W10 A, B, D | W2 (contracts) for the fleet `bind:` field | W4, W6 |
| W10 C | W5.4, W5.7 (or lands inside W5.7) | W4, W6 |
| W11 | W4 (KG in Postgres), W7.5 (tenancy) for new tables | W12 |
| W12 | W6.2–W6.4 (`/rpc`), W2 | W11 |
| W13 C | W5.6 (one spend module) | W14 |
| W14 | W5.9, W5.13, W5.14 | W13 |
| W15 | all | — |

New tables (`analytical_results`, `compute_usage`, plate tables, query indexes) are created with
`tenant_id` and RLS from the first migration, so W7.5 never retrofits them. W7.8 (lineage) adds
backend keys and source citations to the provenance join.

## Risks

| Risk | Wave | Mitigation |
| --- | --- | --- |
| On-demand binding confuses the model or breaks prompt caching | W10 C | Eval-gated (W2.14 protocol); stable tool order; activation cap; per-group rollback to eager |
| Generated query tools leak data across entitlements or tenants | W10 B/D | Withheld, not refused; RLS on Postgres-backed queries; leak test over every tool and actor class |
| SQL injection through the query builder | W10 B | Values only ever bound; identifiers only from the validated binding; fuzz test per dialect |
| Reviving cluster compute recreates the cost D-2026-08-26 removed | W12 | Backends are off until enabled; no `hpc_*` settings in core config (all in the backend manifest); mock-tested lifecycle; per-job ceilings, so nothing global grows |
| Cache rows shared across different engine builds | W12 A | `calc_version` includes engine version and image digest; key contract test in the fleet |
| Follow-up turns read as the system acting on its own | W14 | Opt-in at launch under the plan gate; read-only surface; can only propose; distinct transcript origin; per-job cap |
| Template fan-out explodes cost | W13 | `max_items`, concurrency, run-ceiling check before start, compute budget reservation per item |
| Parallel sessions collide with W4–W7 | all | Tracks own disjoint files; the sequencing table is the claim order; one GitHub issue per track |
