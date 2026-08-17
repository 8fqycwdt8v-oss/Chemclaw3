# Verification: `sweep-config.md` — lens "does it actually reproduce?"

In scope: findings **#1** and **#2** (the only `critical`/`high` entries). #3–#7 are medium/low and
were not verified.

All scripts below are my own (`/tmp/vf/*.py`), written from the source; the reporter's `/tmp/poolcount.py`,
`/tmp/disabled.py` and `/tmp/helmscan.py` were not read or run. Postgres was already up
(`pg_isready` → `localhost:5432 - accepting connections`), Docker daemon running.

---

## 1. The fleet connection guard counts one pool per process; a front-door process opens two

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (and the true multiplier is worse than the finding says — see below)
- **What I did**

  **(a) Two pools in one live process.** `/tmp/vf/repro1.py` sets `CHEMCLAW_SESSION_STORE=postgres`,
  enters `core.db.pooling()`, does one real `SELECT 1` through `db.connection()` (the request path),
  then calls `api.runner._turn_checkpointer()` — the exact function the runner awaits at
  `api/runner.py:282` — against the live database:

  ```
  session_store = postgres
  pg_pool_max_size = 16
  checkpointer type: SchemaStampedSaver
  core.db._POOLS: 1 [16]
  checkpointer _pool is in _POOLS: False
  checkpointer pool max_size: 16 timeout: 30.0 max_idle: 600.0
  core pool timeout: 10.0 max_idle: 300.0
  pool_stats() sees: {'pool_size': 3, 'pool_available': 3, 'requests_waiting': 0}
  metric chemclaw_pg_pool_max_size would be: 16.0
  REAL per-process ceiling: 32
  ```

  My numbers match the reporter's to the digit. `pool_stats()` reports `pool_size: 3` — the core
  pool's three warm connections only; the checkpointer pool is invisible to it, so
  `chemclaw_pg_pool_size` / `_available` / `_requests_waiting` say nothing about the pool every
  turn's state write uses. Confirmed independently.

  **(b) The guard passes on the shipped chart.** `helm template chemclaw deploy/helm/chemclaw`
  (real binary, `rc=0`), then `/tmp/vf/guard.py` clears every `CHEMCLAW_*` var, loads the rendered
  ConfigMap into the environment and constructs `Settings(_env_file=None)`:

  ```
  SETTINGS BOOTED OK
  pg_pool_max_size 8
  pg_fleet_pooled_processes 15
  pg_fleet_max_connections 136
  guard computed: 120
  session_store: postgres
  ```

  **(c) The pod census.** From my own rendered output: HPA `minReplicas: 2 / maxReplicas: 6`;
  Deployments `connector-{bo,calc,molfp,rxnfp}` (server, 1 each), `connector-worker-{bo,calc}` (1 each),
  `connector-worker-qm` (`replicas: 2`), `background-worker` (1), `service`. 6+1+4+4 = 15, matching
  `chemclaw.pooledProcesses`. Only the front door reaches the checkpointer: the only callers of
  `checkpointer()` are `api/runner.py:611` and `cli/chat.py:183` (via `process_checkpointer`), and
  `agent/scratchpad.memory_store()` deliberately reuses `_checkpoint_pool()` rather than opening a
  third. So the real fleet ceiling is 120 + 6×8 = **168** against a declared 136, with the guard
  reporting 120 and booting.

  **(d) The runtime half.** `core/db.py:238` binds `chemclaw_pg_pool_max_size` to
  `float(settings.pg_pool_max_size)` — a constant per process, not a sum over pools. The alert
  asserted at `tests/test_deploy_chart.py:1035-1036` is
  `sum(chemclaw_pg_pool_max_size) > max(chemclaw_pg_fleet_max_connections)`, i.e. 15×8=120 vs 136 →
  never fires. Verified by reading the rendered `prometheusrule.yaml` (lines 254-255) and the test.

  **(e) Line numbers.** All current: guard at `core/config/__init__.py:217-229` (inside
  `_guards_that_the_comments_already_demand`, defined at :151); `_checkpoint_pool` at
  `agent/checkpointer.py:352-376`; `pool_stats` at `core/db.py:269`; `chemclaw.pooledProcesses` at
  `_helpers.tpl:497`.

