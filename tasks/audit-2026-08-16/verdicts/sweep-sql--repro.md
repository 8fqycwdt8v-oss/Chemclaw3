# Adversarial re-derivation of `findings/round1/sweep-sql.md` — lens: does it actually reproduce?

Scope: the two findings marked **critical** and **high**. Medium findings ignored per brief.

Everything below was re-derived from source on a database I built myself (`verifysql`, dropped
afterwards, along with the `chemclaw_app` role I created). I did not run the reporter's scripts and
did not read their transcripts as evidence — only their claims, to know what to attack.

---

## A split-principal deployment cannot take a single turn: the runtime role has no CREATE on schema `public`

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (I would step it down one notch from critical; see the last
  paragraph — the mechanism is total but the blast radius is one opt-in deployment mode and the
  failure is loud and immediate, not silent)

- **What I did**

  Built the split principal from scratch on the live PG 16.15 container, following only what the
  repository itself does — no borrowed scaffolding:

  ```
  $ docker exec infra-postgres-1 psql -U chemclaw -d postgres \
      -c "CREATE DATABASE verifysql OWNER chemclaw;" \
      -c "CREATE ROLE chemclaw_app LOGIN PASSWORD 'apppw';" \
      -c "GRANT CONNECT ON DATABASE verifysql TO chemclaw_app;"
  ```

  The default `public` ACL on a fresh database, before anything of ours runs:

  ```
  $ docker exec infra-postgres-1 psql -U chemclaw -d verifysql -c "\dn+ public"
   public | pg_database_owner | pg_database_owner=UC/pg_database_owner+
          |                   | =U/pg_database_owner
  ```

  PUBLIC has `U` and not `C`. Then the two commands the Helm migrate Job runs
  (`migrate-job.yaml:58-59` is literally `python -m chemclaw.core.migrate && python -m
  chemclaw.agent.message_migration && python -m chemclaw.core.grants`), as the **owner**:

  ```
  $ CHEMCLAW_POSTGRES_DSN=postgresql://chemclaw:chemclaw@localhost:5432/verifysql \
      uv run python -m chemclaw.core.migrate
  applied migrations: 001_… 045_audit_tool_revision.sql        (46 files)

  $ … uv run python -m chemclaw.core.grants
  applied grants: app_privileges.sql
  ```

  The ACL after the grants file has run, plus a direct privilege probe:

  ```
  $ docker exec infra-postgres-1 psql -U chemclaw -d verifysql \
      -c "\dn+ public" \
      -c "SELECT has_schema_privilege('chemclaw_app','public','CREATE') AS can_create,
                 has_schema_privilege('chemclaw_app','public','USAGE')  AS can_use;"
   public | pg_database_owner | pg_database_owner=UC/pg_database_owner+
          |                   | =U/pg_database_owner                  +
          |                   | chemclaw_app=U/pg_database_owner

   can_create | can_use
  ------------+---------
   f          | t
  ```

  `can_create = f`. Then my own three-line driver (`/tmp/v_ckpt.py`) calling the production
  entry point as the runtime role:

  ```
  $ CHEMCLAW_SESSION_STORE=postgres \
    CHEMCLAW_POSTGRES_DSN=postgresql://chemclaw_app:apppw@localhost:5432/verifysql \
    uv run python /tmp/v_ckpt.py
  FAILED: InsufficientPrivilege permission denied for schema public
  LINE 1: CREATE TABLE IF NOT EXISTS checkpoint_migrations (
  ```

  And the memory store (`/tmp/v_store.py`, calling `chemclaw.agent.scratchpad.memory_store()`):

  ```
  FAILED: InsufficientPrivilege permission denied for schema public
  LINE 2:                 CREATE TABLE IF NOT EXISTS store_migrations ...
  ```

  I then checked the three ways this could have been already-prevented upstream, and none of them
  are:

  1. **Nothing anywhere issues the missing grant.** `grep -rn "GRANT CREATE\|CREATE ON SCHEMA"` over
     the whole repository returns nothing — the only hits are inside the findings file being
     verified.
  2. **The migrate Job does not create the library tables.** `migrate-job.yaml` runs migrate →
     message_migration → grants; no `setup()` of either `AsyncPostgresSaver` or
     `AsyncPostgresStore`. So the four `to_regclass`-guarded blocks in `app_privileges.sql` guard
     tables nothing in the split-principal path can ever create.
  3. **The failure is not caught.** `checkpointer()` (`agent/checkpointer.py:321-348`) awaits
     `saver.setup()` with no handler, and `api/runner.py:282` awaits `_turn_checkpointer()`
     unconditionally inside the per-turn `graph_factory(...)` call whenever
     `settings.session_store == "postgres"` (`runner.py:597-611`). There is no fallback branch.

  The test-blindness claim also holds: `tests/test_database_privileges.py` parses
  `CREATE TABLE IF NOT EXISTS (\w+)` out of upstream's `MIGRATIONS` only to harvest *table names*
  (`_upstream_tables`, `_tables`), and `verbs_the_grant_allows` explicitly discards anything that is
  not `INSERT|UPDATE|DELETE` (`if not granted <= {"INSERT","UPDATE","DELETE"}: continue`). Schema-level
  CREATE is outside its universe of discourse.

