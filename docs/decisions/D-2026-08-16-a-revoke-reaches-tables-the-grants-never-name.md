# D-2026-08-16-a-revoke-reaches-tables-the-grants-never-name — the runtime's own schema is granted explicitly, and derived from upstream

**Status:** accepted · **Date:** 2026-08-16

## Context

`infra/sql/grants/app_privileges.sql` reconciles the runtime role's privileges on every release. Its
shape is `REVOKE ALL ON ALL TABLES` followed by an enumerated re-grant, and
`D-2026-08-05-append-only-by-grant-not-by-contract` argues for exactly that: starting from nothing is
what makes the file state the *whole* matrix, so removing a verb from the code narrows the grant
instead of leaving the old one standing.

The LangGraph rebuild put six tables in that database which no file in `infra/sql` declares.
`AsyncPostgresSaver.setup()` creates `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and
`checkpoint_migrations`; `AsyncPostgresStore.setup()` creates `store` and `store_migrations` (plus
`store_vectors` and `vector_migrations` under an `index_config` this deployment does not set). They
are created lazily, by the application role, on first use.

`REVOKE ALL ON ALL TABLES` reaches them. The enumerated re-grants named none of them.

## What was actually wrong

Not a missing privilege — a missing privilege on a table the role **owns**, which is a thing most
readers assume cannot happen. Measured against a real Postgres 16:

```
owner of checkpoints                     : chemclaw_app
after REVOKE ALL + GRANT SELECT ON ALL   : INSERT=f  SELECT=t
```

Revoking from an owner materialises an ACL where the owner previously had implicit rights, and the
implicit rights do not come back. So:

- **A first install is fine.** The tables do not exist when the migrate Job runs, so the `REVOKE` is
  a no-op for them and the app pods then create them as owner with full rights.
- **The second `helm upgrade` is an outage.** `migrate-job.yaml` re-runs `db-grants` on every
  release — deliberately, and that is the right cadence. This time the tables exist, the `REVOKE`
  strips them, and `GRANT SELECT ON ALL TABLES` hands back read only. Every turn fails at its first
  checkpoint write with `InsufficientPrivilege`; `agent/leaver.py`'s erasure and
  `durable/retention.py`'s checkpoint sweep fail the same way.

**The guard that should have caught it was structurally blind.**
`tests/test_database_privileges.py` derives the write matrix from the SQL literals in `src/` and
compares it to the grant file in both directions — the right design, and it reported
`the code writes what the grant withholds: {}`. Its `_tables()` built the known set from
`CREATE TABLE IF NOT EXISTS` in `infra/sql/*.sql` only, and `note()` dropped any table outside it.
So `_DYNAMIC`'s entry explicitly naming `checkpoints` was discarded before it could assert anything,
and the file's own `infra/sql/README.md` note — "these tables appear in no schema review and in no
inventory" — stated the diagnosis without anyone reading it as a finding.

This is the repository's recurring shape, in a new place: a check that runs, returns the right
answer, and is wired to a set that cannot contain the thing it is checking for.

## Decision

**Grant the runtime-created tables explicitly, guarded per table, and derive the same set from
upstream in the test.**

Three parts, and the third is the one that matters in a year:

1. **The grant file names all eight tables**, with the verbs read off the installed distributions
   rather than assumed: `checkpoints` / `checkpoint_writes` / `store` / `store_vectors` upsert
   (`ON CONFLICT … DO UPDATE`), `checkpoint_blobs` does not (`DO NOTHING`, so no UPDATE), the three
   `*_migrations` ledgers take one INSERT per schema step, and the DELETEs are ours.

2. **Guarded per table, not per `setup()` group.** A `GRANT` on a missing table raises, and a raise
   anywhere in the `DO` block aborts the whole reconciliation — so one interrupted `setup()` would
   leave *every* table in the file ungranted, converting a narrow bug into a total one. The vector
   pair is genuinely absent on this deployment, which makes the independent guard load-bearing
   rather than defensive. This was found by running the fixed file against a database holding only
   `checkpoints`: the group-guarded version aborted, and nothing was granted.

3. **`_tables()` unions what upstream creates**, parsed from `checkpoint_base.MIGRATIONS`,
   `store_base.MIGRATIONS` and `store_base.VECTOR_MIGRATIONS`. A table a minor bump adds now fails
   the check instead of inheriting `GRANT SELECT` and being discovered as a write outage.

`store_migrations` and `vector_migrations` are the exception and are named by hand, because upstream
spells them inline in `setup()` (`_get_version(cur, table="store_migrations")`) rather than in any
`MIGRATIONS` list — there is no statement to read them out of. `tests/test_upstream_surface.py` pins
both names against upstream's source, so a rename turns red there instead of silently un-granting.

## What this decision does not change

`CHECKPOINT_TABLES` (`agent/checkpointer.py`) stays as it is, and the first draft of this fix was
wrong to plan otherwise. It is deliberately the *conversation-bearing* set — `checkpoint_migrations`
is excluded by name, with a reason, and `tests/test_message_migration.py` proves the list complete
against upstream. It feeds `DELETE … WHERE thread_id`, and `checkpoint_migrations` has no
`thread_id`, so adding it there would have broken erasure and retention to fix a grant. The grant
needs a different set — every table in the database, not every table holding a thread — and that is
why the derivation lives in the test rather than being borrowed from a constant that means something
else.

## Consequences

- The second-deploy outage is closed, and closed in the direction that fails loudly: the guard now
  reports all eight tables when the grant block is removed (verified by reverting the SQL alone).
- The reconciliation is still idempotent — a second run over a fully-granted schema leaves the
  privileges standing (verified).
- One honest limit: this file grants, and does not test that `setup()` can *create* the tables in the
  first place. That needs `CREATE` on the schema, which is a property of how a split-principal
  deployment provisions its role and not of anything in this repository. It is untested here because
  no such deployment exists yet to test against — recorded rather than implied.
