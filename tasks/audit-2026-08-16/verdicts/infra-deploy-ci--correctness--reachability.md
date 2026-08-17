# Adversarial verification — `infra-deploy-ci--correctness.md`, reachability lens

Three findings are marked **critical** or **high**; the other two (`ci.yml` 80 vs 84,
`schema_migrations` SELECT) are **low** and out of scope.

Environment: Postgres 16.15 (`infra-postgres-1`), helm v3.13.0, bash 5, `uv run` venv.
No source file was mutated. Scratch databases/roles (`vscratch`, `vscratch2`, `v_mig`,
`v_mig2`, `v_app2`) were dropped afterwards. **Hazard note:** the role `chemclaw_app` already
existed in the shared Postgres when I started — presumably another agent's reproduction. My setup
dropped and recreated it with password `apppw`; if a concurrent session depended on a different
password for that role, that is on me. The role is left in place, not dropped.

---

## The fleet Postgres connection budget counts one pool per process; the front door opens two

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (the finding is, if anything, *understated* — see below)

- **What I did**

  Rendered the shipped chart:

  ```
  $ helm template chemclaw deploy/helm/chemclaw | grep -E "CHEMCLAW_PG_FLEET|CHEMCLAW_SESSION_STORE|CHEMCLAW_PG_POOL_MAX"
    CHEMCLAW_PG_POOL_MAX_SIZE: "8"
    CHEMCLAW_SESSION_STORE: "postgres"
    CHEMCLAW_PG_FLEET_POOLED_PROCESSES: "15"
    CHEMCLAW_PG_FLEET_MAX_CONNECTIONS: "136"
  ```

  Counted the rendered Deployments to check the 15: front door 6 (HPA `maxReplicas`, `values.yaml:72`)
  + background worker 1 + bo(1) + bo-worker(1) + calc(1) + calc-worker(1) + molfp(1) + qm-worker(2)
  + rxnfp(1) = 15. The `values.yaml:271` prose says "17 pooled processes × the 8 below". The template
  says 15. The prose is stale, exactly as the finding states.

  Then measured what **one process** actually opens (`/tmp/poolcount.py`, real Postgres, defaults —
  `pg_pool_max_size=16` locally rather than the chart's 8):

  ```
  shared pools in this process: 2 [('postgresql://…/chemclaw', '-c statement_timeout=30000'),
                                   ('postgresql://…/chemclaw', '-c statement_timeout=2000')]
  checkpointer pool is a shared pool? False
  checkpointer pool max_size: 16
  TOTAL max connections this ONE process may open: 48
  settings.pg_pool_max_size (what the metric reports): 16
  ```

- **Why**

  Both halves of the control are confirmed blind, by reading the code that implements them:

  - `core/config/__init__.py:218` — `opened = self.pg_fleet_pooled_processes * self.pg_pool_max_size`.
    One pool per process, by construction.
  - `core/db.py:238` — `METRICS.bind_gauge("chemclaw_pg_pool_max_size", lambda: float(settings.pg_pool_max_size))`.
    A constant per process, independent of how many pools the process holds. So
    `sum(chemclaw_pg_pool_max_size)` reports 8 for a front-door pod that may hold 24.

  And the second pool is real, not incidental: `agent/checkpointer.py:352-376` builds its own
  `AsyncConnectionPool` at `max_size=settings.pg_pool_max_size` against `_session_dsn()`, which
  `agent/session_store.py:212` resolves to `settings.session_store_dsn or settings.postgres_dsn` —
  the same server unless the operator splits it, and the chart does not. It is reached per turn from
  `api/runner._turn_checkpointer()` (runner.py:597-611), gated on exactly the `session_store ==
  "postgres"` the chart ships. The module docstring says the separate pool is deliberate and gives
  three reasons; none of them is a reason for the budget arithmetic to ignore it.

  **What the finding missed, which makes it worse.** `core/db._pool_for` (db.py:136) keys the shared
  pool on `(dsn, options)`, and `options` embeds the per-call statement timeout. `api/routes/ops.py:79`
  — `/readyz` — passes `statement_timeout_seconds=settings.service_readiness_db_timeout_seconds`,
  producing a *second, distinct shared pool* of the same `max_size`. Measured above: two shared pools
  plus the checkpointer pool, three in one process. So a front-door replica at chart values may open
  **24**, not 8 and not 16; the fleet worst case is 9 non-front-door pooled processes × 8 plus
  6 × 24 = **216** against a declared 136, not 168. `/readyz` is a kubelet probe, so that third pool
  is opened on every replica within seconds of start — this is not a corner.

  Reachability is unobstructed: nothing validates the number of pools, `Settings` validates cleanly
  because it computes the wrong product, and the alert aggregates a constant. The consequence as
  stated (pool timeouts / connect failures against an idle server, invisible to both halves of the
  control) is what the code produces.

---

## `grants/app_privileges.sql` aborts in full once any table in `public` is owned by the app role

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Built the split-principal shape the finding names, on PG 16.15: `v_mig` (non-superuser, owns the
  database), role `chemclaw_app`, migrations applied as `v_mig`.

  First, a reachability check the finding only asserts — can the app role create tables in `public`
  at all under the shipped grants? Measured:

  ```
  $ psql -U chemclaw_app -d vscratch -c "CREATE TABLE checkpoints (thread_id text primary key);"
  ERROR:  permission denied for schema public
  ```

  (PG 15+ default: `public` is owned by `pg_database_owner` and CREATE is not held by PUBLIC. The
  file grants USAGE at line 42 and never CREATE.) So I then ran the real checkpointer against that
  database as `chemclaw_app`:

  ```
  SETUP FAILED: InsufficientPrivilege permission denied for schema public
  ```

  i.e. `AsyncPostgresSaver.setup()` fails and no turn can take a checkpoint. Then the other horn —
  operator issues the obvious remedy and the second `helm upgrade` re-runs the file:

  ```
  $ psql -U v_mig -d vscratch -f infra/sql/grants/app_privileges.sql          # run 1
  DO
  $ psql -U v_mig -d vscratch -c "GRANT CREATE ON SCHEMA public TO chemclaw_app;"
  GRANT
  $ psql -U chemclaw_app -d vscratch -c "CREATE TABLE IF NOT EXISTS checkpoints (…);"
  CREATE TABLE
  $ psql -U v_mig -d vscratch -v ON_ERROR_STOP=1 -f infra/sql/grants/app_privileges.sql   # run 2
  psql:infra/sql/grants/app_privileges.sql:168: ERROR:  permission denied for table checkpoints
  CONTEXT:  SQL statement "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM chemclaw_app"
  PL/pgSQL function inline_code_block line 14 at EXECUTE
  PSQL EXIT=3
  ```

  I also attacked the reachability from the most plausible escape: what if the migrator *creates*
  the app role itself (`CREATEROLE`), as the `values.yaml:534-546` note implies an operator does?
  PG 16 gives the creator ADMIN OPTION but not inherited membership, so it does not help:

  ```
  $ psql -U v_mig2 -d vscratch2 -c "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM v_app2;"
  ERROR:  permission denied for table t_own
  $ ... -tAc "select pg_has_role('v_mig2','v_app2','USAGE'), pg_has_role('v_mig2','v_app2','MEMBER');"
  f|t
  ```

  Only a **superuser** migrator escapes — and a managed Postgres (RDS/Azure Flexible Server) admin
  role is not one.

- **Why**

  Everything the finding claims reproduces, including the both-horns argument, which I re-derived
  independently rather than taking on trust. The path is: `values.yaml` ships
  `secrets.migrationKeys.postgresMigrationDsn` as a first-class opt-in; `migrate-job.yaml:55-59`
  runs `migrate && message_migration && grants` as one `sh -c` chain under
  `helm.sh/hook: pre-install,pre-upgrade`, so a non-zero exit from `chemclaw.core.grants` fails the
  Job, all `backoffLimit: 3` (= 4 attempts) fail identically, and a failed pre-upgrade hook fails the
  release. `core/grants.py` imports `migration_dsn`, so it does run as the migrator. Nothing upstream
  guards it: there is no schema check, no `to_regclass` guard on line 40 (the eight guards are all
  on the GRANTs, twenty lines further down), and no test exercises a non-superuser reconciler —
  `tests/test_database_privileges.py` does not touch a live database at all (`grep -rln chemclaw_app
  tests/` returns only `test_prose_contract.py`).

  The one thing the reporter is too generous about is the file's own comment at lines 106-112, which
  asserts "the app pods create them lazily, as owner, on first turn". Measured, they cannot: without
  a hand-issued `GRANT CREATE` that this file never emits, `setup()` dies with
  `permission denied for schema public` and every turn fails at its first checkpoint write. The
  comment describes a state the shipped SQL cannot produce, and the reporter says so — correctly.

  Severity stays high rather than critical only because the shipped default is single-principal
  (`postgresMigrationDsn` unset → `postgres_migration_dsn` falls back to `postgres_dsn`), which I
  confirmed is unaffected. But the split-principal path is the entire point of the file, and it does
  not work in either configuration.

  One incidental obstacle worth recording for whoever fixes this: a non-superuser migrator cannot run
  the migrations from an empty database at all — `001` needs `CREATE EXTENSION vector`, which is not
  trusted in this image (`ERROR: permission denied to create extension "vector" / HINT: Must be
  superuser`). The reporter's transcript shows the migrations applying cleanly as
  `chemclaw_migrator`, which means their scratch database already had the extension. It does not
  change the verdict; it does mean the split-principal path has a third undocumented superuser
  prerequisite.

---

## `up.sh down` SIGTERMs its own process group and never stops this repo's stack

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**

  Reproduced the mechanism faithfully, with `start()` copied from `up.sh:42-52`, `down()`'s kill
  line copied verbatim from `up.sh:229`, and a nested stand-in for
  `bash "$REPO_ROOT/infra/live/processes.sh" up` writing into its **own** `RUN_DIR` (the real
  scripts do use two: `up.sh:21` `$LIVE_DIR/e2e/run`, `processes.sh:29` `$LIVE_DIR/run`).

  **Case A — `up` and `down` in one non-interactive parent (the finding's trigger):**

  ```
  parent pid=27322 pgid=27322
  up.sh pid=27326 pgid=27322
    [processes.sh] api started pid=27331 pgid=27322
    [processes.sh] worker1 started pid=27336 pgid=27322
    [processes.sh] connector1 started pid=27341 pgid=27322
  props.pid pid=27346 pgid=27322
  ui-bff.pid pid=27347 pgid=27322
  down.sh pid=27363 pgid=27322
  Terminated
  parent2 exit=143
  ```

  So the self-SIGTERM is real: exit 143, the `rm -f` never runs, `processes.sh down` is never
  reached. But then I checked the thing the finding asserts and did not measure — whether this
  repo's stack survives:

  ```
  === ps of every recorded pid, from an outside shell ===
  props.pid        27346   Z    [child.sh] <defunct>
  ui-bff.pid       27347   Z    [child.sh] <defunct>
  api.pid          27331   Z    [child.sh] <defunct>
  connector1.pid   27341   Z    [child.sh] <defunct>
  worker1.pid      27336   Z    [child.sh] <defunct>
  ```

  Every one of them is dead.

  **Case B — separate invocations, each in its own process group (what a terminal gives for two
  separate `make` runs, emulated with `setsid`):**

  ```
  up.sh pid=22398 pgid=22398
  props.pid pid=22403 pgid=22398
  ui-bff.pid pid=22404 pgid=22398
  down.sh pid=22427 pgid=22426
  props stopped (pid 22403)
  ui-bff stopped (pid 22404)
  REACHED THE LINE THAT CALLS processes.sh down
  downl exit=0
  -- leftover pidfiles: (none)
  ```

  The script survives, both pidfiles are removed, and `processes.sh down` runs.

- **Why**

  The mechanism (bash disables job control in scripts, so `nohup … &` children inherit the
  launcher's pgid and `ps -o pgid=` can never return a group of the child's own) is correct and I
  grant it. Both things the finding builds on top of it fail.

  **The consequence is inverted.** The finding's headline is that "this repo's connectors, its four
  Temporal workers and the front door are all left running after a 'stop the stack' command", and
  that "a subsequent `up` then sees those pidfiles live and skips restarting them". Neither holds,
  for the same reason the self-kill happens: `infra/live/processes.sh`'s own `start()` also launches
  with a bare `nohup "$@" … &` (no subshell, no `setsid`), so every process it starts is in the
  *same* group the down script signals. `kill -- -$PGID` therefore reaches them. Measured: all five
  recorded pids defunct. `start()`'s `kill -0` guard then fails on the next `up` and everything is
  restarted, so the "silently reuses processes started with the previous run's environment" claim
  does not follow either. What is actually left behind is stale pidfiles in two directories, an
  un-run `processes.sh down` (so `$RUN_DIR/$name.port` files are not cleaned and the workers get no
  ordered drain — though they get the same SIGTERM either way), and a caller that exits 143.

  **The trigger is narrower than "any wrapper".** The only in-repo callers are `Makefile:308` /
  `Makefile:312`, two separate targets. Invoked the normal way — two `make` runs from a terminal —
  each `make` gets its own process group and Case B is what happens: the script works correctly. The
  bug needs `up` and `down` inside one non-interactive shell (`bash -c 'make live-e2e-full-stack;
  …; make live-e2e-full-stack-down'`, or a CI job). `grep -rn e2e-full-stack .github/` finds
  nothing; no such caller exists in the repository.

  So: a real defect in a local four-repo dev-lane script, with no deployment surface, no data
  consequence, a trigger that requires a caller shape nobody ships, and a failure mode that
  over-kills rather than leaks. The fix the finding proposes (`setsid` in `start()`) is the right
  one. Its severity is not high — I would file it low.