- **Why**

  Reproduces exactly, first try, on my own scaffolding, and the cited symbols and line numbers are
  real and current. The stated consequence is understated if anything: it is not only the
  checkpointer — the memory store fails on the same schema privilege, so a split-principal
  deployment cannot take a turn *and* cannot enable agent memory. The long present-tense comment in
  `app_privileges.sql` ("the app pods create them lazily, **as owner**, on first turn") is false
  under the exact deployment the file exists for; on PG15+ the app role is not the database owner
  and has no CREATE, and the "second `helm upgrade` outage" the same comment describes is
  unreachable because there is nothing for the REVOKE to reach. The operational instructions in
  `docs/guides/runbook.md` ("Create a login role the application runs as, **owning nothing**") lead
  an operator straight into this — following them produces a deployment that fails on turn one.

  I step severity down to **high** rather than critical only on blast radius and detectability: the
  split principal is opt-in (`postgres_migration_dsn` falls back to `postgres_dsn`, so every dev,
  CI and single-principal deployment is unaffected — I re-read `migration_dsn()` and
  `_session_dsn()` to confirm the fallback is real), and the failure is a hard, immediate,
  well-named `InsufficientPrivilege` on the first turn rather than silent corruption. Within that
  mode the consequence is exactly as filed: total, permanent, no workaround short of an operator
  hand-issuing `GRANT CREATE ON SCHEMA public` that no file in this repository knows about. The
  proposed fix (do both `setup()` calls from the migrator, before `grants`) is the right shape and
  has the side benefit the reporter names: it makes the four guarded grants land on the first
  deploy instead of never.

---

