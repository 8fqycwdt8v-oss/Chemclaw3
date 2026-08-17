# Verdicts — `sweep-sql.md`, lens: is the trigger reachable and is the consequence what is claimed?

Scope: the one **critical** and the one **high** finding. The two **medium** findings
(`server_embed_function`, retention starvation) were not examined.

Environment: the live stack from `infra/docker-compose.yml` (`pgvector/pgvector:pg16`, PostgreSQL
16.15), a throwaway database `auditv` created for this verification and dropped afterwards, plus a
throwaway `chemclaw_app` login role, also dropped (it had to go — `app_privileges.sql` no-ops on the
role's *absence*, so leaving it on the shared dev cluster would have changed the behaviour of
`tests/test_database_privileges.py` for the next session).

---

## A split-principal deployment cannot take a single turn: the runtime role has no CREATE on schema `public`

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (down from critical — reasoning at the end)

- **What I did**

  1. Built the deployment the finding names, from scratch, following the only procedure this
     repository ships for it (`docs/guides/runbook.md` §"Splitting the database principal", read
     solely to establish *how an operator reaches this state* — its instruction is
     `CREATE ROLE chemclaw_app LOGIN PASSWORD '…';` under the heading "owning nothing", and it
     issues no `GRANT CREATE`):

     ```
     $ psql -U chemclaw -d postgres -c "CREATE DATABASE auditv OWNER chemclaw;" \
                                    -c "CREATE ROLE chemclaw_app LOGIN PASSWORD 'apppw';"
     $ psql -U chemclaw -d auditv -c '\dn+ public'
       Name  |       Owner       |           Access privileges
      -------+-------------------+----------------------------------------
       public| pg_database_owner | pg_database_owner=UC/pg_database_owner+
             |                   | =U/pg_database_owner
     ```

     PUBLIC holds `U` and not `C`, on the shipped image, exactly as reported.

  2. Ran the migrate Job's own command line as the schema owner —
     `deploy/helm/chemclaw/templates/migrate-job.yaml` runs
     `python -m chemclaw.core.migrate && python -m chemclaw.agent.message_migration && python -m
     chemclaw.core.grants`, and nothing in it touches a checkpointer:

     ```
     applied migrations: 001_… 045_audit_tool_revision.sql   (46 files)
     applied grants: app_privileges.sql
     ```

  3. Took the app pod's side of the first turn as the runtime role:

     ```
     $ CHEMCLAW_SESSION_STORE=postgres \
       CHEMCLAW_POSTGRES_DSN="postgresql://chemclaw_app:apppw@localhost:5432/auditv" \
       uv run python -c "import asyncio; from chemclaw.agent import checkpointer as ck; \
                         asyncio.run(ck.checkpointer())"

       File ".../chemclaw/agent/checkpointer.py", line 346, in checkpointer
           await saver.setup()
       File ".../langgraph/checkpoint/postgres/aio.py", line 98, in setup
           await cur.execute(self.MIGRATIONS[0])
       psycopg.errors.InsufficientPrivilege: permission denied for schema public
       LINE 1: CREATE TABLE IF NOT EXISTS checkpoint_migrations (
     ```

  4. Then went past what the finding checked, because its own **fix** depends on the answer.
     Created the four checkpointer tables as the schema owner, re-ran `chemclaw.core.grants` (which
     now reported `applied grants: app_privileges.sql` with the `to_regclass` guards firing), and
     retried as `chemclaw_app`. It fails **identically**:

     ```
     $ psql -U chemclaw_app -d auditv -c "\dt checkpoint*"
       public | checkpoint_blobs      | table | chemclaw
       public | checkpoint_migrations | table | chemclaw
       public | checkpoint_writes     | table | chemclaw
       public | checkpoints           | table | chemclaw
     $ psql -U chemclaw_app -d auditv \
         -c "CREATE TABLE IF NOT EXISTS checkpoint_migrations (v INTEGER PRIMARY KEY);"
       ERROR:  permission denied for schema public
     ```

     Postgres checks schema `CREATE` **before** the `IF NOT EXISTS` existence test, so
     pre-creating the tables from the migrator does not rescue an app-side `setup()`.

  5. Grepped the whole tree for anything that would hand the role the missing privilege:
     `grep -rn "CREATE ON SCHEMA\|GRANT CREATE\|chemclaw_app" deploy infra Makefile src tests`
     returns the grants file, two READMEs, one `values.yaml` comment and two `validate_prose_contract`
     lines. Nothing issues `GRANT CREATE ON SCHEMA public`.

