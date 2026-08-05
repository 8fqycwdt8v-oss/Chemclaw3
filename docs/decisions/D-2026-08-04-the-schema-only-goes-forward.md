# D-2026-08-04-the-schema-only-goes-forward — the schema only goes forward, and a test says so

**Status:** accepted · **Date:** 2026-08-04

## Context

`infra/sql/` holds 35 migrations, applied in filename order and tracked in a `schema_migrations`
ledger. There is no down-path — no `NNN_x.down.sql`, no `migrate --to`, no rollback target in the
`Makefile` — and nothing in the repository said whether that was a decision or an omission.

That silence was the problem, not the absence. An operator preparing a release has to know what
happens if the release is withdrawn, and "look at the migrations and infer" is not an answer. The
deep-testing pass of 2026-08-04 raised it as one of two open questions to settle or explicitly
refuse; refusing without saying so is what created the ambiguity in the first place.

**Measured first.** Every migration in the tree was scanned for statements that destroy schema or
data — `DROP`, `ALTER … DROP COLUMN`, `ALTER … RENAME`, `TRUNCATE`, `DELETE FROM`. There are
**none, in any of the 35 files**. The only `ALTER TABLE` present is
`artifact_blobs ALTER COLUMN data SET STORAGE EXTERNAL` (`019`), a TOAST storage hint that moves no
data and removes nothing.

So the policy already existed and was being followed by everyone who had ever added a migration.
It was written nowhere, which is the exact state in which migration 036 drops a column and nobody
notices until a restore.

## Decision

**The schema is forward-only and additive.** No migration may drop, rename, truncate or delete.
Rollback is *deploying the previous image*, which works precisely because the schema is additive:
the old code ignores the new column, the new table sits unread, and the data stays.

`tests/test_migrations_are_additive.py` enforces it — parametrized per file, so a violation names
the migration that introduced it rather than reporting that "some file" does. Two companion checks
guard the guard: one asserts there are migrations to scan at all (an empty glob would turn every
other assertion into a tautology — the vacuous-pass shape this repository has hit repeatedly), and
one asserts every `CREATE` is `IF NOT EXISTS`, the re-runnability the ledger's drift check assumes.

## Why not a tested down-path

Three reasons, in order of weight.

**A down migration is a compliance hazard here, not a safety net.** `audit_events` is append-only
and hash-chained so that history cannot be rewritten (`infra/sql/011`, D-2026-07-31-the-audit-chain-is-versioned).
A scripted `DROP COLUMN` against it is the operation that control exists to prevent. Writing one
into the repository, tested and ready to run at deploy time, puts it one command away from a
tired operator at 02:00. The GxP posture and a rollback script for the audit trail are not
compatible, and the audit trail wins.

**The rollback that matters already exists and needs no script.** For an additive schema, reverting
the application is a complete revert: the previous image does not know about the new column and
does not read it. That property is what "additive" buys, and it is worth more than a down-path
because it is the one that works under pressure — no ordering to get right, no partial application
to recover from, no second chance to lose data.

**A down-path that is never run is a second schema definition that drifts.** The forward migrations
here are exercised on every `make db-migrate`, every CI run and every live lane bootstrap. Their
inverses would be exercised never, and an untested `DROP` executed for the first time during an
incident is worse than no plan at all.

## Consequences

**The cost is real and is stated rather than hidden.** A column that turns out to be wrong is
*deprecated*, not removed: it stops being written, its comment says so, and it stays. The tree
grows monotonically, and reading it takes longer as a result.

**A genuine removal becomes a deliberate, reviewed operation** rather than a line in a file that
runs at deploy time — a documented one-off against a specific database, with a backup taken and a
human deciding. That is the right ceremony for an irreversible act on production data, and it is
what the test's failure message points an author toward.

**The `mypy`/`ruff`/`pytest` gate carries the policy**, so it applies to every future migration
without anyone remembering this document. That is the whole point of writing it as a test: an ADR
records why, and a failing test is what actually stops the change.

**What this does not decide.** Data *backfills* (a new column populated from an old one) are
additive and therefore allowed, including the `UPDATE` statements they need — `_DESTRUCTIVE` matches
`DELETE FROM` and not `UPDATE`, deliberately. And nothing here constrains `chemclaw.durable.retention`,
which deletes rows as its declared job through the application rather than through the schema.
