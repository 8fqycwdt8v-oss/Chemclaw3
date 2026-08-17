# Sweep: configuration as a surface

Method: enumerated all **327** `Settings` fields programmatically and grepped each across `src/`;
rendered the shipped Helm chart with the real `helm` binary and fed the rendered ConfigMap back into
`Settings()`; opened a live Postgres-backed process and inspected the pool objects it actually
constructs. Everything below was produced by running something, not by reading a comment.

Seven findings. The two that matter are #1 and #2 — the fleet connection budget and its runtime
alert are both blind to the pool that every durable turn writes through, and the chart's
`connectors.<name>.enabled: false` switch is half-wired.

---

## 1. The fleet connection guard counts one pool per process; a front-door process opens two

- **Severity**: high
- **Location**: `src/chemclaw/core/config/__init__.py:217-229` (`_guards_that_the_comments_already_demand`) ·
  `src/chemclaw/agent/checkpointer.py:365-373` (`_checkpoint_pool`) ·
  `src/chemclaw/core/db.py:269-281` (`pool_stats`) ·
  `deploy/helm/chemclaw/templates/_helpers.tpl:498-517` (`chemclaw.pooledProcesses`)
- **Trigger**: any deployment with `session_store="postgres"` — which is what the shipped chart sets
  (`deploy/helm/chemclaw/values.yaml:341`). Take one turn on the front door.
- **Consequence**: the startup guard computes `pg_fleet_pooled_processes × pg_pool_max_size` and
  passes, while the process opens **twice** `pg_pool_max_size`. `core.db._POOLS` holds one pool per
  `(dsn, options)`, and `agent/checkpointer._checkpoint_pool` builds a **separate** `AsyncConnectionPool`
  with `max_size=settings.pg_pool_max_size` that `_POOLS` never sees. With the shipped chart the real
  fleet ceiling is 168 connections against a declared server ceiling of 136, and the guard reports 120.
  This is precisely the failure the guard exists to prevent, restated one pool over.

  The runtime half is blind for the same reason: `chemclaw_pg_pool_max_size` is bound to
  `settings.pg_pool_max_size` (one value per process, `core/db.py:238`), and the alert
  `sum(chemclaw_pg_pool_max_size) > max(chemclaw_pg_fleet_max_connections)`
  (`deploy/helm/chemclaw/templates/prometheusrule.yaml`, asserted at `tests/test_deploy_chart.py:1037`)
  therefore evaluates 15×8=120 vs 136 and never fires either. And `pool_stats()` iterates `_POOLS`
  only, so `chemclaw_pg_pool_size` / `_available` / `_requests_waiting` report **nothing** about the
  checkpointer pool — the one every turn's state write goes through.
- **Evidence**: measured against the live Postgres in this sandbox (`/tmp/poolcount.py`):

  ```
  pg_pool_max_size = 16
  core.db pools in this process: 1 [16]
  checkpointer pool max_size  : 16 timeout: 30.0 max_idle: 600.0
  core pool timeout: 10.0 max_idle: 300.0
  PER-PROCESS CEILING (sockets): 32
  guard assumes per process     : 16
  ```

  Shipped-chart arithmetic (rendered with `helm template`, `pg_pool_max_size=8`,
  `pg_fleet_pooled_processes=15`, `pg_fleet_max_connections=136`):

  | process | count | core pool | checkpointer pool | total |
  |---|---|---|---|---|
  | front door (HPA max) | 6 | 48 | 48 | 96 |
  | background worker | 1 | 8 | – | 8 |
  | connector servers (bo, calc, molfp, rxnfp) | 4 | 32 | – | 32 |
  | connector workers (bo, calc, qm×2) | 4 | 32 | – | 32 |
  | | | | | **168** |

  Guard computes 120 ≤ 136 → boots. `agent/checkpointer.py` is reached from
  `api/runner._turn_checkpointer` (`api/runner.py:597-611`) on every turn when
  `session_store == "postgres"`.
