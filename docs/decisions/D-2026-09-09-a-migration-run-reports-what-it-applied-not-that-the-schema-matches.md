# D-2026-09-09-a-migration-run-reports-what-it-applied-not-that-the-schema-matches — the two directions of a schema mismatch are two different answers

**Context.** An image and a database can disagree in two directions, and until now nothing in this
repository looked at either.

*Backwards* (the image is behind the schema — a rollback): `migrate()`'s apply loop iterates the
**image's** files, so a ledger row with no corresponding file is never looked at. Measured, with a
directory shipping through `080` against a database at `091` (90 ledger rows):

```
migrate() → applied: []
applied migrations: (none — already up to date)
ledger rows: 90        # eleven of them name files this image does not have
```

That is the state an operator is in immediately after a rollback, and it is exactly when they run
the documented recovery command to find out whether the schema matches the image. It told them it
did.

*Forwards* (the image is ahead of the schema — new code, old database): every statement today's code
issues against an older schema dies loudly, which is right —

```
[FAIL] SELECT claimed_at FROM outbox        → UndefinedTable: relation "outbox" does not exist
[FAIL] SELECT epoch FROM calculation_results → UndefinedColumn: column "epoch" does not exist
[FAIL] SELECT turn_id FROM turn_costs        → UndefinedColumn: column "turn_id" does not exist
```

— but `/readyz` probed with `SELECT 1`, which every schema answers. Measured against a database
migrated through `080` with the current code: `200 {"status": "ready", "connectors_unhealthy": 8}`.
The pod joins the Route and throws on the first turn. The Helm `pre-upgrade` hook Job normally
prevents that state; `--no-hooks`, a `kubectl set image`, and an ArgoCD sync that proceeds past a
failed hook all reach it.

**Decision.**

1. **The backwards direction is a WARNING, not a refusal, and it is the migrator's to report.**
   `migrate()` now reads the ledger once under the lock it already holds and logs
   `migrate.database_ahead` — the count and the newest unknown filename — before it applies
   anything. It does not refuse: **a rollback has to be able to start**, and this schema only ever
   goes forward by a merged decision, so there is nothing here for the migrator to undo. The whole
   defect was that the mismatch was silent.

   The comparison is a set difference computed in Python against `sorted(sources)`'s own
   comparator, not a `WHERE filename > …` predicate, because Postgres would compare under the
   database's collation while the apply order is a Python string sort — and two orderings that
   agree today are a defect waiting for a locale.

   `__main__`'s "(none — already up to date)" is now "(none)". The phrase asserted a match the run
   had never checked, and it was the half of the output an operator actually reads.

2. **The forwards direction is readiness, and the comparison is one-directional.**
   `/readyz` now asks a second question in the same cached, single-flighted round trip: is the
   newest migration **this image ships** recorded in `schema_migrations`? Ledger rows past it are
   not looked at, so a rolled-back pod stays ready and can serve. The body distinguishes the two
   outages — `"database unreachable"` against `"schema behind image"` — because a kubelet reads
   only the status code and the operator running `curl` gets that line and nothing else.

   **The direction is the decision, and getting it backwards would be worse than the defect.**
   A symmetric check ("the ledger equals the file set") refuses traffic on *every* rolled-back pod,
   turning a recovery into an outage. Driven as an experiment rather than argued: with the
   predicate flipped to `NOT EXISTS (… WHERE filename > %s)`,
   `test_readyz_stays_ready_when_the_schema_is_ahead_of_the_image` fails with
   `a rolled-back pod refused to serve: {"status":"schema behind image"}`.

   **It gates on positive evidence only.** A ledger that cannot be read at all — no
   `schema_migrations`, or a role that cannot select it — stays ready and logs. The probe reads
   `session_store_dsn or postgres_dsn`, which under a split session store is a different server
   from the one `migrate()` runs against; every database that can serve as the session store was
   migrated by this same runner and so carries the ledger, but *"was"* is a claim about somebody
   else's operations, and readiness is the wrong place to be right about it by refusing. Driven the
   same way: with the `UndefinedTable` branch returning `False`,
   `test_a_ledger_it_cannot_read_does_not_take_the_pod_out_of_the_route` fails with
   `an unreadable ledger drained the pod`.

   Readiness rather than a startup refusal, matching
   `test_a_database_outage_drains_the_pod_without_restarting_it`: a mismatch drains the pod from
   the Route rather than crash-looping it, and the pod becomes ready again the moment an operator
   applies the migration, with no restart.

3. **A blocked peer migrator is a `MigrationError` that names the peer and the budget.** Waiting out
   `pg_migration_lock_wait_seconds` surfaced as a raw
   `psycopg.errors.LockNotAvailable: canceling statement due to lock timeout` pointing at the
   `pg_advisory_xact_lock` line. The hook Job's `backoffLimit: 3` self-heals it, so the traceback
   only ever bought an operator reading a crash where the system was working as designed. The catch
   is narrow and around that one statement: the same `lock_timeout` bounds every DDL statement
   below, where `LockNotAvailable` means a *table* lock queued in front of live traffic — a
   different event that must keep its own error.

4. **The message-conversion pass reports rows, not attempts.** `convert_stored_messages` counted
   `len(updates)` — how many rows it *tried* — while the `AND message_shape = 'maf'` predicate on
   the UPDATE means a row a peer converted first matches nothing. Measured with two overlapping
   passes over three convertible rows, the configuration the module's own docstring names (no
   advisory lock, and two things can start it): **`3 + 3 = 6` reported over 3 rows converted**; now
   `3 + 0 = 3`. The data was never wrong — the predicate is what makes that true — but
   "converted 6 stored message(s)" over a table of three is a report an operator cannot reconcile
   against `SELECT count(*) … WHERE message_shape`, the one check available to them.

**What was checked and left alone.** `migrate` is correctly idempotent; the whole run is one
transaction, so a mid-run failure leaves the ledger at 0 rows; the checksum guard does not
false-positive on a rollback; and `lock_timeout` genuinely bounds `pg_advisory_xact_lock` (measured
at ~1 s against a 1 s budget, and at 2000.8 ms against 2 s by the review that expected otherwise).

**Consequences.** One extra `SELECT filename FROM schema_migrations` per migration run, under a lock
already held. One extra `SELECT EXISTS (…)` per readiness *window* per pod — not per request; the
probe is cached and single-flighted, and the newest shipped filename is one directory listing with
no file opened. A new `schema_current` verdict on `FrontDoorState` beside `database_reachable`,
seeded `True` in `create_app` for the same reason its sibling is: readiness must not refuse traffic
on the strength of never having asked.

`api/routes/ops.py` now imports `chemclaw.core.migrate`, which is the direction the layering already
allows (`api/` → `core/`) and the only way the probe can name the same "newest" the applier does —
`tests/test_migrations.py::test_the_newest_shipped_migration_is_what_a_full_run_applies_last`
holds the two to one ordering, because a probe asking about a file no full run ends on takes every
pod out of the Route.
