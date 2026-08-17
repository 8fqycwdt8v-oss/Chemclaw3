# Verdicts — `sweep-config.md`, lens: reachability and consequence

In scope: findings #1 and #2 (the only `critical`/`high` entries). #3–#7 ignored per scope.

---

## 1. The fleet connection guard counts one pool per process; a front-door process opens two

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (unchanged — and the ceiling is worse than reported)

- **What I did**:

  **(a) Proved the two pools are two sets of real sockets, not two objects sharing one.**
  Started the sandbox stack (`docker ps` → `infra-postgres-1 Up (healthy)`), then ran
  `/tmp/poolmeasure.py`: it enters `core.db.pooling()`, saturates the core pool by holding
  `pg_pool_max_size` connections open, then takes `agent.checkpointer.checkpointer()` and holds
  `pg_pool_max_size` connections off *its* pool, counting real backends from an independent
  autocommit admin connection via `pg_stat_activity`.

  ```
  $ CHEMCLAW_SESSION_STORE=postgres CHEMCLAW_PG_POOL_MAX_SIZE=4 CHEMCLAW_PG_POOL_MIN_SIZE=0 \
      uv run python /tmp/poolmeasure.py
  pg_pool_max_size 4 min 0
  baseline backends (incl admin): 1
  after core saturation, backends: 5
  after checkpointer saturation, backends: 9
  checkpointer pool max_size: 4 timeout 30.0
  core.db._POOLS count: 1 sum max_size: 4
  pool_stats(): {'pool_size': 4, 'pool_available': 0, 'requests_waiting': 0}
  ```

  One process on `pg_pool_max_size=4` held **8** Postgres backends (9 minus the admin one).
  `pool_stats()` — the source of `chemclaw_pg_pool_size` / `_available` / `_requests_waiting`
  (`core/db.py:233-237`) — reported **4**, i.e. exactly half, and nothing about the checkpointer's
  four. (Note for anyone re-running this: `pg_stat_activity` is cached per transaction, so the
  admin connection must be autocommit or every read returns the first snapshot.)

  **(b) Confirmed the shipped chart's arithmetic.**

  ```
  $ helm template chemclaw deploy/helm/chemclaw | grep -E "PG_FLEET|PG_POOL_MAX|SESSION_STORE"
  CHEMCLAW_PG_FLEET_MAX_CONNECTIONS: "136"
  CHEMCLAW_PG_FLEET_POOLED_PROCESSES: "15"
  CHEMCLAW_PG_POOL_MAX_SIZE: "8"
  CHEMCLAW_SESSION_STORE: "postgres"
  ```

  15 × 8 = 120 ≤ 136 → the `_guards_that_the_comments_already_demand` branch at
  `core/config/__init__.py:217-227` passes. The reporter's per-process table is right: I re-derived
  `chemclaw.pooledProcesses` from `values.yaml` (front door HPA max 6, background 1, connector
  servers without `url:` = molfp/rxnfp/calc/bo = 4, connector workers = calc/bo/qm×2 = 4) → 15.
  Only the front door and the CLI build a checkpointer (`grep process_checkpointer` → `cli/chat.py`
  only; `checkpointer()` → `api/runner._turn_checkpointer`, awaited once per turn at
  `api/runner.py:282`), so the +48 lands exactly where the reporter puts it.

  **(c) Found the ceiling is higher still than 168.** `core.db._POOLS` is keyed by
  `(dsn, merged libpq options)`, and the options string carries the statement timeout — so the
  front door's `/readyz` probe (`api/routes/ops.py:79`, `statement_timeout_seconds=
  service_readiness_db_timeout_seconds`) opens a **third** pool, also `max_size=pg_pool_max_size`:

  ```
  $ CHEMCLAW_PG_POOL_MAX_SIZE=8 uv run python /tmp/pools3.py
  core pools: [('-c statement_timeout=30000', 8), ('-c statement_timeout=2000', 8)]
  sum max_size across core pools: 16
  ```

  A front-door pod that has served one readiness probe and one turn therefore declares
  3 × 8 = 24, not 8. Fleet theoretical ceiling: 6×24 + 9×8 = **216** against a declared 136 and a
  guard that computes 120. (The readyz pool will realistically hold 1–2 connections, so 168 is the
  right *practical* number and the reporter's table is the honest one to fix against — but the
  guard's unit is a declared ceiling, and by its own unit it is wrong by 3×, not 2×, for the front
  door.)

  **(d) Confirmed the runtime half is blind for the same reason.** `core/db.py:238` binds
  `chemclaw_pg_pool_max_size` to `settings.pg_pool_max_size` — a constant per process, not a sum
  over pools — and `prometheusrule.yaml:252-256` alerts on
  `sum(chemclaw_pg_pool_max_size) > max(chemclaw_pg_fleet_max_connections)`, i.e. 120 > 136 → never
  fires. No test constrains the pool count per process either (`grep pooledProcesses tests/` finds
  only assertions about how the *helper* is written, `tests/test_deploy_chart.py:372-1015`).

- **Why**: Both halves of the finding hold under my lens.

  *Reachability*: nothing upstream stands in the way. The trigger is the shipped chart's own
  defaults — `session_store: postgres` is rendered, `_turn_checkpointer` is on the unconditional
  per-turn path, and no validator, Helm default, or startup guard constrains how many pools a
  process may open. There is no private-function-only path here: an ordinary HTTP turn against the
  front door opens the second pool.

  *Consequence*: as stated, and slightly understated. The failure is not merely arithmetic. An
  operator who provisions Postgres for 136 because `values.yaml` calls it "a provisioning
  requirement, not a preference" gets `FATAL: sorry, too many clients already` at peak — which is
  the moment HPA has all six front doors up and both of each one's pools filling. That surfaces as
  `ConnectionError` out of `core/db.connection` (turn 500s) and as retry churn in the Temporal
  activities, i.e. a load-correlated partial outage. The two controls built to prevent exactly this
  — the startup validator and `ChemclawFleetAboveItsConnectionCeiling` — both pass while it
  happens, and there is no third signal, because `pool_stats()` cannot see the pool that every
  turn's state write goes through. A control that reports green while the condition it names is
  true is worse than no control; high is right.

  One thing the reporter's fix note gets right and is worth keeping: option (a) — registering the
  checkpointer's pool in `_POOLS` under its own options key — is not just cheaper than option (b),
  it is the only one of the two that also fixes the missing `pool_size`/`requests_waiting`
  coverage, which is the signal that would have made this visible in the first place.

---

## 2. `connectors.<name>.enabled: false` removes the pods and leaves the bundle loaded

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**: Reproduced the mechanism exactly, then checked each of the three stated
  consequences against the code that implements them.

  **The mechanism reproduces.**

  ```
  $ helm template chemclaw deploy/helm/chemclaw --set connectors.molfp.enabled=false > /tmp/dis.yaml
  $ grep -c molfp /tmp/dis.yaml        # 0 — no Deployment, no Service, not in CONNECTOR_URLS
  CONNECTOR_URLS: {"bo":...,"calc":...,"chem":...,"rxnfp":...,"safety":...}
  CONNECTORS_ENABLED present: False
  CONNECTORS_REQUIRED present: False
  ```

  Feeding that rendered ConfigMap into the process environment (`/tmp/checkdisabled.py`):

  ```
  connectors_enabled setting: ''
  connector_urls keys: ['bo', 'calc', 'chem', 'rxnfp', 'safety']
  registry.enabled(): ['bo', 'calc', 'chem', 'molfp', 'qm', 'rxnfp', 'safety']
    molfp: url -> http://127.0.0.1:8811/mcp | health -> http://127.0.0.1:8811/healthz
  ```

  `grep -rn CONNECTORS_ENABLED deploy/` returns exactly one hit — the *comment* at
  `values.yaml:135` — so the cross-reference it makes ("`CHEMCLAW_CONNECTORS_ENABLED` in `config`
  below") does point at a key that is not there. That part of the finding is accurate.

  **Consequence 1 — "Its tools are still assembled into the model's tool surface and fail at call
  time" — is false.** `registry.open_connector_specs` (`registry.py:475-532`) returns
  `[tool for tools in opened for tool in tools]`, and `opened` comes from entering each
  `HeldConnectorSession`; `load_mcp_tools` needs a **live** session, so a connector that does not
  connect contributes **zero** tools. The model never sees a molfp tool, so there is no call-time
  failure. What actually happens is announced in four places: a `logger.warning` naming the
  connector, `chemclaw_connectors_unreachable_total`, the `chemclaw_connectors_unhealthy` gauge,
  and a `CapabilityDegradedEvent` yielded to the user's own stream (`api/runner.py:265`). Nothing
  about this is silent.

  **Consequence 2 — "`/readyz` reports it `unreachable` forever" — is accurate**, and correctly
  scoped: `api/routes/ops.py:118-125` gates readiness on the database only, not on connectors, so
  the pod stays Ready and traffic is unaffected. I checked this specifically because a
  connector-gated `/readyz` would have made the finding *worse* than filed; it is not.

  **Consequence 3 — "with `connectors_required: true` … every pod fails startup" — is wrong twice.**
  (i) `check_connectors_at_startup` has exactly one caller in the whole tree — `api/app.py:155`,
  the front door's lifespan. `grep -rn check_connectors_at_startup src/` returns that line and the
  definition; no worker and no connector server calls it, so at most the *service* Deployment
  crash-loops, not "every pod". (ii) `connectors_required` is not in the rendered ConfigMap at all
  (`CONNECTORS_REQUIRED present: False` above) and the code default is `False`
  (`core/config/connectors.py:52`), so the fail-fast branch is a second, separate operator opt-in
  rather than "the documented posture" of the shipped chart.

  **What the reporter missed that cuts the other way**: `skills_dirs()`, `declared_note_types()`
  and `declared_relations()` all read `enabled()` too, so disabling a bundle that ships skills
  (`bo`, `calc`, `qm`, `safety` — I listed them: `registry.skills_dirs()` returns all four) leaves
  its judgment published into the model's prompt for a capability that is not there. molfp happens
  to ship none, which is why the reporter's own repro did not surface it.

- **Why**: The mechanism is real and the trigger is reachable — `connectors.<name>.enabled` is a
  documented, supported values switch, and an operator turning one off gets a bundle that stays
  loaded and resolves to a loopback address nothing in the pod listens on. The half-wired switch,
  the false `values.yaml` cross-reference, and the absent test pairing are all genuine and worth
  fixing, and the reporter's fix (derive the key the same way `connectorUrls` is derived) is the
  right one.

  But the severity rests on consequences that do not survive checking. There is no security or
  data impact, no outage (readiness does not gate), and — decisively for a "high" — the failure is
  not silent. It announces itself in a WARNING log, two metrics, a `/readyz` field, a purpose-built
  Prometheus alert (`ChemclawConnectorsUnhealthy`, `prometheusrule.yaml:184`), and an event on the
  chemist's own stream, every turn, forever. The residual harm is a permanently-firing warning
  alert plus a per-turn degradation notice for a capability the operator meant to remove — real
  operator-visible noise and an alert that desensitizes, but a self-reporting misconfiguration, not
  a defect that costs correctness. The two clauses that make it read as high — tools in the model's
  surface failing at call time, and every pod failing startup — are the two I could not reproduce.
  Medium.
