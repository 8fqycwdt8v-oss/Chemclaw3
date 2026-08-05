# D-2026-08-05-append-only-by-grant-not-by-contract — append-only by grant, not by contract

**Status:** accepted · **Date:** 2026-08-05

## Context

`infra/sql/006_audit_events.sql` has said this since it was written:

> Append-only by contract: the writer (`chemclaw.agent.audit_store.PostgresAuditSink`) only inserts.

Nothing enforced it. Across all 36 migrations there was **no `GRANT`, no `REVOKE`, no `CREATE ROLE`,
no trigger** — one `CHEMCLAW_POSTGRES_DSN` with full DDL and DML, mounted on the front door, on
every Temporal worker and on every connector server, for the life of each pod. So the credential
that runs a chat turn could also `UPDATE` or `DELETE` the GxP trail recording that turn, and could
`DROP` any table in the schema.

The system's answer to tampering is *detection*, and it is good: the hash chain (`infra/sql/011`,
D-061), its versioning (D-2026-07-31-the-audit-chain-is-versioned), the signed high-water anchors
that catch a trailing truncation a chain cannot see
(D-2026-08-01-a-restore-is-a-truncation-nobody-can-see), and `make audit-verify` on a schedule with
an alert. What was missing was *prevention*, and for a 21 CFR Part 11 posture the distinction
matters: detection tells an auditor that the record was altered, which is a finding, not a control.

`docs/planning/BACKLOG.md` recorded it as an open `[M]`: "One database credential can rewrite the
audit chain… no `REVOKE`, no trigger, no separate role."

## Decision

**Two principals: a migrator that owns the schema, and a runtime role granted exactly the verbs the
code executes.**

`postgres_migration_dsn` is the migrator, falling back to `postgres_dsn` when unset — so a
single-principal database stays a fully supported deployment and is what `make up`, CI, the whole
test suite and every dev machine run. Splitting is a deployment's opt-in, not a precondition for
running the software.

**The grant matrix is derived, not maintained.** `tests/test_database_privileges.py` parses the SQL
literals in `src/` — including f-string statements, whose fragments an `ast.Constant` walk splits —
and asserts the grant file matches in **both** directions. A verb the code needs and the grant
withholds is an outage; a verb the grant allows and the code never uses is the boundary quietly
widening back out. This is the same shape as `connector-validate` and `datasource-validate`: a
declaration checked against the live surface, not a second definition of it.

That test earned its place immediately. It caught two over-grants and one under-grant in the matrix
this ADR's author had written by hand — `turn_costs` upserts, so it needs `UPDATE`; `job_records`
and `note_index` were flagged as excess until the derivation was taught to read f-string SQL, which
is how the two genuine upserts had been hidden from it.

**The grants are not a numbered migration**, and that is the second decision here. `infra/sql/*.sql`
is applied exactly once per file and tracked by checksum, which is right for a schema change and
wrong for this in two ways at once:

- A deployment that creates its runtime role *after* the first `db-migrate` would never have the
  grants applied at all.
- Every table added by a later migration would ship ungranted, and the application would break on
  first use of it while the ledger reported everything applied.

A grant is a *reconciliation* between a schema that keeps growing and a role that may appear at any
time. So it lives in `infra/sql/grants/`, invisible to the runner's non-recursive glob by
construction rather than by an exclusion list, and `make db-grants` re-applies it on every deploy,
after the migrations. The chart's hook Job runs both in one container, `migrate && grants`, so the
ordering is the shell's rather than a hook weight in another file — and so a failed migration is
never followed by a grant run.

**The migration credential is mounted on the hook Job and nowhere else.** `secrets.keys` is
iterated by `chemclaw.env`, which every Deployment includes, so listing it there would have left
the exposure exactly where it was while appearing to fix it. A second map (`secrets.migrationKeys`)
and a second helper carry it, `optional: true` so a single-principal deployment still starts, and
`tests/test_helm_chart.py` asserts no other template includes that helper.

**The runner moved to `chemclaw.core.migrate`.** The schema belongs to the whole application; it
was living in `science/calc/`, which is neither of the two homes `ARCHITECTURE.md` allows for
capability code — an artefact of the QM cache having been the first thing that needed a table.

## What the boundary actually buys, stated precisely

Verified against a live Postgres 16 with a real `chemclaw_app` role, not reasoned about:

| Operation | As the runtime role |
| --- | --- |
| `SELECT` / `INSERT` on `audit_events` | allowed |
| `UPDATE` / `DELETE` / `TRUNCATE` on `audit_events` | refused (`InsufficientPrivilege`) |
| `DELETE` on `audit_anchors` | refused |
| `DELETE` on `calculation_results`, `job_records` | refused |
| `INSERT` on `schema_migrations` | refused |
| `CREATE TABLE` | refused |
| upsert on `calculation_results`, the session tables' full DML | allowed |

Two of those deserve their reasons said out loud. `audit_anchors` is insert-only because an anchor
is the evidence that a *trailing* truncation happened, so an actor able to delete anchors could hide
the one alteration the chain by itself cannot see. And `calculation_results` and `job_records` lose
`DELETE` because `durable/retention.py` already refuses to prune them, for stated reasons — this
turns those refusals from intentions into enforcement.

**This does not make the audit trail tamper-proof, and the ADR will not claim it does.** The
migrator credential can still rewrite anything. What changes is the blast radius: the credential
that can is no longer the one mounted on every pod for the life of the deployment: it lives on a Job
that exists for the seconds a release takes. Whoever holds the migration secret can still do it, and
the hash chain plus the anchors remain the detection layer they always were. Prevention here is a
narrowing, not an absolute.

## Consequences

**A deployment that wants the split has work to do that this chart cannot do for it**: create the
`chemclaw_app` role, point `CHEMCLAW_POSTGRES_DSN` at it, and put the owner DSN in
`CHEMCLAW_POSTGRES_MIGRATION_DSN`. `docs/guides/runbook.md` documents it. The chart deploys neither
Postgres nor the roles inside it (the ownership row in `docs/planning/BACKLOG.md`), so role creation
is necessarily the operator's.

**`CREATE EXTENSION vector` needs superuser or `rds_superuser` on most managed Postgres.** That was
always true and was always the migration path's problem; the split makes it visibly so, which is an
improvement over a runtime credential that had to carry it.

**A seventh plain secret**, and D-047 says a new one is an architecture change. The argument is that
this is the one secret whose *purpose* is to be absent from the pods — every other entry in
`secrets.keys` exists so a pod can use it, and this exists so a pod cannot.

**A new failure mode: a grant that lags its migration.** If `db-grants` is skipped, the application
hits `InsufficientPrivilege` on the new table. The hook Job runs both, so this only bites an
operator migrating by hand — which the runbook now says not to do.
