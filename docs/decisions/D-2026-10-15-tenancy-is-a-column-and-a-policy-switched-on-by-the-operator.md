# D-2026-10-15-tenancy-is-a-column-and-a-policy-switched-on-by-the-operator — every tenant-scoped table carries `tenant_id` and a row-level-security policy, the connection says whose rows it may see, and nothing changes until an operator turns it on

**Status:** accepted · **Date:** 2026-10-15 · **Builds on**
`D-2026-10-07-the-architecture-programme` (decision 4: tenancy is `tenant_id` plus row-level
security in one deployment) and `D-2026-08-05-append-only-by-grant-not-by-contract` (the app role
holds only the verbs `src/` executes).

## Context

Programme items W7.4 to W7.6. The programme fixed the model; this record fixes the parts it left
open, because each of them is a place a second tenant would read the first one's rows without any
error.

What exists today. `kg_notes`, `kg_note_revisions` and `kg_edges` (migration 128) are keyed by
`tenant_id`; nothing else carries one. The schema has 61 tables, 4 of them LangGraph's own
(`checkpoints*`, `store*`), which no migration here creates. Every store borrows from
`core/db.connection()`, a per-process pool; identity is ambient (`core/identity_context.py`), bound
from the validated `Principal` by `api/auth.require_principal` and read out of activity arguments
by `durable/interceptor.py`. The runtime role (`chemclaw_app`) is separate from the table owner in
a split deployment and the same role in a single-principal one (dev, CI, `make up`).

Two facts shape the rest. A pool hands one physical connection to many requests, so anything the
database knows about "the current tenant" is per-borrow state that must be written at checkout and
gone at return. And a deployment has a database-wide fact no table can hold: the chart refuses two
releases that share a Temporal namespace, and says in terms that two releases sharing a Postgres
database is "the same hazard and this does not cover it" (`deploy/README.md`, the namespace
section).

## Options

**The model** (decided by the programme, recorded for the cost it carries):

1. **A deployment per tenant.** Strongest isolation, own keys, own database. Multiplies every
   operational cost (Temporal namespace, pgvector index, backup, migration run, on-call) by the
   tenant count. Declined as the model; it stays available for a tenant that contracts for it.
2. **`tenant_id` plus Postgres row-level security in one deployment.** One schema, one migration
   run, one pool. The database, not the application's `WHERE` clauses, refuses to return another
   tenant's row, so a store that forgets to filter fails closed. Costs: every index and key must
   account for the column, the connection must carry the tenant, and isolation is only as strong
   as the roles (a superuser or `BYPASSRLS` role sees everything).
3. **A schema per tenant.** Migrations times tenants, and the pool cannot share a connection
   across tenants without a `search_path` switch per borrow. Declined.

**Where the tenant comes from** (the authenticated principal):

4. **An app role `tenant:<id>` in the validated token's `roles`.** Assigned in the directory the
   way every other role is, already signature-checked, already on `Principal`. Exactly one is
   required once tenancy is on.
5. **A custom claim** (an optional claim or a claims-mapping policy). Needs directory features not
   every tenant has, and a second place the claim set is defined.
6. **A table mapping actor to tenant.** A second source of identity truth to keep in step with
   leavers; a mapping nobody updated is a person in the wrong tenant.
7. **The directory's own `tid`.** A deployment pins one issuer, so every token carries the same
   `tid`; it distinguishes nothing here.

**What is shared:**

8. **The calculation cache keyed by structure, shared across tenants.** A persisted result is
   never recomputed (D-011), so sharing saves the most compute. A hit or a miss tells a tenant
   that some tenant already computed this structure, and the structure is the confidential thing.
9. **Scoped per tenant.** The same structure is computed once per tenant that asks.
10. **Shared payload, scoped provenance.** Two tables and a join to make a cache hit tell nothing;
    more machinery than the saving buys at the volumes a tenant produces.

**How the isolation is switched on:**

11. **Policies enabled by the migration, every connection sets `app.tenant` always.** Every
    process, script and `psql` session that does not set it sees nothing, including every
    existing test and the migrator's own data steps. "Inert until a second tenant" would be
    false.
12. **Policies created by the migration, enabled and forced by the migrator when
    `tenancy_enabled` is set.** Off, the policies are inert, no statement changes, and the new
    column only fills itself. On, the migrator runs `ENABLE` and `FORCE`.

## Decision