## `erase_actor` deletes other people's tool results: the content-addressed blob cascade is not identity-scoped

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Wrote my own repro (`/tmp/v_leaver.py`, `/tmp/v_leaver2.py`) against a freshly migrated
  `verifysql`, seeding through plain SQL and then through the *production* write path, and reading
  back through the *production* read path rather than by counting rows.

  First, that the blob really is shared by construction — via `api.tool_results.store_tool_result`,
  not a hand-crafted row (`/tmp/v_share.py`):

  ```
  same ref: True
  blobs: 1
  links: 2
  ```

  Then the erasure, with the bystander's fetch checked through `load_tool_result` /
  `fetchable_refs`, i.e. what `GET /sessions/{id}/tool-results/{ref}` actually calls:

  ```
  $ CHEMCLAW_SESSION_STORE=postgres CHEMCLAW_POSTGRES_DSN=…/verifysql \
      uv run python /tmp/v_leaver2.py
  bob before: True  refs: True
  report.erased: {'tool_result_blobs': 1, 'session_owners': 1}
  bob after : False refs: []
  owners: [('s-bob', 'bob-oid')]
  ```

  Bob's session, ownership row and transcript survive; Bob's tool result is gone and unfetchable;
  the report names one blob and says nothing about a second session. Same result with the plain-SQL
  seeding:

  ```
  before links: [('s-alice',), ('s-bob',)]
  erased: {'tool_result_blobs': 1, 'session_owners': 1}
  after links: []
  blobs remaining: 0
  owners remaining: [('s-bob', 'bob-oid')]
  ```

  Mechanism verified in the source rather than inferred: `_ERASE`'s first entry
  (`agent/leaver.py:176-181`) is `DELETE FROM tool_result_blobs WHERE content_hash IN (SELECT
  content_hash FROM tool_result_links WHERE session_id IN (<_SESSION_SCOPED>))` — the *selection* is
  session-scoped, the *deletion* is on a table whose primary key is the content hash alone
  (`infra/sql/042_tool_result_store.sql`: `content_hash TEXT PRIMARY KEY`), and
  `tool_result_links.content_hash … REFERENCES tool_result_blobs (content_hash) ON DELETE CASCADE`
  carries the collateral.

  Test-blindness verified by running, not by reading: `tool_result_blobs` appears in
  `tests/test_leaver.py` in exactly one place (line 434) inside
  `test_the_erase_statements_are_valid_sql`, which erases `oid-nobody-at-all` and asserts
  `erased_total == 0`. And the suite is green while the defect exists, with the Postgres tests
  actually running rather than skipping:

  ```
  $ CHEMCLAW_POSTGRES_DSN=postgresql://chemclaw:chemclaw@localhost:5432/chemclaw \
      uv run pytest tests/test_leaver.py -q -rs
  14 passed in 5.27s          (no skips reported)
  ```

- **Why**

  Reproduces cleanly on scaffolding I wrote, through the real read and write APIs, and the cited
  code does exactly what the finding says on the arguments it says. Two things I would correct in
  the write-up, neither of which changes the verdict:

  1. **The specific supporting quote is stale, though the argument it supports survives.** The
     finding asserts (from `api/tool_results.py:67-69`) that *"`include_detailed_errors` is off, so
     **every** unexpected tool exception in the whole system returns the string `"Error: Function
     failed."`"*. That string is no longer produced anywhere — `grep -rn "Error: Function failed"
     src/` returns only comments and docstrings, all of them describing the *removed* framework's
     behaviour, and `include_detailed_errors` is not a setting in this codebase at all (two prose
     mentions, zero definitions, zero readers). The comment in `api/tool_results.py` is itself out
     of date. But the property it was asserting is intact by a different route:
     `agent/tool_authz.py:196-210`'s `unexpected_error_result()` returns a *constant* string for
     every unclassified tool exception, funnelled through `_refusal_message` at line 335 — so there
     is still one deployment-wide blob for failed calls, plus every byte-identical deterministic
     tool answer. I verified the sharing empirically above using that exact constant text. The
     collision is not hypothetical.

  2. **"A partial erasure that looks complete" is the wrong name for it.** Alice's erasure is
     *complete*; the defect is over-deletion into a bystander, not under-deletion. (The reporter's
     own proposed fix inverts this: once blobs survive while another link references them, Alice's
     bytes do remain in `tool_result_blobs` — unattributable, but present. That trade is worth
     stating explicitly in whatever change lands, and the reporter's "report both counts separately"
     is the right instrument for it.)

  One thing the reporter under-weights, which makes it worse rather than better: the constant
  error-result blob is shared across the **entire deployment**, not across two sessions. A single
  offboarding of any person whose turn ever hit an unhandled tool exception deletes that one blob
  and cascades away *every* session's link to it, for every user, permanently. So the realistic
  blast radius of one `erase_actor` run is not "one bystander" but "every conversation in the
  system that ever recorded a failed tool call". That is what carries this to high on its own.

  What holds severity at high rather than critical: what is destroyed is a *trace rendering*. The
  transcript's 200-character `preview` survives in `session_events`, `calculation_results` and
  `job_records` are untouched, and nothing here is a result of record or a recomputation cost
  (042's own framing, which I checked against the schema rather than taking on trust — the blob
  table holds only `content_hash/byte_size/data/created_at` and is swept on a plain age cutoff).
  There is no confidentiality breach: nobody gains access to anything. It is silent, cross-tenant,
  irreversible data loss of a low-value artifact, triggered by a privileged and rare operation.
