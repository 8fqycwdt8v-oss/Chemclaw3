# D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns — `CREATE ON SCHEMA public` is kept deliberately, and the two narrower postures are measured rather than aspired to

**Status:** accepted · **Date:** 2026-09-07 · **Builds on:**
D-2026-08-05-append-only-by-grant-not-by-contract (the grant file, and why the trail's integrity is
a privilege rather than a hash chain), D-2026-08-16-a-revoke-reaches-tables-the-grants-never-name
(which measured `owner of checkpoints: chemclaw_app` on a working deployment) ·
**Supersedes** nothing. It **makes true** one sentence of
`infra/sql/grants/app_privileges.sql` that was false.

## Context

`infra/sql/grants/app_privileges.sql:68-70` states the cost of `GRANT CREATE ON SCHEMA public`
honestly and then closes:

> The narrower posture — the runtime's own schema in a schema of its own, or a migrator-side
> `setup()` so the app never issues DDL — needs a decision and code outside this file; it is
> recorded, not silently taken here.

Wave 8 checked. `grep -rn 'narrower posture\|migrator-side' docs/` returned only the backlog row
that reported the gap, and `D-2026-08-16-a-revoke-reaches-tables-the-grants-never-name` does not
carry it either. **A comment asserting a decision is recorded when it is not is a claim that a
control exists**, which is the shape this repository has spent sweeps deleting
(`map_to_hpc_identity`, `reject_widening`, `set_current_specialist`). This ADR is what makes the
sentence true, and closing it means taking the decision rather than restating the options.

## What was measured

All four measurements are against the live PostgreSQL **16.15** this repository's `make up` runs,
with a purpose-built role and schema, dropped after.

**1. The grant file's load-bearing claim is true, re-checked independently.** Pre-creating the
tables in a numbered migration does not remove the need for `CREATE`. With every table `setup()`
would create already present, created by the owner, and the app role holding
`has_schema_privilege(..., 'CREATE') = false`:

```
A. CREATE TABLE IF NOT EXISTS on an EXISTING table: permission denied for schema w9_pre
B. full setup() against a pre-migrated schema:      InsufficientPrivilege: permission denied for schema w9_pre
```

Postgres checks the schema ACL before it checks existence. So the fourth posture somebody always
proposes — declare the eight tables in `infra/sql/086_*.sql` and drop the grant — is closed.

**2. The "runtime's own schema" posture works.** With `CREATE` revoked on `public` (and on
`PUBLIC`), a schema owned by the app role, and `ALTER ROLE … SET search_path = <schema>, public`:

```
A. CREATE in public refused: permission denied for schema public
B. AsyncPostgresSaver.setup(): OK with no CREATE on public
C. checkpoint tables land in the app's own schema
```

Upstream's migrations are unqualified (`CREATE TABLE IF NOT EXISTS checkpoints`), so they follow
`search_path`. This is feasible, and recording that it is feasible is the useful half of this ADR:
the next session does not have to re-measure it.

**3. The application issues no first-party DDL at all.** An AST/text pass over `src/` for
`CREATE TABLE|INDEX|SCHEMA|EXTENSION|FUNCTION`, `DROP TABLE|SCHEMA` and `ALTER TABLE` in SQL
literals finds nothing outside `core/migrate.py`'s own commentary. Every DDL statement a runtime
process issues comes from upstream's `AsyncPostgresSaver.setup()` and `AsyncPostgresStore.setup()`.

**4. The app is, deliberately and recently, a *coordinated* migrator.** `agent/checkpointer.py::
_setup_once` takes a polled `pg_try_advisory_lock` on a dedicated autocommit connection, falls back
to running `setup()` anyway after ten seconds, and its docstring records that a *held* wait was
tried and deadlocks — because three of `setup()`'s migrations are `CREATE INDEX CONCURRENTLY`,
which waits on any transaction holding a snapshot, including the loser's own wait. That machinery
was built to make the app a safe migrator for its own tables, not to work around a privilege
somebody wished were narrower.

## Decision

**`GRANT CREATE ON SCHEMA public` stays, and the app remains the migrator for the eight tables it
owns.** Both alternatives cost more than they remove:

**The migrator-side posture removes the DDL genuinely and replaces it with a silent failure mode.**
It requires the runtime to *decline to call* `setup()` — nothing else works, per measurement 1 —
which means a setting whose wrong value is invisible until a langgraph release adds a migration
nobody applies. The app then runs against a schema one version behind with no signal, which is
strictly worse than a privilege whose blast radius is bounded. It would also delete measurement 4's
machinery, whose deadlock avoidance was measured rather than reasoned.

**The own-schema posture does not remove the app's DDL; it relocates it.** The app still issues
`CREATE TABLE` and `CREATE INDEX CONCURRENTLY` on every process start — it just does so somewhere
else. What it buys is "not in `public`", and what it costs is a role-level `search_path` that
becomes load-bearing for every statement the application issues, including `durable/retention.py`'s
unqualified checkpoint sweep and `agent/leaver.py`'s erasure, on a system where nothing currently
depends on name resolution. An operator's `psql` as the migrator would then see a different set of
tables than the app does, which is a debugging surface nobody asked for. It would also want the
runbook's role recipe and the chart's migrate Job rewritten in the same commit, or the documented
provisioning steps stop producing a working deployment.

**What the kept privilege actually is.** The role may create objects in a shared namespace. It
still cannot read, write or drop anything it does not own: every table verb is enumerated in
`app_privileges.sql` and derived from the SQL `src/` issues by `tests/test_database_privileges.py`,
which fails in both directions, and `audit_events` stays INSERT-only. `CREATE` buys an attacker who
already holds the runtime credential nothing against the trail this file exists to protect.

**One thing does change, and it is the argument's own guard.** The decision above rests entirely on
measurement 3 — that the only DDL in a runtime process is upstream's two `setup()`s. If a
first-party module ever starts issuing DDL, the privilege stops being "what upstream's checkpointer
needs" and becomes "what this application does", which is a different decision.
`tests/test_database_privileges.py::test_the_only_ddl_a_runtime_process_issues_is_upstreams_setup`
is that guard, so the ADR's premise fails loudly instead of quietly ceasing to be true.

## Consequences

- The backlog row is deleted; the grant file's closing sentence now names this ADR, so the comment
  is true.
- **Revisit when** either half of the argument moves: upstream gains a way to apply its checkpoint
  migrations out of band with a version check the app can assert (which retires the silent-failure
  objection to the migrator-side posture), or a first-party module needs DDL (which turns the guard
  red and forces the question). Not on a date.
- The runbook's `has_schema_privilege('chemclaw_app', 'public', 'CREATE') → t` verification step is
  correct as written and stays.
- No `DEFERRED.md` row: nothing is pending. A posture that is kept deliberately is a decision, and
  a register of what is pending is not where a decision goes.
