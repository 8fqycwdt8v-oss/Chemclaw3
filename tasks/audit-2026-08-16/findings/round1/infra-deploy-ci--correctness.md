# infra/sql · infra/live · deploy · .github/workflows · Makefile · pyproject.toml — CORRECTNESS

All findings below were reproduced against the live environment (Postgres 16 + pgvector on
127.0.0.1:5432, `helm` v3.13.0, bash 5). Scratch databases and roles created for the reproductions
were dropped afterwards.

---

## The fleet Postgres connection budget counts one pool per process; the front door opens two

- **Severity**: high
- **Location**: `deploy/helm/chemclaw/templates/_helpers.tpl:497-514` (`chemclaw.pooledProcesses`),
  `deploy/helm/chemclaw/templates/config.yaml:43` (`CHEMCLAW_PG_FLEET_POOLED_PROCESSES`),
  `deploy/helm/chemclaw/values.yaml:262-286` (`postgres.maxConnections: 136`),
  `src/chemclaw/core/config/__init__.py:217-225` (the startup check),
  `src/chemclaw/agent/checkpointer.py:367-372` (the second pool)
- **Trigger**: any install of the shipped chart with `config.CHEMCLAW_SESSION_STORE: "postgres"`
  (the shipped value) once the front-door HPA reaches `maxReplicas: 6` and every replica has taken
  at least one turn.
- **Consequence**: the deployment can open **168** Postgres connections against a declared and
  provisioned ceiling of **136**, and neither the startup check nor the runtime alert can see it.
  The overflow surfaces as `pg_pool_timeout_seconds` expiries / connect failures against a server
  that is not busy — D-119's exact symptom — while `Settings` validated cleanly in every pod and
  `ChemclawFleetAboveItsConnectionCeiling` stays silent, because both compare
  `pooled_processes × pg_pool_max_size` (120) or `sum(chemclaw_pg_pool_max_size)` (15 pods × 8 =
  120) to 136.