**Option 2, with the tenant taken from a role (4), the calculation cache scoped (9), and the
policies switched on by the migrator (12).**

**Identity.** `Principal.tenant` is the single `tenant:<id>` role of the token. With
`tenancy_enabled` false it is `default` and roles are not read for it. With it true, none or more
than one is a 403, and an id not in the `tenants` registry is a 403 too: a tenant exists because an
operator ran `python -m chemclaw.cli.tenants add <id>`, never because a token said so. The id is a
slug (`[a-z0-9][a-z0-9_-]{0,62}`), so it can be a header value, a schedule id suffix and a
namespace component without escaping. `require_principal` binds it to a context variable beside the
actor; the turn runner binds it again where it re-binds identity, because a turn outlives its
request.

**The connection.** `core/db.connection()` writes `app.tenant` with `set_config(…, false)` on each
borrow, in autocommit so a later `rollback()` cannot revert it, from the context variable. The pool's
`reset` callback issues `RESET app.tenant` as the connection returns, after the pool has rolled
back whatever transaction was open, so a pooled connection never carries a previous tenant. No
tenant bound is the empty string, not an absent setting, and the policy `tenant_id =
current_setting('app.tenant', true)` is false for both: no rows read, every insert refused by
`WITH CHECK`. A process whose work is for one tenant (a CLI, a worker run per tenant) names it
with `tenancy_process_tenant`; with `tenancy_enabled` and nothing named there is no fallback. With
`tenancy_enabled` false nothing is written, so the borrow costs what it cost.

**The column.** `tenant_id TEXT NOT NULL DEFAULT 'default'` is added to every tenant-scoped table,
and its default then becomes `coalesce(nullif(current_setting('app.tenant', true), ''), 'default')`.
An `INSERT` that does not name the tenant takes the connection's, so no store grows a parameter and
the policy's `WITH CHECK` is what refuses a row for any other. A key that a source or a hash makes
and two tenants can both produce (`calculation_results.key`, `structures`, the blobs, the
fingerprints and measurements, `note_index.note_id`), or that begins with a person who may be moved
from one tenant to another (preferences, composed workflows, the budget), gains `tenant_id` as its
**last** column, so a lookup by the old key still leads the index. The key changes when the
operator switches tenancy on, not in the migration, and the stores name it by constraint
(`ON CONFLICT ON CONSTRAINT …`), which is the same statement under either key. Keys a
process mints (session, turn, job, exhibit and design ids) keep their key: a tenant cannot collide
with them. A name a source gives (`source`, `path`) cannot collide because a data source belongs to
one tenant and source names are unique in a deployment.

**Shared and not.** Every table that holds a chemist's content, attribution or an access signal is
tenant-scoped, including the calculation cache and its single-flight claims, the corpora and
fingerprints derived from a tenant's data, the audit trail, the turn ledger, the budget, and the
vector stores (the policy applies to the row; an `embedding_<N>` column is part of it).
**Global**, each for a stated reason: `schema_migrations` (the ledger), `tenants` and
`deployment_id` (they define the tenancy), the fleet coordination tables `session_turns`,
`session_turn_remotes`, `session_turn_frames` and `request_buckets` (the deployment's concurrent-turn
ceiling and the relay between replicas count across tenants by definition, and hold leases and
holder ids, not content), the retired `audit_anchors` (empty, unwritten) and the LangGraph tables
(`checkpoints*`, `store*`: keyed by a session's thread id or a namespace that begins with the actor,
reached only after the session or actor has been resolved through a scoped table; the organisation
skill tier, which is shared by design, takes the tenant into its namespace). A shared read-only
corpus tier is declined: a tenant that wants the public reaction corpus ingests it into its own
tenant and pays its storage.

`tests/test_tenancy_inventory.py` holds the partition: every table in the schema is either
scoped (a `tenant_id` column, the policy, and a registered key class) or in the global register with
its reason, and the migration, the grants and the README rows agree.

**Workers.** The tenant crosses Temporal as a header, `x-chemclaw-tenant`, not as a field of each
payload: a client interceptor stamps it on `start_workflow` from the context variable, a workflow
interceptor copies it onto the activities and child workflows the workflow starts, and the activity
interceptor in `durable/interceptor.py` binds it for the activity's duration. A payload field would
need every workflow input to grow one, and a missed one would be a worker that silently reads
nothing. Scheduled jobs have no request to inherit from, so `planned_schedules()` is expanded per
registered tenant (`retention@acme`), each Schedule carrying the header; a data source's schedules
carry the source's tenant. With tenancy off there is one Schedule per job under the same ids as
today. A job started without a tenant fails closed.