- **Why** — Every element reproduces from scratch: the second pool exists, it is not in `_POOLS`, it
  carries its own `max_size=pg_pool_max_size`, the guard multiplies by a per-process count of one
  pool, and the metric that is supposed to catch drift is the same constant. The trigger is the
  chart's own default (`CHEMCLAW_SESSION_STORE: "postgres"` in the rendered ConfigMap) plus a turn.

  **Worse than reported.** `_pool_for` keys `_POOLS` on `(dsn, options)` and `options` embeds the
  statement timeout (`core/db.py:82-102`), so one DSN with two different statement timeouts is two
  core pools. `/tmp/vf/repro1b.py`:

  ```
  core.db._POOLS keys: ['-c statement_timeout=30000', '-c statement_timeout=2000']
  core pools: 2 [16, 16]
  ```

  `api/routes/ops.py:79` is the second timeout — `/readyz`, which every front-door pod serves on a
  kubelet schedule. A front-door process that has served one readiness probe and one turn therefore
  declares **3 × pg_pool_max_size = 24** sockets, not 8 and not 16. On the shipped chart the
  ceiling is 120 + 6×8 (readyz pool) + 6×8 (checkpointer) = **216** against 136. The finding's fix
  (b) — bind the metric to `sum(p.max_size for p in pools)` — is the one that survives this; a
  hardcoded `pools_per_turn_process` constant would be wrong again the next time a call site picks a
  distinct statement timeout.

---

## 2. `connectors.<name>.enabled: false` removes the pods and leaves the bundle loaded