- **Fix**: make the multiplicand honest rather than the count. Either (a) have the checkpointer
  borrow from `core.db` (register its pool in `_POOLS` under its own `options` key — it needs
  `autocommit`, which is a distinct key, so this works and gets it `pool_stats` coverage for free),
  or (b) if the second pool must stay separate, give the validator a per-process pool count
  (`pools_per_turn_process`) and bind `chemclaw_pg_pool_max_size` to `sum(p.max_size for p in pools)`
  including the checkpointer's, so the startup check and the alert measure the same thing the
  database sees.

---

## 2. `connectors.<name>.enabled: false` removes the pods and leaves the bundle loaded

- **Severity**: high
- **Location**: `deploy/helm/chemclaw/values.yaml:128-137` and the `config:` block ·
  `deploy/helm/chemclaw/templates/_helpers.tpl:457-473` (`chemclaw.connectorUrls`) ·
  `src/chemclaw/core/config/connectors.py:37` (`connectors_enabled: str = ""`)
- **Trigger**: `helm template chemclaw deploy/helm/chemclaw --set connectors.molfp.enabled=false`
- **Consequence**: the chart *derives* `CHEMCLAW_CONNECTOR_URLS` and `CHEMCLAW_PG_FLEET_POOLED_PROCESSES`
  from `.Values.connectors` precisely so the topology is declared once — but it never emits
  `CHEMCLAW_CONNECTORS_ENABLED`, and the code default `""` means *every discovered bundle*. So a
  disabled connector: loses its Deployment, loses its `connector_urls` entry, and stays enabled in the
  registry — falling back to its manifest's loopback dev address, which the front door then dials
  **inside its own pod**. Its tools are still assembled into the model's tool surface and fail at call
  time; `/readyz` reports it `unreachable` forever; and with `connectors_required: true` (the
  documented fail-fast posture) **every pod fails startup**. This is the exact defect
  `registry.health_url` was written to fix (D-131, quoted in its own docstring) reproduced on the
  *tool* endpoint instead of the probe.

  The values file does warn ("Enabling a connector here is only half the switch:
  `CHEMCLAW_CONNECTORS_ENABLED` in `config` below"), but that cross-reference is false — there is no
  such key in `config`, and nothing checks the pairing. `tests/test_deploy_chart.py` checks
  `server`/`worker` against the manifest in both directions and does not check this one.
- **Evidence**: `/tmp/disabled.py` against the rendered chart:

  ```
  CONNECTOR_URLS: {"bo":...,"calc":...,"chem":...,"rxnfp":...,"safety":...}   # molfp gone
  CONNECTORS_ENABLED key present: False
  molfp Deployment rendered: False
  registry.enabled() -> ['bo', 'calc', 'chem', 'molfp', 'qm', 'rxnfp', 'safety']
  molfp effective tool URL the front door will dial: http://127.0.0.1:8811/mcp
  ```
- **Fix**: derive it, exactly as `connectorUrls` is derived. Add to `templates/config.yaml`:
  `CHEMCLAW_CONNECTORS_ENABLED: {{ include "chemclaw.enabledConnectors" . }}` over the same
  `range .Values.connectors` / `if $cfg.enabled` predicate, joined with `:`. Then delete the stale
  "in `config` below" sentence, since the key is no longer hand-maintained.

---

## 3. The checkpointer pool silently ignores `pg_pool_timeout_seconds` and `pg_pool_max_idle_seconds`

- **Severity**: medium
- **Location**: `src/chemclaw/agent/checkpointer.py:365-373` vs `src/chemclaw/core/db.py:145-158`
- **Trigger**: saturate the checkpointer pool (more concurrent turns than `pg_pool_max_size` writing
  state at once).
- **Consequence**: the same two settings get two different answers depending on which pool a caller
  lands in. `_pool_for` passes `timeout=settings.pg_pool_timeout_seconds` (10.0) and
  `max_idle=settings.pg_pool_max_idle_seconds` (300.0); `_checkpoint_pool` passes neither, so
  psycopg_pool's own defaults stand — **30.0 s** and **600.0 s**. A turn waiting on a checkpoint write
  therefore blocks for 30 s, holding its admission permit, where the deployment configured a 10 s
  fail-fast; and an idle checkpointer connection is held twice as long as configured, which feeds
  finding #1. `core/db.py`'s docstring calls the 10 s bound "the same transient infrastructure fault
  (a `ConnectionError`, which Temporal retries)" — the checkpointer path produces neither the timing
  nor the exception type.
- **Evidence**: printed directly off the two live pool objects (see #1's output):
  `checkpointer pool timeout: 30.0 max_idle: 600.0` vs `core pool timeout: 10.0 max_idle: 300.0`.
- **Fix**: pass `timeout=settings.pg_pool_timeout_seconds, max_idle=settings.pg_pool_max_idle_seconds`
  in `_checkpoint_pool`, or (better, and it also fixes #1) build it through `core.db._pool_for` with
  the autocommit option string.

---

## 4. `otel_llm_spans` and `otel_include_sensitive_data` are silently inert when `otel_enabled=false` — contradicting the docstring that promises otherwise

- **Severity**: medium
- **Location**: `src/chemclaw/core/logging.py:272-275` (early return) vs `:302-340`
  (`_warn_about_sensitive_data`) and `:375-376` (`_instrument_llm_calls`)
- **Trigger**: `CHEMCLAW_OTEL_ENABLED=false CHEMCLAW_OTEL_LLM_SPANS=true CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA=true`
- **Consequence**: `configure_telemetry` returns at `if not settings.otel_enabled` before
  `_instrument_llm_calls` and before `_warn_about_sensitive_data`. Two settings that read as switched
  on do nothing, no validator rejects the combination, and **nothing is logged**. `Settings` refuses
  five other incoherent combinations at startup (workers>1, fleet ceilings, resume>turn, budgets all
  zero, embedding width) — this one it accepts.

  This is also a comment that measures wrong. `configure_telemetry`'s own docstring states: *"It still
  governs nothing when that is off — and the warning below still says so out loud in exactly that
  case, rather than letting an enabled-but-ineffective privacy switch read as an effective one."*
  The warning is unreachable in exactly that case, because it lives inside the `otel_enabled` branch.
  The `otel_llm_spans` config comment likewise promises `RuntimeError` when the instrumentation
  package is missing — also unreachable.
- **Evidence**:

  ```
  $ CHEMCLAW_OTEL_ENABLED=false CHEMCLAW_OTEL_LLM_SPANS=true \
    CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA=true uv run python -c '...configure_telemetry()...'
  otel_enabled False llm_spans True sensitive True
  instrumented? False
  ```
  (log level DEBUG; no warning emitted at all)
- **Fix**: add to `_guards_that_the_comments_already_demand`:
  `if self.otel_llm_spans and not self.otel_enabled: raise ValueError(...)`. That is the shape the
  neighbouring five guards already take, and it makes the docstring's promise true by making the
  combination unreachable instead of unwarned.

---

## 5. Four settings with no reader anywhere in `src/` — three of them describing a deleted credential path the chart still advertises

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/entra.py:74-76` · `src/chemclaw/core/config/kg.py:44` ·
  `deploy/helm/chemclaw/values.yaml:385` · `deploy/helm/chemclaw/values.yaml:577-581`
- **Trigger**: `helm install` the shipped chart.
- **Consequence**: a control that reads as existing.
  - `entra_sa_token_path` — **1** occurrence in `src/` (its own definition).
  - `entra_token_refresh_leeway_seconds` — **1** occurrence (its own definition).
  - `entra_token_endpoint` — **2** occurrences: its definition, and a *comment* in
    `core/logging.py:446` naming it as an example of what secret-redaction must not match. Zero code
    readers. The chart nevertheless ships it into every pod's ConfigMap as
    `https://login.microsoftonline.com/TENANT/oauth2/v2.0/token` — a literal `TENANT` placeholder for
    a token exchange no code performs.
  - `structure_render_size_px` — **1** occurrence. `render_structure` is a tool in the `chem`
    connector manifest, i.e. it now runs in `Chemclaw3-mcp`, in a different process that cannot read
    this `Settings` object. The knob is a control for a capability this repo no longer holds.

  Alongside them, `deploy/helm/chemclaw/values.yaml:577-581` annotates the ServiceAccount
  `azure.workload.identity/client-id` under the header *"Workload Identity Federation: the
  ServiceAccount is annotated so the pod's projected SA token can be exchanged for an Entra token
  (F4-T2)"*. No code performs that exchange; the annotation is a claim about a mechanism that is not
  in the tree.

  The config comment above the three Entra fields asserts *"These three survive it because they
  describe the tenant rather than the deleted mechanism"*. That is true of `entra_token_endpoint` and
  false of the other two: a path to a projected ServiceAccount JWT inside a pod, and a refresh leeway,
  are parameters *of the deleted mechanism*, not descriptions of a tenant.
- **Evidence**:
  ```
  entra_sa_token_path                 src/ occurrences: core/config/entra.py:1
  entra_token_refresh_leeway_seconds  src/ occurrences: core/config/entra.py:1
  entra_token_endpoint                src/ occurrences: core/logging.py:1 (comment) core/config/entra.py:1
  structure_render_size_px            src/ occurrences: core/config/kg.py:1
  ```
  (`rg -w -c <name> src/`; `structure_render_size_px` also has zero occurrences in `tests/`,
  `deploy/`, `infra/`.)
- **Fix**: delete `entra_sa_token_path` and `entra_token_refresh_leeway_seconds` and
  `structure_render_size_px`; delete `CHEMCLAW_ENTRA_TOKEN_ENDPOINT` from `values.yaml` (or the field
  too, since nothing reads it); drop the WIF annotation and its header, or restore the mechanism.
  `extra="forbid"` does not bite on env vars (see #6), so removing a field cannot break a deployment
  that still sets it.

---

## 6. `extra="forbid"` protects the `.env` path and not the ConfigMap path — a typo'd production setting boots green

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/__init__.py:143-149` (`model_config`) and its class docstring at `:141`
- **Trigger**: set `CHEMCLAW_ENTRA_REQUIRE=true` (one letter short) in the process environment.
- **Consequence**: the process boots, `entra_required` stays `False`, and nothing says anything —
  every request runs as the shared dev principal with all authorization gates open, which is the
  posture `api/middleware._refuse_unauthenticated_exposure` exists to prevent (it only refuses when
  the *host* is non-loopback, so a Route-fronted deployment binding a Service IP is not covered by
  every combination). The same typo in a local `.env` is a hard startup error.

  The class docstring says the `model_config` "(prefix, `.env`, `extra="forbid"`)" *"governs them
  all"*, which reads as uniform coverage. It is not: pydantic-settings' env source only yields keys it
  recognises, so unknown `CHEMCLAW_*` process env vars are dropped before `extra` is evaluated. The
  chart's own keys are covered by `tests/test_helm_chart.py:193`, so this bites operator overrides
  (`--set config.CHEMCLAW_...`, an ExternalSecret, a hand-edited ConfigMap) rather than the shipped
  values.
- **Evidence**:
  ```
  $ CHEMCLAW_SERVICE_MAX_CONCURENT_TURNS=999 CHEMCLAW_ENTRA_REQUIRE=true uv run python -c '...'
  booted fine; entra_required= False turns= 8

  $ uv run python -c "Settings(_env_file='/tmp/envtest/.env')"   # file holds CHEMCLAW_TOTALLY_BOGUS=1
  env-file extra REJECTED: 1 validation error for Settings
  chemclaw_totally_bogus  Extra inputs are not permitted
  ```
- **Fix**: add a `model_validator(mode="before")` (or a startup check in `core/logging.configure_logging`,
  which every entrypoint already calls) that scans `os.environ` for `CHEMCLAW_*` keys whose lowercased
  suffix is not in `Settings.model_fields` and raises naming them. That makes the ConfigMap path as
  strict as the `.env` path, which is what the docstring already claims.

---

## 7. `values.yaml` declares `CHEMCLAW_CALC_SERVER_URL` twice, and states a pooled-process count the chart no longer renders

- **Severity**: low
- **Location**: `deploy/helm/chemclaw/values.yaml:335` and `:380` (duplicate key) · `:267-284`
  (`postgres.maxConnections` rationale) · `:344` (dangling cross-reference)
- **Trigger**: edit `values.yaml:335`.
- **Consequence**:
  - `CHEMCLAW_CALC_SERVER_URL` appears **twice** in the same `config:` mapping, each with its own
    multi-line justification for why the key exists. YAML takes the last; an edit to line 335 is
    silently discarded. The two values agree today, which is exactly why the duplication will not be
    noticed until they do not.
  - `postgres.maxConnections: 136` is justified as *"exactly what the shipped values produce (17
    pooled processes × the 8 below)"*. `chemclaw.pooledProcesses` renders **15**, so the product is
    120 and the stated derivation is stale by two processes. (Separately from #1, which says the true
    number is higher than either.)
  - `:344` refers to `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS below`; that key is not in `config` — the
    front door runs on the code default 8. Same class as the `CHEMCLAW_CONNECTORS_ENABLED` reference
    in #2.
- **Evidence**:
  ```
  $ uv run python /tmp/helmscan.py
  config keys: 33
    DUPLICATE KEY: CHEMCLAW_CALC_SERVER_URL 2
  $ helm template ... | grep PG_FLEET_POOLED_PROCESSES
  CHEMCLAW_PG_FLEET_POOLED_PROCESSES: "15"
  ```
  Recomputed by hand from `.Values`: 6 (HPA max) + 1 (background) + 4 (connector servers without
  `url:`) + 4 (connector workers, qm at 2 replicas) = 15.
- **Fix**: delete the first `CHEMCLAW_CALC_SERVER_URL` block, keeping the second (longer) rationale;
  add a duplicate-key assertion to `tests/test_helm_chart.py` (parse the raw text, not the loaded
  dict — `yaml.safe_load` hides it); correct the `136` derivation once #1 settles what the right
  multiplicand is; drop or fix the two dangling `config` cross-references.

---

## What I checked and found clean

- **Phantom / missing chart keys.** All 33 `config:` keys and all 10 `secrets.*` env names resolve
  against `Settings.model_fields` (or are deliberately non-`Settings` tokens read by
  `token_env` indirection). `tests/test_helm_chart.py` already asserts this in both directions.
- **The rendered chart validates.** Fed the full rendered ConfigMap (38 keys) into `Settings(_env_file=None)`
  with the environment otherwise cleared: `VALIDATES OK`. All seven cross-field guards pass.
- **Documented-vs-code defaults.** Scripted comparison of every `` `field` … (N) `` / "defaults to N"
  occurrence across `src/`, `deploy/` and the READMEs against the live `Field` defaults: **zero**
  mismatches. `entrypoint.sh`'s three `${VAR:-N}` fallbacks agree with the Python defaults and are
  pinned by `tests/test_deploy_chart.py:1093`. (That test reads `getattr(settings, field)` off the
  live singleton rather than `model_fields[...].default`, so it compares against ambient env rather
  than the default its own docstring names — worth one line to fix, not a finding.)
- **Magic numbers.** Grepped `src/` for hardcoded URLs, timeouts and model names outside
  `core/config/`. Every hit is either a dev-only CLI (`cli/live_storm.py`, `cli/connectors_dev.py` —
  the latter argues the case explicitly and correctly), a connector manifest's loopback default (the
  documented dev address the deployment overrides), or a derived Entra URL built from
  `entra_tenant_id`. No violation of "config, never magic numbers" on a deployment path.
- **`health_url` re-rooting.** Ran every shipped connector through `registry.health_url` with the real
  rendered `connector_urls`: all four in-cluster bundles and both externally hosted ones
  (`chem`, `safety`) re-root correctly (`http://chemclaw3-mcp-chem:8858/healthz`).
- **`embedding_dim` vs the second `vector(1536)` column.** `infra/sql/037_document_index.sql` is not
  covered by the startup guard, but `ingest/documents/index.require_schema_vector_width` covers it at
  both constructors, states the residual honestly, and correctly no-ops for a non-pgvector store.
  Not a finding.
- **`extra="forbid"` and non-`Settings` secrets.** Confirmed `CHEMCLAW_CHEM_TOKEN` in the environment
  does not fail `Settings()` — the secret-env indirection is safe. (The same behaviour is what makes
  #6 a finding in the other direction.)