**Roles.** The policies are `FOR ALL … USING (…) WITH CHECK (…)` for every role, and the tables are
`FORCE ROW LEVEL SECURITY`, so the owner is subject to them: in a single-principal deployment the
runtime role is the owner, and without `FORCE` it would see every tenant. The runtime role must be
neither a superuser nor `BYPASSRLS`; the startup guard reads `pg_roles` and refuses to serve. The
migrator needs no bypass for DDL, and the maintenance that reads across tenants (`erase_actor`,
the restore drill, a data conversion) either names a tenant or runs as a role the operator has
given `BYPASSRLS`, which no shipped grant does. `tenancy_enabled` makes the migrator apply the
switch on every run (`ENABLE` and `FORCE`); turning it off again is refused while more than one
tenant is registered.

**The deployment guard (W7.6).** `deployment_id` is a one-row table the migrator stamps with the
release's identity (`deployment_id`, from the chart's release namespace and name). The migrate hook
and every API and worker start compare it with their own. A different id is refused with both ids
and the remedy, unless `tenancy_enabled` is set, the database has the policies enforced, and the
release owns at least one tenant (`tenants.deployment_id`). A tenant has one owning release, and a
request for a tenant another release owns is a 403; a tenant owned by none is served by every
release, so sharing demands that none is left (the `default` tenant every database starts with is
given to the release that has been serving it). A refused run in the migrate hook rolls the whole
run back, the stamp included. Nothing is compared where no identity is configured, and a database
that cannot be reached is readiness's to report, not a refusal. That is the check `deploy/README.md`
said no chart guard could make, made where the database can answer it.

**What it does not cover, and the guard says so.** A second tenant is refused at start while
knowledge is deployment-wide: `kg_store=git` is one repository for everyone, so the guard demands
the Postgres store before it admits a second tenant. A vector search over a policy-filtered HNSW
index returns fewer rows than asked unless iterative scan is on, so a second tenant also requires
`hnsw_iterative_scan` not `off`.

## Consequences

- Migration `131_tenancy.sql` adds the column, default, policy and (for the hash-keyed tables) the
  widened key; `132_deployment_id.sql` adds the stamp. Both are additive except the key widening,
  which ends "deploy the previous image" for those tables and is listed with the other key changes
  in `infra/sql/README.md` and `tests/test_migrations_are_additive.py`; it takes `ACCESS
  EXCLUSIVE` on each table for an index rebuild.
- With `tenancy_enabled` false (the default) nothing observable changes: the policies exist and are
  disabled, the borrow issues no extra statement, schedules keep their ids. The existing suite is
  the evidence.
- A pooler in transaction mode cannot hold `app.tenant`; a tenancy deployment dials a session-mode
  endpoint (`D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint`).
- A cached calculation is recomputed once per tenant that needs it; D-011 holds within a tenant.
- A new table without `tenant_id` and a policy, or without a reason in the global register, fails
  the inventory; a new API route without a tenant verdict fails the route inventory.

Revisit when: a tenant requires separate encryption keys or a separate database by contract (move
that tenant to a deployment of its own), when `tests/test_tenancy_leak.py` can no longer enumerate
the routes, or when the per-tenant storage of a reference corpus exceeds what a deployment will
carry twice (then a shared read-only tier is a new decision).

## What keeps it true

- `tests/test_tenancy_inventory.py` — every table is scoped with a policy or globally registered;
  keys, grants and the README agree.
- `tests/test_tenancy_connection.py` — a borrowed connection carries the bound tenant, a returned
  one carries none, an unbound one reads nothing; tenancy off issues no statement.
- `tests/test_tenancy_rls.py` — as a role that is neither superuser nor `BYPASSRLS`: select, update,
  delete and insert across tenants, the `WITH CHECK` refusal, the owner under `FORCE`, the vector
  columns.
- `tests/test_tenancy_leak.py` — every API route has a tenant verdict, and none returns or changes
  the other tenant's rows with two tenants' data in a real database.
- `tests/test_deployment_guard.py` — two releases on one database are refused with both ids unless
  tenancy and distinct tenants say otherwise.
