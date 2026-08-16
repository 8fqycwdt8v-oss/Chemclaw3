# Sweep: SQL, migrations and grants

Environment: live PostgreSQL 16.15 (`pgvector/pgvector:pg16`) from `infra/docker-compose.yml`,
all 46 migrations applied, `uv run` against the synced venv. Every finding below was reproduced by
running code, not by reading it.

---

## A split-principal deployment cannot take a single turn: the runtime role has no CREATE on schema `public`

- **Severity**: critical
- **Location**: `infra/sql/grants/app_privileges.sql` (the `IF to_regclass('public.checkpoints') IS NOT NULL` block and the comment above it) · `src/chemclaw/agent/checkpointer.py:326-348` (`checkpointer()` → `saver.setup()`) · `src/chemclaw/agent/checkpointer.py:355-372` (`_checkpoint_pool`, which connects on `_session_dsn()`, i.e. the **runtime** DSN) · `deploy/helm/chemclaw/templates/migrate-job.yaml`
- **Trigger**: The deployment the grants file exists for — a database with a `chemclaw_app` role separate from the schema owner, `CHEMCLAW_POSTGRES_MIGRATION_DSN` on the migrate Job only (`values.yaml:544-545`), the runtime DSN on the app pods. First turn on a fresh install.
- **Consequence**: `AsyncPostgresSaver.setup()` runs as `chemclaw_app` and issues `CREATE TABLE IF NOT EXISTS checkpoint_migrations …`. On PostgreSQL 15+ the default `public` schema ACL is `pg_database_owner=UC` / `=U` — PUBLIC has USAGE and **not** CREATE — and `app_privileges.sql` grants only `USAGE ON SCHEMA public`. Every turn fails at checkpointer construction. `AsyncPostgresStore.setup()` (`agent/scratchpad.py`) fails identically. The four guarded `to_regclass` blocks in the grants file can therefore never fire, because the tables they guard are never created by anyone.

  The comment those blocks carry asserts the opposite, in the present tense, at length: *"The first install survives that because the tables do not exist yet when this file runs: the app pods create them lazily, **as owner**, on first turn."* Under a split principal the app pods are by construction **not** the owner — that is the entire premise of the file. The described second-`helm upgrade` outage also requires the app role to own those tables, which cannot happen on PG15+ without an explicit `GRANT CREATE ON SCHEMA public` that nothing in this repository issues.

- **Evidence**: the `public` ACL on the shipped image, and the failure, both measured.

  ```
  $ docker exec infra-postgres-1 psql -U chemclaw -d chemclaw -c "\dn+ public"
    Name  |       Owner       |           Access privileges
   -------+-------------------+----------------------------------------
    public| pg_database_owner | pg_database_owner=UC/pg_database_owner+
          |                   | =U/pg_database_owner
  ```

  Fresh database `auditsql` owned by `chemclaw`, role `chemclaw_app` created, then
  `python -m chemclaw.core.migrate && python -m chemclaw.core.grants` as the owner (both
  succeeded, 46 migrations + `applied grants: app_privileges.sql`), then a checkpointer build as
  the runtime role:

  ```
  $ CHEMCLAW_SESSION_STORE=postgres \
    CHEMCLAW_POSTGRES_DSN="postgresql://chemclaw_app:apppw@localhost:5432/auditsql" \
    uv run python -c "import asyncio; from chemclaw.agent import checkpointer as ck; asyncio.run(ck.checkpointer())"

  File ".../langgraph/checkpoint/postgres/aio.py", line 98, in setup
      await cur.execute(self.MIGRATIONS[0])
  psycopg.errors.InsufficientPrivilege: permission denied for schema public
  LINE 1: CREATE TABLE IF NOT EXISTS checkpoint_migrations (
                                     ^
  ```

  `tests/test_database_privileges.py` cannot see this: it parses `CREATE TABLE IF NOT EXISTS (\w+)`
  out of upstream's `MIGRATIONS` strings purely to learn *table names*, then compares DML verbs
  (`tests/test_database_privileges.py:92-120, 210`). It never asks whether the role may execute
  those `CREATE`s. Nothing in the suite runs the grants file and then acts as `chemclaw_app`.