- **Why**

  Reachability holds all the way out to the entry point. `_checkpoint_pool()` connects on
  `_session_dsn()` = `session_store_dsn or postgres_dsn` — the **runtime** credential — and
  `api/runner._turn_checkpointer()` awaits `checkpointer()` once per turn with no fallback
  (`if settings.session_store != "postgres": return None`, otherwise the saver, unguarded). There is
  no validator, no Helm default, no startup guard and no caller-side branch between an HTTP turn and
  that `CREATE TABLE`. The only thing standing in the way of the trigger is the operator choosing
  not to split the principal — which is opt-in, and the finding scopes itself to the deployment that
  does. The runbook's own acceptance test for the split (`INSERT INTO audit_events` succeeds,
  `DELETE` fails) passes cleanly in this exact broken state, so the documented verification does not
  catch it either.

  Consequence holds and is not a paraphrase: the exception is raised inside `checkpointer()` at
  `await saver.setup()`, propagates out of `_turn_checkpointer()` before the graph is built, and is
  not a psycopg error the erasure-style translation layer wraps — the turn dies. Same for
  `AsyncPostgresStore.setup()` in `agent/scratchpad.py` where memory is enabled. And the corollary
  the finding draws is right: with the tables never created, all eight `to_regclass` guards in
  `app_privileges.sql` are permanently false, so that whole block is dead, and the "second
  `helm upgrade` outage" its comment describes is unreachable for the same reason (it requires the
  app role to *own* those tables).

  One correction that makes the finding's fix section wrong rather than the finding wrong: step 4
  above shows the app-side `setup()` cannot merely be "kept idempotent". `CREATE TABLE IF NOT
  EXISTS` is an ACL failure regardless of whether the table is there, so the migrate-Job pre-create
  must be paired with **removing** the app-side `setup()` call (or gating it on the migration DSN).

  On severity. Everything technical reproduces, so this is not overstated — but "critical" reads as
  a defect of the shipped default, and it is not one. The default chart leaves the migration Secret
  key unset, `postgres_migration_dsn` falls back to `postgres_dsn`, and every single-principal
  deployment (`make up`, CI, dev) is unaffected: I verified the split is what breaks it, not the
  chart. It also fails loudly, on the very first turn of a fresh install, with a message naming the
  schema and the statement — an operator cannot ship past it — and there is no data loss, no silent
  corruption and no privilege bypass. High, not critical. The one thing that keeps it from dropping
  further is the *shape* of the likely field remedy: an operator debugging `permission denied for
  schema public` will reach for `GRANT CREATE ON SCHEMA public TO chemclaw_app`, which makes the app
  role the owner of the tables it creates and re-arms exactly the second-`helm upgrade` self-revoke
  the grants file warns about — so the failure pushes the deployment toward a worse state than the
  one it is in.

---

## `erase_actor` deletes other people's tool results: the content-addressed blob cascade is not identity-scoped

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium (down from high — reasoning at the end)