- **Evidence**:

  `chemclaw.pooledProcesses` counts *processes*, one pool each:

  ```
  {{- $total := .Values.service.replicas | int -}}
  {{- if .Values.service.autoscaling.enabled -}}
  {{- $total = .Values.service.autoscaling.maxReplicas | int -}}
  ...
  ```

  and `core/config/__init__.py:218` multiplies it by one pool size:

  ```python
  opened = self.pg_fleet_pooled_processes * self.pg_pool_max_size
  ```

  But every front-door process opens a **second, independent** pool of the same width
  (`agent/checkpointer.py:367`):

  ```python
  pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
      conninfo=_session_dsn(),
      kwargs={"autocommit": True, "connect_timeout": settings.pg_connect_timeout_seconds},
      min_size=0,
      max_size=settings.pg_pool_max_size,
      open=False,
  )
  ```

  `api/app.py:154` enters `db.pooling()` (pool #1) and `api/runner.py:597-611` awaits
  `checkpointer()` per turn, which builds pool #2 whenever `session_store == "postgres"`.
  `agent/scratchpad.py:151-153` shares that same second pool, so it is one extra pool per process,
  not two.

  Measured render of the shipped values:

  ```
  $ helm template chemclaw deploy/helm/chemclaw | grep CHEMCLAW_PG_FLEET
    CHEMCLAW_PG_FLEET_POOLED_PROCESSES: "15"
    CHEMCLAW_PG_FLEET_MAX_CONNECTIONS: "136"
  ```

  15 shared pools × 8 = 120 accounted for. The unaccounted half is 6 front-door replicas ×
  `max_size=8` = 48, for a real maximum of 168.

  The same render also falsifies the arithmetic `values.yaml:271` states in prose: *"136 is exactly
  what the shipped values produce (17 pooled processes × the 8 below)"*. The template computes
  **15**, not 17 — `chem` and `safety` moved to an external `url:` and stopped rendering server
  pods, and the comment was not re-derived. So the declared ceiling is a stale number that happens
  to sit between the counted total (120) and the real one (168).

  `core/db.py:239` binds `chemclaw_pg_pool_max_size` to `settings.pg_pool_max_size` once per
  process, so `sum(chemclaw_pg_pool_max_size)` — the runtime half of the control, alerted on by
  `ChemclawFleetAboveItsConnectionCeiling` — reports 120 as well. Both halves of the control share
  the same blind spot.
- **Fix**: make the count a count of *pools*, not processes. Either add a front-door term to
  `chemclaw.pooledProcesses` (the front-door replica count is already computed there, so
  `$total = add $total $frontDoor` a second time when `config.CHEMCLAW_SESSION_STORE` is
  `postgres`), or — better, because it survives a topology this chart does not render — bind
  `chemclaw_pg_pool_max_size` to the *sum of the process's open pools* rather than to
  `settings.pg_pool_max_size`, and have `Settings` validate against a `pg_pools_per_process`
  factor. Re-derive `postgres.maxConnections` from whichever number results and delete the
  hand-written "17" from the comment.

---

## `grants/app_privileges.sql` aborts in full once any table in `public` is owned by the app role

- **Severity**: high
- **Location**: `infra/sql/grants/app_privileges.sql:40` (`REVOKE ALL ON ALL TABLES IN SCHEMA
  public FROM %I`), reached from `deploy/helm/chemclaw/templates/migrate-job.yaml:55-59` and
  `Makefile:93-97`
- **Trigger**: a split-principal deployment (`secrets.migrationKeys.postgresMigrationDsn` set, per
  `values.yaml:534-546`) in which the reconciling principal is the schema owner but **not** a
  superuser, and the `chemclaw_app` role owns at least one table in `public` — which is exactly
  what `AsyncPostgresSaver.setup()` / `AsyncPostgresStore.setup()` produce for `checkpoints`,
  `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`, `store`, `store_migrations`
  the first time a pod takes a turn.
- **Consequence**: `python -m chemclaw.core.grants` fails deterministically. **No grant in the file
  is applied at all** — not the append-only `audit_events` INSERT, not the DML for any table a new
  migration just created. In the chart the failure is worse than a missing grant: the migrate hook
  is a single container running `migrate && message_migration && grants`, so the third command's
  non-zero status fails the Job, all four `backoffLimit` attempts fail identically, and the
  `pre-upgrade` hook failure fails the whole `helm upgrade`, leaving the release in
  `pending-upgrade`.
- **Evidence**: reproduced end to end.

  Setup — a non-superuser migrator owning the schema, migrations applied, then the app role creates
  the checkpoint table exactly as `setup()` does:

  ```
  $ psql -U chemclaw -d postgres -c "CREATE ROLE chemclaw_migrator LOGIN PASSWORD 'migpw' CREATEDB;" \
                                  -c "CREATE DATABASE splitscratch OWNER chemclaw_migrator;"
  $ CHEMCLAW_POSTGRES_MIGRATION_DSN=postgresql://chemclaw_migrator:migpw@127.0.0.1:5432/splitscratch \
    uv run python -m chemclaw.core.migrate
  applied migrations: 001_… … 045_audit_tool_revision.sql
  $ PGPASSWORD=apppw psql -U chemclaw_app -d splitscratch \
      -c "CREATE TABLE checkpoints (thread_id text primary key, v int);"
  CREATE TABLE
  ```

  The reconciliation — the second `helm upgrade` — then dies on its **first** statement:

  ```
  $ PGPASSWORD=migpw psql -U chemclaw_migrator -d splitscratch -f infra/sql/grants/app_privileges.sql
  psql:infra/sql/grants/app_privileges.sql:168: ERROR:  permission denied for table checkpoints
  CONTEXT:  SQL statement "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM chemclaw_app"
  PL/pgSQL function inline_code_block line 14 at EXECUTE
  ```

  This is the failure mode the file's own comment (lines 125-130) says it defended against —
  *"a `GRANT` naming a table that does not exist raises, and a raise anywhere in this block aborts
  the whole reconciliation, so one interrupted `setup()` would leave every table in this file
  ungranted"* — defended in the wrong place. The eight `to_regclass` guards protect the GRANTs;
  the statement that actually raises is the unguarded `REVOKE ALL ON ALL TABLES`, which requires
  ownership or grant option on **every** table in the schema, including ones the app role created
  for itself.

  The precondition is currently reached only by an operator who has issued
  `GRANT CREATE ON SCHEMA public TO chemclaw_app` by hand, because the file grants USAGE and never
  CREATE. Measured on a fresh split-principal database after a clean `db-grants` run:

  ```
  $ PGPASSWORD=migpw psql -U chemclaw_migrator -d split2 -f infra/sql/grants/app_privileges.sql
  DO
  $ PGPASSWORD=apppw psql -U chemclaw_app -d split2 -c "CREATE TABLE checkpoints (…);"
  ERROR:  permission denied for schema public
  $ PGPASSWORD=apppw psql -U chemclaw_app -d split2 \
      -c "INSERT INTO session_messages (session_id,message) VALUES ('s','{}');"
  INSERT 0 1
  ```

  So the split-principal deployment fails one of two ways and there is no third: without
  `GRANT CREATE` the app role cannot create its checkpoint tables and every turn dies at its first
  checkpoint write; with `GRANT CREATE` — the obvious remedy, and the one the file's comment
  presumes ("the app pods create them lazily, **as owner**, on first turn") — the next `db-grants`
  run hard-fails and takes the `helm upgrade` with it. The comment describes a state the code
  cannot produce.

  For completeness, the single-principal path (the shipped default, `postgresMigrationDsn` unset)
  is unaffected: measured against a superuser reconciler, the file runs (`DO`), the app keeps
  `arwd` on its own `checkpoints` (`relacl = {chemclaw_app=arwd/chemclaw_app}`), `audit_events` is
  INSERT-only (`UPDATE` and `DELETE` both `permission denied`), and `schema_migrations` is
  unwritable.
- **Fix**: replace the blanket `REVOKE ALL ON ALL TABLES` with a revoke scoped to the tables this
  file goes on to grant (they are already enumerated), or filter it to tables the reconciler owns:

  ```sql
  FOR t IN SELECT c.oid::regclass FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
              AND pg_has_role(current_user, c.relowner, 'USAGE')
  LOOP EXECUTE format('REVOKE ALL ON %s FROM %I', t, app_role); END LOOP;
  ```

  and add `GRANT CREATE ON SCHEMA public TO %I` beside the existing `GRANT USAGE`, since the
  checkpointer and the store create their own tables as this role by design.

---

## `up.sh down` SIGTERMs its own process group and never stops this repo's stack

- **Severity**: high
- **Location**: `infra/live/e2e-full-stack/up.sh:229` (`down()`), reached from `Makefile:308`
  (`live-e2e-full-stack-down`)
- **Trigger**: `bash infra/live/e2e-full-stack/up.sh down` run in the same process group as the
  processes it recorded — i.e. any non-interactive wrapper or CI job that runs `up.sh up` and
  `up.sh down` in sequence, or a `down` invoked from the shell that ran `up`.
- **Consequence**: the script kills itself at the **first** pidfile. The `rm -f "$pidfile"` never
  runs, the remaining pidfiles are never visited, and `bash "$REPO_ROOT/infra/live/processes.sh"
  down` (line 235) is never reached — so this repo's connectors, its four Temporal workers and the
  front door are all left running after a "stop the stack" command that reported nothing. A
  subsequent `up` then sees those pidfiles live and skips restarting them, so the next run
  silently reuses processes started with the previous run's environment.
- **Evidence**: the mechanism is that bash disables job control in scripts, so `nohup … &`
  children inherit the **launcher's** process group. There is no per-child group for
  `ps -o pgid=` to return:

  ```
  $ bash pgid_repro.sh          # start() copied verbatim from up.sh:42-52
  script pid=19141  script pgid=19141
  sleeper1: pid=19147 pgid=19141
  sleeper2: pid=19148 pgid=19141
  ```

  Running `down()`'s kill line verbatim over those pidfiles:

  ```
  $ bash down_repro.sh
  Terminated
  outer exit=143
  $ ls rundir2
  props.pid
  ui-bff.pid
  ```

  Exit 143 is SIGTERM: the script terminated itself. Both pidfiles survive (`rm -f` never ran) and
  the `log "REACHED THE LINE THAT CALLS processes.sh down"` marker never printed.

  The comment above the line — *"UI dev server forks (vite + the BFF): kill the process group, not
  just the recorded pid"* — states an intent that cannot be met as written, because the group it
  computes is never the child's own. The sibling implementation in `infra/live/processes.sh:215`
  does **not** have this bug (`kill "$pid"`), so the two `down` verbs behave differently for the
  same reason they were written to behave identically.
- **Fix**: give each child its own process group at launch and kill that, e.g. in `start()`:

  ```bash
  setsid nohup "$@" >"$LIVE_DIR/e2e-$name.log" 2>&1 &
  ```

  after which `kill -- "-$pid"` is correct and needs no `ps`. If `setsid` is unacceptable, drop the
  group kill and match `processes.sh` (`kill "$pid"`), accepting that `npm run dev`'s children need
  a separate sweep.

---

## `ci.yml` states an 80 % coverage floor; the gate is 84

- **Severity**: low
- **Location**: `.github/workflows/ci.yml:95` vs `pyproject.toml:344`
- **Trigger**: reading the workflow to find out what the gate enforces.
- **Consequence**: no runtime effect — `make cov` reads `pyproject.toml`. It is a stale number in
  the file whose job is to say what CI checks, in a repository where the floor was deliberately
  moved once and this copy was not.
- **Evidence**:

  ```
  .github/workflows/ci.yml:95:  # `cov`, not `test`: the 80% floor is a regression gate, …
  pyproject.toml:344:fail_under = 84
  ```
- **Fix**: delete the number from the workflow comment and point at `[tool.coverage.report]`, so
  there is one place it is written down.

---

## `app_privileges.sql` says `schema_migrations` is absent from every grant; SELECT is granted

- **Severity**: low
- **Location**: `infra/sql/grants/app_privileges.sql:164-166` vs line 46
- **Trigger**: reading the file to establish what the runtime credential can see.
- **Consequence**: the substantive claim (the runtime role cannot *write* the ledger) holds; the
  literal claim ("deliberately absent from every GRANT above") does not, because
  `GRANT SELECT ON ALL TABLES IN SCHEMA public` at line 46 includes it. Harmless in itself, and
  worth correcting because this file is read as the authoritative statement of the matrix.
- **Evidence**: measured after running the file as a superuser reconciler against a fully migrated
  database:

  ```
  $ PGPASSWORD=apppw psql -U chemclaw_app -d auditscratch -tAc "select count(*) from schema_migrations;"
  46
  $ PGPASSWORD=apppw psql -U chemclaw_app -d auditscratch -c "INSERT INTO schema_migrations VALUES ('fake','x');"
  ERROR:  permission denied for table schema_migrations
  ```
- **Fix**: reword to "absent from every write grant", or add
  `REVOKE SELECT ON schema_migrations FROM %I` after line 46 if the read is genuinely unwanted.

---

## What I checked and found sound

Recorded so the absence of a finding is a result rather than a gap.

- **All 46 migrations apply cleanly, in order, from an empty database**, under both a superuser and
  a non-superuser owner (`applied migrations: 001_… 045_…`, run twice against fresh databases). The
  two duplicated numeric prefixes (`037_bo_suggestion_provenance` / `037_document_index`,
  `043_session_listing` / `043_session_message_shape`) sort in filename order, and in both pairs the
  order that results is the one the dependencies need — `037_document_index` creates the tables
  `038`–`041` alter, and neither `043` touches the other's columns. Re-running the runner applies
  nothing (`[]`).
- **The additive/idempotency properties hold where they are claimed.** `041_document_chunk_identity`
  cannot lose rows: the old key `(doc_id, ordinal)` is a prefix of the new
  `(doc_id, chunking_key, ordinal)`, so the `ADD PRIMARY KEY` cannot collide after the `UPDATE …
  SET chunking_key = ''`, and `DROP CONSTRAINT IF EXISTS document_chunks_pkey` makes a re-run a
  no-op rather than an error.
- **The grant matrix covers every write the code issues.** I extracted every
  `INSERT INTO`/`UPDATE`/`DELETE FROM <table>` literal from `src/` and compared it to the matrix:
  every table the application writes has the verb it uses, including the eight LangGraph tables and
  the two fingerprint tables (whose statements build the table name dynamically and so do not
  appear in a naive grep). Nothing is written that is not granted.
- **Every metric name the PrometheusRule alerts on exists in `src/`** — all 18 checked by name
  against `core/metrics.py` and the call sites; none is a typo that would make an alert silently
  un-fireable. PromQL precedence in the two compound rules
  (`ChemclawFleetAboveItsTurnCeiling`, `ChemclawFleetAboveItsConnectionCeiling`) groups as intended
  (`>` binds tighter than `and`), and the bare `sum()`/`max()` aggregations produce matching empty
  label sets, so the vector matching in the `and` is real rather than accidental.
- **The `make deps-audit` classifier is sound in both directions**: a real finding is checked before
  any excuse (`AUDIT_FOUND` first, `exit $rc` unconditionally), an unclassifiable non-zero exit
  still fails, and the output is classified from a shell variable rather than a re-read file. The
  `Found [0-9]+ known vulnerabilit` prefix matches both the singular and plural pip-audit wordings.
- **The `image.yml` smoke step's greps do not silently under-cover.** They extract
  `chemclaw.durable.background_worker`, `chemclaw.connectors.server_entry` and
  `chemclaw.api.app` from `entrypoint.sh`, guarded by `test "${#modules[@]}" -ge 2`; the
  `[ -f … ] && modules+=(…)` idiom does not trip `set -e` (verified in bash 5), so no bundle is
  dropped from the loop.
- **Prefix assignments on shell-function calls** (`CHEMCLAW_WORKER_METRICS_PORT="$port" start …` in
  `processes.sh:99`, `CHEMCLAW_PROPS_TOKEN=… start_props` in `up.sh:80`) do **not** leak into the
  shell after the call in bash 5 — verified — so later processes started by the same script do not
  inherit a previous worker's metrics port.
- `knowledge-sync.sh`'s destructive paths are correctly fenced: `rsync` and `flock` are both checked
  by name and their absence returns 1 rather than falling back to a delete, `publish` failing
  propagates through `publish_under_submit_lock` → `refresh` → a failed `once` (so an init container
  fails rather than serving a half-published tree), and `seed_from_image` refuses to overwrite a
  populated publish directory.