- **Fix**: create the library-owned tables from the migrator, not from the app pods. Add a step to
  the migrate Job (before `chemclaw.core.grants`, which already runs last) that opens
  `migration_dsn()` and awaits `AsyncPostgresSaver(...).setup()` and `AsyncPostgresStore(...).setup()`,
  so the tables exist and are owned by the schema owner before the guarded grants run — which is
  also what makes the guards land on the *first* deploy rather than the second. Then the app role
  needs no CREATE at all and `USAGE ON SCHEMA public` stays correct. Keep the app-side `setup()`
  call idempotent, or drop it. A test that applies `app_privileges.sql` and then runs one turn's
  worth of statements as `chemclaw_app` is the check that would have caught this.

---

## `erase_actor` deletes other people's tool results: the content-addressed blob cascade is not identity-scoped

- **Severity**: high
- **Location**: `src/chemclaw/agent/leaver.py:176-181` (the `tool_result_blobs` entry of `_ERASE`) · `infra/sql/042_tool_result_store.sql` (`tool_result_links.content_hash … REFERENCES tool_result_blobs ON DELETE CASCADE`)
- **Trigger**: Two sessions owned by two different people whose tools returned **byte-identical** text. Erase one of them.
- **Consequence**: the statement selects blobs by the leaver's `session_id`, but a blob is keyed on the SHA-256 of its bytes and is shared by every session that produced those bytes (`api/tool_results.py`: *"Content-addressed, so a repeat stores nothing"*). Deleting the blob cascades and removes the **bystander's** `tool_result_links` row too. The bystander's session, transcript and ownership row all survive, still carrying the `result_ref`; `GET /sessions/{id}/tool-results/{ref}` now 404s for them, permanently. The erasure report counts `tool_result_blobs: 1` and says nothing about the second session — a partial erasure that looks complete, plus a collateral deletion the report actively hides.

  This is not a rare collision. `api/tool_results.py` states it as a certainty: `include_detailed_errors` is off, so **every** unexpected tool exception in the whole system returns the string `"Error: Function failed."` — one shared blob across the entire deployment. Any deterministic tool answer (a property lookup, a hazard screen for a common compound) collides the same way.

  The module's own docstring claims the opposite property — *"Erasure is **irreversible and identity-scoped**, so the caller states the actor exactly; there is no pattern match"* — and `_actor_forms` carries a long, measured argument against `LIKE` matching precisely because it *"deletes a different person's conversation"*. The blast radius arrives through a different route than the one that was guarded.

- **Evidence**: `/tmp/repro_leaver.py`, run against the live dev database.

  ```
  before erase, links: [('s-alice',), ('s-bob',)]
  erased: {'tool_result_blobs': 1, 'session_owners': 1}
  after erase, links: []
  blob rows remaining: 0
  owners remaining: [('s-bob', 'bob-oid')]
  ```

  Setup: `session_owners` rows `('s-alice','alice-oid')` and `('s-bob','bob-oid')`; one
  `tool_result_blobs` row for `sha256("Error: Function failed.")`; one `tool_result_links` row per
  session pointing at it. `erase_actor("alice-oid", apply=True)`. Bob's link is gone; Bob is not.

  Not covered by `tests/test_leaver.py`: `tool_result_blobs` appears only in
  `test_the_erase_statements_are_valid_sql` (lines 412-434), which runs against
  `oid-nobody-at-all` and asserts `erased_total == 0`. Its docstring reasons about the cascade
  approvingly — *"The link rows are not listed because they are not deleted here: the cascade
  removes them"* — without noticing the cascade crosses sessions.
  `test_erasing_one_person_spares_another_whose_id_contains_theirs` tests the `LIKE` hazard only.

