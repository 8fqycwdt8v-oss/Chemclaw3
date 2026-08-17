# infra/deploy/CI — security slice, reproduction verdicts

Scope: findings marked **critical** or **high** in
`tasks/audit-2026-08-16/findings/round1/infra-deploy-ci--security.md`. That file has exactly one:
the `db-grants` fail-open. The other five are medium/low and are out of scope.

Method note: I did not run the reporter's script or reuse the reporter's database state. I built a
throwaway database (`auditrepro`), migrated it with `chemclaw.core.migrate`, created my own roles
with names of my choosing, and measured privileges out of `information_schema.table_privileges`.
Every artefact I created has been dropped; no source file was modified.

---

## `db-grants` reports success while reconciling nothing — the append-only audit grant fails open on any role not literally named `chemclaw_app`

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**:

  1. Confirmed the cited code is real and current. `infra/sql/grants/app_privileges.sql:29`
     is `app_role CONSTANT TEXT := 'chemclaw_app';` and `:31-35` is the
     `IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN RAISE NOTICE … RETURN;`
     branch — before any REVOKE or GRANT. `src/chemclaw/core/grants.py:apply_grants` executes the
     file text and returns filenames; it has no post-condition on the role.
     `deploy/helm/chemclaw/templates/migrate-job.yaml:55-59` is the
     `sh -c "… && python -m chemclaw.core.grants"` command. All three citations check out.

  2. Grepped the whole repo for the literal. It exists in exactly one executable place
     (`app_privileges.sql:29`); everywhere else it is prose (`values.yaml:541`, `.env.example:76`,
     two READMEs) plus an unrelated metric-name allowlist in `cli/validate_prose_contract.py`.
     Nothing creates the role, and nothing anywhere parses the DSN's user:
     `grep -rn "CREATE ROLE\|createuser\|rolname" infra deploy src Makefile` returns only the
     `pg_roles` check itself.

  3. Built the scenario from scratch:

     ```
     $ psql .../postgres -c "CREATE DATABASE auditrepro OWNER chemclaw;"
     $ CHEMCLAW_POSTGRES_DSN=…/auditrepro uv run python -m chemclaw.core.migrate
     applied migrations: 001_… 045_audit_tool_revision.sql

     $ psql .../auditrepro -c "CREATE ROLE repro_runtime LOGIN PASSWORD 'x';" \
                           -c "GRANT USAGE ON SCHEMA public TO repro_runtime;" \
                           -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO repro_runtime;"
     BEFORE: DELETE,INSERT,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE

     $ CHEMCLAW_POSTGRES_MIGRATION_DSN=chemclaw@…/auditrepro \
       CHEMCLAW_POSTGRES_DSN=repro_runtime@…/auditrepro \
       uv run python -m chemclaw.core.grants
     applied grants: app_privileges.sql
     exit=0
     AFTER:  DELETE,INSERT,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE
     ```

     Identical before and after — the reconciliation applied zero privileges and said it applied a
     file.

  4. Then exercised the consequence as the runtime credential:

     ```
     (as repro_runtime)
     INSERT INTO audit_events (correlation_id,actor,tool,arguments,outcome,latency_ms)
       VALUES ('c1','victim','t','{}','ok',1);        -- ok
     UPDATE audit_events SET actor='someone-else', outcome='denied' WHERE actor='victim';  -- ok
     DELETE FROM audit_events WHERE actor='someone-else';                                   -- ok
     SELECT count(*) FROM audit_events;  ->  0
     ```

  5. Ran the control, to prove my harness was not the thing failing: with a role named exactly
     `chemclaw_app` holding `GRANT ALL`, the same command narrowed it to `INSERT,SELECT` on
     `audit_events`. So the file works; the name is the whole of it.

  6. Checked the "silent in both channels" half rather than taking it. psycopg 3's notice plumbing
     (`psycopg/_connection_base.py:341-353`) dispatches only to handlers registered via
     `add_notice_handler`, and logs nothing itself when the list is empty. `grep -rn "notice"` over
     `src/chemclaw/core/` finds no registration anywhere. The `RAISE NOTICE` is therefore discarded
     by the client, not merely unlogged. `tests/test_database_privileges.py` opens no connection
     (its only Postgres references are `langgraph.checkpoint.postgres`/`langgraph.store.postgres`
     imported for table *names*), so it cannot see this either.

  Cleanup: `DROP DATABASE auditrepro`, `DROP ROLE repro_runtime, repro_member`. The pre-existing
  `chemclaw_app` role in this cluster (another agent's) was not dropped and holds `(none)` on
  `audit_events` in the `chemclaw` database, unchanged by my run.

- **Why**: it reproduces exactly as written, on the arguments stated, with the stated output
  (`applied grants: app_privileges.sql`, exit 0) and the stated consequence (audit rows rewritten
  and deleted by the runtime credential after a "successful" reconciliation). The line numbers and
  symbols are current. Nothing upstream prevents it: no component creates the role, none compares
  the DSN's user to the literal, and the one test that guards the matrix never touches a database.

  The obvious objection — "the operator was told to name it `chemclaw_app`" — is real but weaker
  than it looks, and one measurement I ran that the reporter did not makes it weaker still. Both
  prose sites say *"create a `chemclaw_app` role"*; neither says the runtime DSN must **log in as**
  that role rather than as a member of it. So I tested membership, which is the ordinary Postgres
  shape for a service account:

  ```
  CREATE ROLE repro_member LOGIN;  GRANT chemclaw_app TO repro_member;
  GRANT ALL ON ALL TABLES IN SCHEMA public TO repro_member;
  $ uv run python -m chemclaw.core.grants   ->  applied grants: app_privileges.sql
  repro_member direct privileges on audit_events: DELETE,INSERT,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE
  has_table_privilege('repro_member','audit_events','DELETE'): t
  ```

  Here the role **is** named `chemclaw_app`, the guard passes, the REVOKE/GRANT pair runs and
  narrows `chemclaw_app` correctly — and the credential that actually serves traffic still deletes
  audit rows, because `REVOKE … FROM chemclaw_app` reaches only that role's own grants. So the
  control is defeated by a deployment that followed the documented naming to the letter. That
  removes the "operator deviated from the docs" mitigation and is why I keep the reporter's `high`
  rather than dropping to medium.

  Two corrections to the finding's framing, neither of which changes the verdict:

  - The failure requires the operator's bootstrap to have granted the runtime role broadly. With no
    grants at all the application breaks on the first query, so the silent mode needs a permissive
    bootstrap — which is the common case (`GRANT ALL ON ALL TABLES`) and is what the finding
    assumes, so this is a completeness note, not a defect in the claim.
  - The proposed fix ("raise when a migration DSN distinct from `postgres_dsn` is configured but the
    named role does not exist") would not catch the membership variant above. A check that actually
    holds has to assert the *effective* privileges of the DSN's own login role after
    reconciliation — e.g. `has_table_privilege(current_user_of_runtime_dsn,'audit_events','DELETE')
    IS FALSE` — rather than assert that a role by some name exists.