- **Verdict**: CONFIRMED (one stated sub-consequence is wrong; the corrected one is worse)
- **Severity I would assign**: high
- **What I did**

  **(a) Rendered it myself.** `helm template chemclaw deploy/helm/chemclaw --set connectors.molfp.enabled=false`:

  ```
  CHEMCLAW_CONNECTOR_URLS: {"bo":…,"calc":…,"chem":…,"rxnfp":…,"safety":…}   # molfp absent
  CHEMCLAW_PG_FLEET_POOLED_PROCESSES: "14"
  grep -c connector-molfp  →  0        # no Deployment, no Service
  ```
  No `CHEMCLAW_CONNECTORS_ENABLED` anywhere in the render. `grep -rn CONNECTORS_ENABLED deploy/ src/`
  returns exactly two hits: the *comment* at `values.yaml:135` and a docstring at `kg/validate.py:130`.
  There is no such key in the `config:` block — I re-checked by scanning the whole `config:` mapping
  for `CONNECTOR`: zero matches. The values-file cross-reference is dangling as claimed.

  **(b) What the code then does.** `/tmp/vf/repro2.py` clears the environment, loads the *disabled*
  render's ConfigMap and asks the registry:

  ```
  CONNECTORS_ENABLED in ConfigMap: False
  settings.connectors_enabled = ''
  connector_urls keys = ['bo', 'calc', 'chem', 'rxnfp', 'safety']
  registry.enabled() -> ['bo', 'calc', 'chem', 'molfp', 'qm', 'rxnfp', 'safety']
  molfp endpoint url = http://127.0.0.1:8811/mcp   health_url = http://127.0.0.1:8811/healthz
  ```

  Confirmed: `connectors_enabled: str = ""` (`core/config/connectors.py:37`) means every discovered
  bundle (`registry.enabled()` lines 174-177), and `_endpoint_url` (:267) falls back to the manifest's
  loopback because `connector_urls` has no `molfp` key. `health_url` re-roots off that same fallback,
  so the probe also aims inside the pod.

  **(c) The fail-fast case, reproduced end to end.** `/tmp/vf/repro2c.py` stands up a local HTTP
  stand-in on 127.0.0.1:8899, rewrites the rendered `CHEMCLAW_CONNECTOR_URLS` so that every connector
  the chart *did* render answers `/healthz` 200 (a faithful cluster), leaves molfp with no override,
  sets `CHEMCLAW_CONNECTORS_REQUIRED=true`, and calls `health.check_connectors_at_startup()`:

  ```
  connectors unreachable at startup: molfp (ConnectError: All connection attempts failed)
  STARTUP FAILED: ConnectorsUnavailable connectors_required is set but these connectors are unreachable: molfp
  per-connector: [('bo','healthy'),('calc','healthy'),('chem','healthy'),('molfp','unreachable'),
                  ('qm','unprobed'),('rxnfp','healthy'),('safety','healthy')]
  ```

  Every pod fails startup, exactly as claimed — conditional on `connectors_required: true`, which the
  chart does not ship (`grep -rn CONNECTORS_REQUIRED deploy/` → nothing) and which the finding names
  as a condition.

  **(d) The sub-claim that does NOT hold.** "Its tools are still assembled into the model's tool
  surface and fail at call time" is false for MCP tools. `registry.open_connector_specs`
  (`registry.py:475-532`) builds tools *from a live session*, so a connector that cannot connect
  contributes none. `/tmp/vf/repro2d.py`:

  ```
  6 connector(s) did not come up for this scope and contribute no tools: bo, calc, chem, molfp, rxnfp, safety
  tools that reached the model: []
  ```
  (Nothing resolves in this sandbox, so all six fail — but that is the mechanism: unreachable ⇒ zero
  tools. molfp's `similar_molecules` / `substructure_matches` never reach the model.)

  **(e) The consequence the reporter missed, which is worse.** Durable **job** tools do not need a
  session. `registry.job_tools()` (:571-587) builds a launcher from each enabled manifest's `jobs:`
  block with no connectivity, and `agent/chemclaw_agent.py:500` adds all of them to every turn's tool
  surface. Enumerated live:

  ```
  bo   ['start_optimization_campaign']
  calc ['compute_reaction_energy','compare_solvents','scan_coordinate','sample_conformers','compute_interaction_energy']
  qm   ['compute_dft_energy']
  ```

  Disabling `calc`, `bo` or `qm` in the chart deletes the `connector-worker-<name>` Deployment while
  leaving those launchers in the model's tool surface, dispatching to
  `bundle_queue(name) == "connector-<name>"` (`connectors/queues.py:16`) — a Temporal queue with no
  poller. That is the module's own docstring's stated failure ("a job sitting forever in a queue
  nobody polls") reached through the half-wired Helm switch, and unlike the MCP half it is silent:
  the tool exists, the launch succeeds, the workflow never starts.

- **Why** — The chart derives the topology from `.Values.connectors` for the URLs, the pods and the
  pooled-process count, and does not derive the one key that decides what the *registry* loads. The
  code default `""` therefore re-enables what the chart just removed, and the fallback address is a
  loopback port inside the front door's own pod. Reproduced end to end from a clean environment
  against the real `helm` render. On shipped defaults the damage is a permanently `unreachable`
  connector in `/readyz`, a permanently non-zero `chemclaw_connectors_unhealthy`, and the
  `ChemclawConnectorsUnhealthy` warning alert (`prometheusrule.yaml:184-190`, `for: 10m`) firing
  forever — which makes the genuine dark-connector signal indistinguishable from a deliberate
  disable. With `connectors_required: true` it is a fleet-wide startup failure, and for a
  jobs-bearing bundle it is a live tool that enqueues into a dead queue. The reporter's fix (derive
  `CHEMCLAW_CONNECTORS_ENABLED` from the same `range/if $cfg.enabled` predicate) is the right one and
  closes all three.