- **Fix**: delete the *link* rows for the leaver's sessions, then delete only the blobs that no
  link references any more:

  ```sql
  DELETE FROM tool_result_links WHERE session_id IN (<_SESSION_SCOPED>);
  DELETE FROM tool_result_blobs b
   WHERE NOT EXISTS (SELECT 1 FROM tool_result_links l WHERE l.content_hash = b.content_hash);
  ```

  This needs `DELETE ON tool_result_links` in `app_privileges.sql`, which the grants file currently
  withholds on purpose. That comment's reasoning (*"withholding it is what keeps 'the sweep deletes
  blobs and links follow' the only way a link row can disappear"*) is exactly what forces the
  over-deletion, so the grant is the thing to change: the invariant worth keeping is "a link never
  outlives its blob", which the foreign key enforces regardless of who may DELETE. Report both
  counts separately so an operator sees how many blobs survived because someone else also produced
  them.

---

## `VectorBinding.server_embed_function` reaches the SQL text unchecked, contradicting the invariant both modules state

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/eln/warehouse/sql.py:130-136` (`vector_statement`) · `src/chemclaw/ingest/eln/warehouse/binding.py:483-520` (`VectorBinding._is_coherent`, which checks `relation`, `key`, `vector_column`, `content_columns` and `filter_columns` — and not this field)
- **Trigger**: any `datasource.yaml` with `vector.embedding: server` and a `server_embed_function` value.
- **Consequence**: the field is declared as a plain `str = Field(default="")`, never passed through `_check_identifier`, and interpolated verbatim into the statement. Two documented invariants are false as written:
  - `sql.py` module docstring: *"Every value is bound; only checked identifiers are written. … Relation and column names reach the statement text, and each one was matched against `binding._IDENTIFIER` before it got here. … **The one deliberate exception is `where:`**"* — there are two exceptions, and only one is deliberate.
  - `binding.py:_IDENTIFIER` comment: *"everything a binding contributes to a statement is either an identifier matching this or a bound parameter."*

  Practically this is not a privilege boundary crossing — a manifest is operator-authored, from
  `settings.data_sources_dirs`, exactly like `where:` — so the harm is (a) an invariant that is
  asserted and not held, which is what a future reader will trust, and (b) the loss of the
  startup-time failure the check exists to give: a malformed function name that `_check_identifier`
  would reject at worker startup instead surfaces as a syntax error from the warehouse on the first
  live query.

- **Evidence**: `/tmp/repro_binding.py`.

  ```
  binding accepted: 'F(1)) UNION ALL SELECT PASSWORD, 1 FROM CREDS --'
  SQL:
   SELECT ID, BODY, VECTOR_COSINE_SIMILARITY(EMB, F(1)) UNION ALL SELECT PASSWORD, 1 FROM CREDS --(%s)) AS CHEMCLAW_SCORE FROM REACTIONS ORDER BY CHEMCLAW_SCORE DESC LIMIT %s
  params: ['query text', 5]
  ```

  `VectorBinding.model_validate` accepted it without complaint.

- **Fix**: type the field `Identifier` and add `_check_identifier(self.server_embed_function, "vector server_embed_function")` to `_is_coherent` under the `embedding == "server"` branch — the dotted form the regex already allows covers `SNOWFLAKE.CORTEX.EMBED_TEXT_768`, which is the realistic value. Then either delete "the one deliberate exception" sentence or leave it true.

  Related, minor, and worth a line in the same change: `_IDENTIFIER` uses `$` as its anchor, so
  Python accepts one trailing newline — `_check_identifier("REACTIONS\n", …)` returns rather than
  raises (measured). Nothing injectable follows from it (`"FOO\n--"` is correctly rejected), but
  `\Z` is the anchor that means what the comment means.

---

## Retention's per-session sweep is permanently starved by any session it refuses

- **Severity**: medium
- **Location**: `src/chemclaw/durable/retention.py:152-155` (`_EXPIRED_SESSIONS`) and `:281-329` (`_prune_session_messages`)
- **Trigger**: one `session_messages` row in a stored shape `stored_call_ids` cannot read (returns `None`) — the module itself documents that the table holds two historical shapes and that a third would be silent. The session is refused with `continue` and nothing ever makes it readable.
- **Consequence**: `_EXPIRED_SESSIONS` is `SELECT DISTINCT session_id … WHERE created_at < cutoff ORDER BY session_id LIMIT cap` — a **stable** ordering with no cursor and no exclusion of refused sessions. A refused session still has expired rows, so it is re-selected first on every pass forever and permanently consumes one of `retention_max_sessions_per_pass` (default 500) slots. At 500 such sessions the sweep deletes nothing, for the rest of the deployment's life, while reporting `deleted: {"session_messages": 0}` and a `sessions_deferred` that the model documents as merely *"the next scheduled pass has work"*.

  This is specifically **not** what `_prune_checkpoints`' analogous cap does, because a thread there is always deleted once selected. And it is not what `droppable_rows`' straddling case does either — that one genuinely self-corrects when the partner expires. Only the unreadable-row refusal is permanent; `droppable_rows`' comment says *"the next pass sees the same rows once somebody has looked at them"*, and nothing makes anybody look.

  Confirmed by the plan that the LIMIT terminates early on `session_messages_session_recent_idx` — so the same lexicographically-smallest sessions really are re-read every pass:

  ```
  Limit (rows=501) -> Unique -> Index Only Scan using session_messages_session_recent_idx
        Index Cond: (created_at < (now() - '30 days'::interval))
  Execution Time: 2.384 ms      (200,000 rows / 20,000 sessions)
  ```

- **Evidence**: `/tmp/repro_starve.py`, cap set to 1, one refused session (`s-000-stuck`) sorting
  ahead of one perfectly prunable session (`s-999-ok`):

  ```
  skipping retention for session s-000-stuck: 1 row(s) in an unrecognised stored shape (ids: 200001)
  pass 1: deleted=0 deferred=1 remaining=[('s-000-stuck', 1), ('s-999-ok', 1)]
  pass 2: deleted=0 deferred=1 remaining=[('s-000-stuck', 1), ('s-999-ok', 1)]
  pass 3: deleted=0 deferred=1 remaining=[('s-000-stuck', 1), ('s-999-ok', 1)]
  ```

  `s-999-ok` is never reached, on any pass, and the outcome model reports a constant
  `sessions_deferred=1` rather than "wedged".

- **Fix**: make the batch a cursor rather than a fixed head — carry the last `session_id` handled
  into the next pass (`AND session_id > %s`, wrapping at the end), so a refused session costs one
  pass rather than every pass. Cheaper alternative: count refusals separately from `sessions_deferred`
  (`sessions_refused`), so the wedge is visible in the job's own result, and emit a metric on it.
  The cursor is the actual fix; the counter is what makes the failure legible either way.

---

# Checked and found sound

Recording these because the brief asks for them by name and "no finding" is only useful if it says
what was looked at.

- **The `037_` and `043_` filename collisions have no ordering dependency.** `sorted()` in
  `core/migrate.py:_read_sql_files` applies `037_bo_suggestion_provenance.sql` (adds `problem`,
  `job_id` to `bo_suggestions`, created by `031`) before `037_document_index.sql` (creates
  `document_files`/`document_chunks`) — disjoint objects. `043_session_listing.sql`
  (`session_owners.title`, an index on `session_messages(session_id, created_at DESC)`) before
  `043_session_message_shape.sql` (`session_messages.message_shape`) — both touch `session_messages`
  but different objects, and the index does not reference the new column. Swapping either pair
  changes nothing. Downstream files that depend on the `037` document tables (`038`, `040`, `041`)
  all sort after both.
- **`ingest/eln/warehouse/sql.py`'s `where:` is a genuine, correctly-argued exception**, not a
  smuggled one: manifests come from `settings.data_sources_dirs` on disk (`ingest/sources/registry.py:_source_dirs`),
  i.e. the same operator-controlled mount as the `module:callable` driver the same file names.
  Every *other* identifier reaching those four statements does pass `_check_identifier` —
  `entry.relation/key/created_at/modified_at`, `block.relation/foreign_key/order_by`,
  `vector.relation/key/vector_column/content_columns/filter_columns.values()`,
  `component.attributes` — and `since`, `limit`, `keys`, `top_k`, the query vector and
  `server_embed_model` are all bound. `server_embed_function` is the single leak (above).
- **The grant matrix covers every DELETE the code issues.** Enumerated all 20 `DELETE FROM` sites in
  `src/`; every target (`session_messages`, `session_events`, `session_turns`, `subscriptions`,
  `user_preferences`, `session_owners`, `artifact_blobs`, `document_files`, `document_chunks`,
  `tool_result_blobs`, `checkpoints`/`_blobs`/`_writes`, `store`, `store_vectors`) is in a
  full-DML or an explicit group. No code UPDATEs `audit_events`. `schema_migrations` is correctly
  absent. The `tool_result_links` cascade-vs-privilege claim is accurate (and is what the
  erasure finding above turns on).
- **Every `ON CONFLICT` target has a matching constraint or index**, including the two expression
  and partial ones: `subscriptions (owner, query, coalesce(note_type,''))` → `subscriptions_identity`
  (017); `bo_suggestions (campaign_id, job_id) WHERE job_id <> ''` → `bo_suggestions_job_idx` (037);
  `session_events (dedupe_key) WHERE dedupe_key IS NOT NULL` → `session_events_dedupe_idx` (014);
  `predictions (calc_type, calc_version, input_hash)` → `predictions_identity` (016).
  The `DO NOTHING … RETURNING id` in `campaign_record_store` correctly falls back to
  `_SELECT_SUGGESTION_BY_JOB` on the empty return. `tool_results._INSERT_BLOB`'s
  `DO UPDATE SET created_at = now()` is the right choice for a column that *is* the retention
  clock, and its comment matches the code.
- **`LIMIT` without a deterministic `ORDER BY`**: none found that matters. The two vector searches
  (`retrieval/vector_index.py:318`, `ingest/documents/index.py:680`) order by distance in the inner
  query and re-sort the k rows with a full tie-break outside — both carry the EXPLAIN numbers that
  justify the shape, and the "ties at the k-th place are not pinned" caveat in both comments is
  honest about what remains. `plan_approvals`, `note_proposals`, `bo_suggestions`, `job_records`
  and both retention batch queries all carry a unique secondary key or order on a unique column.
- **The two retention batch queries are genuinely bounded**, contrary to my first hypothesis that
  the `LIMIT` sat above an unbounded aggregate. Measured on seeded data (200,000 rows / 20,000
  groups each): `_EXPIRED_SESSIONS` terminates early on `session_messages_session_recent_idx`
  (2.4 ms, 5,001 rows touched), and `_EXPIRED_THREADS`' `GROUP BY … HAVING max(...)` terminates
  early on `checkpoints_thread_id_idx` as a `GroupAggregate` (5.7 ms, 5,011 rows touched). Neither
  scans its table.
- **`document_chunks`' orphan sweep is affordable.** `DELETE FROM document_chunks c WHERE NOT
  EXISTS (…)` plans as a Hash Anti Join and ran in **154 ms** at 500,000 chunks over 50,000 files,
  which extrapolates to ~1.5 s at the 5M-chunk scale the module's own comments contemplate — well
  inside the 30 s `pg_statement_timeout_seconds`.
- **`droppable_rows`' contraction property holds** and the `None`-is-not-empty-set distinction is
  implemented as documented; the refusal is correct, it is only the *batch selection* around it
  that starves (above).
