# Adversarial re-derivation — `infra-deploy-ci--correctness.md`, lens: does it reproduce?

In scope: the three findings marked **high**. The two `low` findings were ignored per scope.
Everything below was re-derived from source against the live environment (Postgres 16.15 +
pgvector on 127.0.0.1:5432, helm v3.13.0, GNU make, bash 5). None of the reporter's scripts were
run; every script is mine and is under `/tmp/repro/`. Scratch database `vfy_split` and roles
`vfy_migrator` / `vfy_app` / `chemclaw_app` were created and **dropped** afterwards (verified: the
cluster's role list and database list are back to what they were). No source file was mutated.

---

## The fleet Postgres connection budget counts one pool per process; the front door opens two

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (and the finding *understates* the size of the gap — see below)

- **What I did**

  1. Confirmed the rendered numbers are what the finding says, from the shipped values:

     ```
     $ helm template chemclaw deploy/helm/chemclaw | grep -E 'CHEMCLAW_PG_|CHEMCLAW_SESSION_STORE|FLEET_REPLICAS'
       CHEMCLAW_PG_POOL_MAX_SIZE: "8"
       CHEMCLAW_SESSION_STORE: "postgres"
       CHEMCLAW_SERVICE_FLEET_REPLICAS: "6"
       CHEMCLAW_PG_FLEET_POOLED_PROCESSES: "15"
       CHEMCLAW_PG_FLEET_MAX_CONNECTIONS: "136"
     ```

     So the template computes **15**, while the prose at `values.yaml:271` says the 136 comes from
     "17 pooled processes × the 8 below" (17 × 8 = 136). The comment is stale, exactly as claimed.

  2. Confirmed the startup check passes at that shape and what it counts:

     ```
     $ CHEMCLAW_PG_FLEET_POOLED_PROCESSES=15 CHEMCLAW_PG_FLEET_MAX_CONNECTIONS=136 \
       CHEMCLAW_PG_POOL_MAX_SIZE=8 CHEMCLAW_PG_POOL_MIN_SIZE=0 uv run python -c '...Settings()...'
     validated ok; opened counted = 120 ceiling 136
     ```

  3. **Measured the real per-process count** against the live database (`/tmp/repro/pools.py`) —
     one process, `pg_pool_max_size=8`, `session_store=postgres`, entering `db.pooling()` and then
     `agent.checkpointer._checkpoint_pool()`, saturating both, counting rows in `pg_stat_activity`:

     ```
     baseline backends: 0
     after saturating shared pool: 8
     after saturating checkpointer pool too: 16
     end backends delta: 0
     ```

     **16 real Postgres backends held by one process the budget counts as 8.**

  4. Went further than the finding (`/tmp/repro/pools3.py`). `core/db._pool_for` keys its pool
     dictionary on `(dsn, options)`, and `options` embeds the statement timeout — so `/readyz`
     (`api/routes/ops.py:77-80`, `statement_timeout_seconds=service_readiness_db_timeout_seconds`)
     builds a **third** pool in every front-door process, and the k8s readiness probe hits it
     continuously. Measured in one process:

     ```
     shared pools in this process: 2
        options='-c statement_timeout=30000' max_size=8
        options='-c statement_timeout=2000'  max_size=8
     checkpointer pool max_size: 8
     summed max connections this ONE process may open: 24
     what chemclaw_pg_pool_max_size reports: 8
     ```

- **Why**

  The mechanism is exactly as filed and the cited lines are real and current:
  `_helpers.tpl` `chemclaw.pooledProcesses` counts *processes*
  (`$total = service.autoscaling.maxReplicas + workers + connectors`);
  `core/config/__init__.py:218` computes `pooled_processes × pg_pool_max_size`;
  `core/db.py:238` binds `chemclaw_pg_pool_max_size` to `settings.pg_pool_max_size` — one number
  per process regardless of how many pools that process holds — so the PrometheusRule's
  `sum(chemclaw_pg_pool_max_size)` has the identical blind spot. Both halves of the control
  under-report, confirmed by reading and by the measured 8-vs-16.

  Reachability is not in doubt: the shipped chart renders `CHEMCLAW_SESSION_STORE: "postgres"`, and
  `api/runner._turn_checkpointer()` (line 597-611) returns `await checkpointer()` on every turn when
  it is postgres, which builds the second pool. `agent/scratchpad.memory_store()` shares it, so it
  really is one extra pool per front-door process rather than two.

  The finding's arithmetic (6 front-door × 8 unaccounted = 48, real max 168 vs declared 136) holds:
  6 × 16 + 9 × 8 = 168. My `/readyz` observation makes the true front-door figure 24 per process,
  i.e. 6 × 24 + 9 × 8 = **216** against a declared 136 — so the reported 168 is a floor, not a
  ceiling. The consequence stated (pool timeouts / connect failures against an idle server, with
  `Settings` and the alert both silent) follows directly and needs no further proof.

  Nothing upstream prevents this: I verified `Settings()` validates cleanly at the rendered values,
  which is precisely the "validated in every pod" claim.

---

## `grants/app_privileges.sql` aborts in full once any table in `public` is owned by the app role

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did** — built the split-principal database from scratch, my own scaffolding throughout.

  Setup (non-superuser migrator owning the database; pgvector pre-created by the superuser because
  `CREATE EXTENSION vector` needs one — worth noting, it is a second split-principal precondition
  the chart does not mention):

  ```
  CREATE ROLE vfy_migrator LOGIN PASSWORD 'migpw' CREATEDB;
  CREATE ROLE vfy_app      LOGIN PASSWORD 'apppw';
  CREATE DATABASE vfy_split OWNER vfy_migrator;
  $ CHEMCLAW_POSTGRES_DSN=postgresql://vfy_migrator:migpw@127.0.0.1:5432/vfy_split \
      uv run python -m chemclaw.core.migrate
  applied migrations: 001_calculation_results.sql … 045_audit_tool_revision.sql
  ```

  **Clean run of the grants file succeeds** (role-name substituted to `vfy_app` here so I did not
  have to mint a cluster-wide `chemclaw_app` for this step):

  ```
  $ PGPASSWORD=migpw psql -U vfy_migrator -d vfy_split -f /tmp/repro/grants_vfy.sql
  DO
  $ PGPASSWORD=apppw psql -U vfy_app -d vfy_split -c "INSERT INTO session_messages(session_id,message) VALUES ('s','{}');"
  INSERT 0 1
  ```

  **Half 1 — without `GRANT CREATE`, the app cannot create its checkpoint tables.** Not a psql
  approximation: I ran the real `AsyncPostgresSaver.setup()` as the app role
  (`/tmp/repro/setup_nocreate.py`):

  ```
  setup FAILED: InsufficientPrivilege permission denied for schema public
  LINE 1: CREATE TABLE IF NOT EXISTS checkpoint_migrations (
  ```

  **Half 2 — with `GRANT CREATE`, `setup()` succeeds and the app owns the tables**, then the grants
  reconciliation hard-fails. Tables created by the real library, then re-owned to `chemclaw_app`
  so I could run the **shipped file through the shipped entrypoint** verbatim:

  ```
  app-owned tables: ['checkpoint_blobs','checkpoint_migrations','checkpoint_writes',
                     'checkpoints','store','store_migrations']

  $ CHEMCLAW_POSTGRES_DSN=postgresql://chemclaw_app:apppw@…/vfy_split \
    CHEMCLAW_POSTGRES_MIGRATION_DSN=postgresql://vfy_migrator:migpw@…/vfy_split \
    uv run python -m chemclaw.core.grants
  psycopg.errors.InsufficientPrivilege: permission denied for table checkpoint_migrations
  CONTEXT:  SQL statement "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM chemclaw_app"
  PL/pgSQL function inline_code_block line 14 at EXECUTE
  EXIT=1
  ```

  **Nothing in the file is applied.** Proved rather than assumed — the migrator created a fresh
  table and re-ran the file:

  ```
  $ psql -U vfy_migrator -d vfy_split -c "CREATE TABLE newtab(id int);"
  $ psql -U vfy_migrator -d vfy_split -f grants_vfy.sql
  ERROR:  permission denied for table checkpoint_migrations …
  $ psql -tAc "select has_table_privilege('vfy_app','newtab','SELECT');"
  f
  ```

  A table a migration just created ships with **no grant at all**, and the app role cannot read it.

  I also tested the one thing that could have refuted it — an upstream preventer. Making the
  migrator a *member* of the app role does fix the REVOKE:

  ```
  $ psql -U chemclaw -d vfy_split -c "GRANT chemclaw_app TO vfy_migrator;"
  $ uv run python -m chemclaw.core.grants
  applied grants: app_privileges.sql        EXIT=0
  ```

  But nothing in the chart, `values.yaml`, the Makefile or the SQL file asks an operator to do
  that, so it is a workaround, not a prevention.

- **Why**

  Reproduced end to end on the shipped file, through the shipped `python -m chemclaw.core.grants`,
  exiting 1 — which is what fails the `migrate-job.yaml` container (`… && python -m
  chemclaw.core.grants`, `helm.sh/hook: pre-install,pre-upgrade`), hence the Job, hence the
  upgrade. The cited line is real and current (`app_privileges.sql:40`), the `to_regclass` guards
  at 134-157 are real and do not cover the statement that raises, and the file's own comment at
  104-116 does describe a state the code cannot produce: it presumes the app pods create the
  LangGraph tables "as owner", and the grants file never gives the app role `CREATE ON SCHEMA
  public` — which I measured to be exactly the privilege `setup()` dies for want of.

  The finding is honest about its own precondition (an operator issuing `GRANT CREATE` by hand),
  and this is opt-in configuration (`secrets.migrationKeys.postgresMigrationDsn` is unset by
  default, so the shipped path is single-principal and unaffected). That keeps it out of
  *critical*. It stays **high** because the "no third way" claim is the real content and I
  confirmed both branches: without the grant the front door cannot take a single turn, with it the
  next `helm upgrade` fails in `pre-upgrade`. The split-principal deployment is unusable as
  shipped.

---

## `up.sh down` SIGTERMs its own process group and never stops this repo's stack

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did** — three experiments, all against the shipped `up.sh` with `CHEMCLAW_LIVE_DIR`
  pointed at `/tmp/repro/live` so `processes.sh`'s run directory was isolated from other agents.

  **(a) The mechanism is real when up and down genuinely share a process group.** A non-interactive
  wrapper that starts children with `nohup … &` exactly as `start()` does, then calls the shipped
  `bash up.sh down` in the same group:

  ```
  wrapper pid=10856 pgid=10856
    props  pid=10861 pgid=10856
    ui-bff pid=10865 pgid=10856
  --- invoking the shipped down verb ---
  [e2e] stopping Chemclaw3_ui
  Terminated
  OUTER EXIT=143
  --- pidfiles left behind ---
  props.pid
  ui-bff.pid
  ```

  Exit 143 = SIGTERM; both pidfiles survive (`rm -f` never ran) and the `processes.sh down` line
  never printed. So far the finding holds.

  **(b) The trigger clause "a `down` invoked from the shell that ran `up`" is FALSE.** An
  interactive shell has job control on, which puts `bash up.sh up` in its *own* process group, and
  puts the later `bash up.sh down` in a *different* one. Emulated with `set -m` (the same mechanism
  an interactive shell uses):

  ```
  outer(interactive-like) pid=16442 pgid=16442
  up.sh-shaped script      pid=16446 pgid=16446
    props.pid  pid=16450 pgid=16446
    ui-bff.pid pid=16451 pgid=16446
  --- now the down verb, same outer shell ---
  [e2e] props stopped (pid 16450)
  [e2e] ui-bff stopped (pid 16451)
  [e2e] stopping this repo's connectors/workers/front door
  [live] nothing running
  OUTER SURVIVED exit=0
  pidfiles left: (none)
  ```

  Correct behaviour: the group killed was the *up run's* group, the children died, the pidfiles
  were removed, and `processes.sh down` was reached.

  **(c) The cited reach path — `Makefile:308` — is immune, even inside one non-interactive
  wrapper.** GNU make puts each invocation in its own process group; measured:

  ```
  wrapper bash pgid=17108
  make recipe pgid=17821   (make itself is the group leader)
  make recipe pgid=17829   (second invocation, a different group)
  ```

  End to end with the real `up.sh down` behind a make target, both makes in one non-interactive
  `bash -c`:

  ```
  wrapper pgid=18446
  up.sh-shaped script pid=18815 pgid=18814
    props.pid  pid=18819 pgid=18814
    ui-bff.pid pid=18820 pgid=18814
  [e2e] props stopped (pid 18819)
  [e2e] ui-bff stopped (pid 18820)
  [e2e] stopping this repo's connectors/workers/front door
  [live] nothing running
  MAKE-DOWN EXIT=0
  WRAPPER SURVIVED
  pidfiles: (none)
  no sleep 500 left
  ```

  I also searched for an in-repo caller that would hit case (a): `grep -rn "up.sh down"` finds only
  `Makefile:309` and `infra/live/e2e-full-stack/README.md`. There is no CI workflow and no wrapper
  script in this repo that runs `bash up.sh up` and `bash up.sh down` in one shell.

- **Why**

  The *code reading* in the finding is right — bash disables job control in scripts, so
  `nohup … &` children inherit the launcher's PGID and `ps -o pgid= "$pid"` never returns a
  per-child group — and I reproduced the self-SIGTERM. But the two consequential claims do not
  survive:

  1. **The stated consequence on the stated reach path does not occur.** The finding cites
     `Makefile:308` as how `down()` is reached, and via make the stack *is* stopped, the pidfiles
     *are* removed, and `processes.sh down` *is* reached (exit 0, measured). The two documented
     entry points — the make targets and a shell running the script directly — are both immune,
     for two independent reasons (make's own process group; the shell's job control).

  2. **"The group it computes is never the child's own" is wrong as a general statement.** In every
     invocation where `down` is a separate process-group from the `up` run, the computed PGID is
     the up run's group, which contains exactly the children (plus a dead leader) — so the group
     kill does the thing the comment says it does, including sweeping vite's forks. I watched one
     `kill -- -PGID` take both recorded processes down in a single iteration.

  What is left is a real latent fragility: a hand-rolled non-interactive wrapper that calls
  `bash up.sh up` and `bash up.sh down` directly (bypassing make) would self-terminate, and the
  code has no defence against that. That is worth the one-line `setsid` fix the finding proposes.
  But it is a dev-only live-testing harness, no path in this repository reaches it, and the
  headline "never stops this repo's stack" is false for every way the repository offers to invoke
  it. Mechanism real, blast radius and reachability overstated — **low**.

---

### Hygiene

- Scratch database `vfy_split` and roles `vfy_migrator`, `vfy_app`, `chemclaw_app` dropped;
  `select rolname from pg_roles where rolname like 'vfy%' or rolname='chemclaw_app'` returns
  nothing, and `pg_database` is back to `postgres/chemclaw/template0/template1/temporal/
  temporal_visibility` (+ `vscratch`, which is another agent's and was left alone).
- No file under `src/`, `infra/`, `deploy/` or `Makefile` was modified — `git status --short`
  shows only untracked verdict files.
- All scripts under `/tmp/repro/`; the `.live` directory used was `/tmp/repro/live`, never the
  repository's.