- **What I did**

  Reproduced the cascade against a fresh fully-migrated database (`auditv`), driving the real
  `chemclaw.agent.leaver.erase_actor`, not a hand-written DELETE (`/tmp/v_leaver.py`):

  ```
  before erase, links: [('s-alice',), ('s-bob',)]
  erased (non-zero): {'tool_result_blobs': 1, 'session_owners': 1}
  after erase, links: []
  blob rows remaining: 0
  owners remaining: [('s-bob', 'bob-oid')]
  ```

  Setup was two `session_owners` rows for two different oids, one `tool_result_blobs` row, and one
  `tool_result_links` row per session pointing at it. Bob's link is gone; Bob is not; the report
  says `tool_result_blobs: 1` and mentions no link and no second session. Verbatim the reporter's
  numbers.

  Traced the two ends of the claim through code rather than trusting the comments:

  - **Is the collision producible by ordinary traffic?** Yes, and by more than the error path.
    `api/runner_trace.RunTrace.returned()` stores **every** tool result through
    `_stored_ref` → `session_sink` → `store_tool_result`, gated only on
    `stream_max_result_bytes` (default **131072**, i.e. on) — not on success. So any two sessions
    whose tools returned byte-identical text share one blob. The finding's flagship case is
    slightly mis-quoted: `agent/tool_authz.unexpected_error_result()` returns *"Error: that tool
    failed unexpectedly and returned nothing. …"*, not `"Error: Function failed."` — that literal
    survives only in a stale comment in `api/tool_results.py`, and `include_detailed_errors` is
    not a setting anywhere in `core/config` (grep finds it only in two comments). The **mechanism**
    is unaffected: it is still one fixed byte string for every unexpected tool exception in the
    deployment, hence one blob shared by every session that ever had one.
  - **Is the loss permanent?** Yes by default. `retention_tool_results_days` defaults to `0`
    (`core/config/memory.py:83`), so nothing else was going to remove that blob.
  - **Is the read really 404?** Yes. `load_tool_result` joins `tool_result_links` (the join *is* the
    authorization), returns `None` with the link gone, and `api/routes/results.py:45` raises
    `HTTPException(404, "no such tool result")`.

- **Why**

  The trigger is reachable by the intended operator path with no user in the loop: `erase_actor` has
  exactly one caller, `python -m chemclaw.cli.erase_actor <oid> --apply`, i.e. offboarding. Nothing
  upstream scopes the blob DELETE — the subquery selects `content_hash` values, and a content hash
  is by construction not identity-scoped. No gate, no validator, no grant stands in the way; the
  grant matrix in fact *forces* this shape, since `DELETE ON tool_result_links` is deliberately
  withheld.

  Two things the reporter got right that are worth restating because they are the load-bearing half:
  the erasure of the *leaver* is complete (their link goes with the blob), and the report's silence
  is real — `report.erased["tool_result_blobs"]` is the blob rowcount, and no key exists for the
  link rows the cascade took from third parties. An operator signing off a dry run sees a number
  that describes only the leaver.

  Where I part company is the weight. Three things bound the damage, and none of them was checked in
  the finding:

  1. **What is destroyed is a rendering cache, not a record.** `042_tool_result_store.sql` and
     `durable/retention.py` both treat these as trace blobs; the answer of record is
     `calculation_results` / `job_records`, which erasure does not touch. The bystander can re-run
     the tool and get the same bytes — and, by definition of a content-address collision, the bytes
     they lost are byte-identical to somebody else's, which for the flagship case is a constant
     error string.
  2. **The bystander's UI degrades rather than breaks.** `fetchable_refs()` is what the transcript
     projection consults on reload; with the link gone the ref is simply not advertised, and
     `TranscriptToolCall.result_ref` is empty — the documented "not fetchable" state that makes the
     client fall back to the stored preview. The permanent 404 the finding leads with is only
     reachable from a ref a client is still holding from the live stream of that earlier turn.
  3. **No cross-tenant read, no integrity loss, no privilege change.** This is a deletion of one
     regenerable row belonging to someone else, by an administrative command, with a reporting gap.

  Medium. It would be high if the cascade reached a table erasure is not allowed to touch, or if a
  non-admin could trigger it; neither is the case.

  **One thing the finding missed that makes the same sweep worse**, and that belongs in whatever
  change fixes this: `api/tool_results.store_tool_result` opens on `settings.postgres_dsn`, while
  `erase_actor` opens on `_session_dsn()` = `session_store_dsn or postgres_dsn`
  (`core/config/service.py:87`). In a deployment that sets `CHEMCLAW_SESSION_STORE_DSN` — a
  supported configuration the leaver's own comment is written about — the two point at different
  databases, `existing_tables()` finds no `tool_result_blobs` in the session database, and the sweep
  records `tool_result_blobs: 0` and reports success while the departed person's full untruncated
  tool output stays in the other database, indefinitely. That is a false-green on a completeness
  check, which is the failure mode the module's own `_MEMORY_ERASE` comment says it exists to
  prevent, arriving through the DSN rather than through the key. It also means the cross-session
  cascade above fires only in the default single-DSN configuration.
