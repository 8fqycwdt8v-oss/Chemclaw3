# D-2026-08-01-a-migration-waits-in-front-of-live-traffic — A migration that waits, waits in front of live traffic

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-034 (migrations as a pre-deploy hook),
D-148 (the migration directory is a setting)

## Context

`science/calc/migrate.py` applies DDL to the configured database, and until now it did so with
neither of the two locks that kind of code needs.

**No serialization.** `infra/sql/011` documents that the *audit writer* takes a transaction advisory
lock so two concurrent inserts cannot read the same chain tip and fork it. The migrator — which
issues `CREATE TABLE`, `ALTER TABLE` and `CREATE INDEX` — took none. That contrast is what made the
gap conspicuous rather than theoretical: the subsystem appending rows was serialized and the one
changing the schema was not.

**No lock timeout, and this is the one that can cause an outage.** The module connects deliberately
without a `statement_timeout`, correctly, because an index build may run long. But `statement_timeout`
was never the relevant bound. An `ALTER TABLE` needs `ACCESS EXCLUSIVE`; Postgres's lock queue is
**FIFO**; so a lock request that queues behind one long-running read blocks *every later query on
that table behind it*. A migration run against a live database therefore does not merely wait — it
takes the table down for as long as the slowest open query lasts, from a Job the operator started
expecting a schema change. That is a well-known production failure and nothing here bounded it.

**No deadline on the hook.** The Job is a `pre-install,pre-upgrade` Helm hook with `backoffLimit: 3`
and no `activeDeadlineSeconds`. Helm waits for a hook, so a Job that keeps retrying leaves the
release in `pending-upgrade` — a state that blocks the next `helm upgrade` and whose recovery was
documented nowhere.

A fourth finding turned up while reading the module. Its own docstring said migrations "run at
service startup (the front door and each worker migrate before serving)". Nothing has ever done
that: `migrate()` has exactly one caller, its own `__main__`. The false sentence mattered *here*,
because it made concurrent migration sound like a routine per-process event, when the real
concurrency is two overlapping deploys or an operator running `make db-migrate` during one.

## Decision

**Two locks with two budgets, far apart, because they bound opposite things.**

```
set_config('lock_timeout', <300s>)   -- waiting for a peer migrator is legitimate
pg_advisory_xact_lock(<key>)         -- serialize
set_config('lock_timeout', <5s>)     -- waiting in front of live traffic is not
<the ledger, then every unapplied file>
```

- **`pg_advisory_xact_lock`**, transaction-scoped. The whole run is already one transaction
  (Postgres DDL is transactional and there is a single commit at the end), so the lock's span is
  exactly right and it is released by the commit, the rollback, *or the connection dropping* —
  there is no path that leaks it and nothing for an operator to clean up after a crashed migrator.
  The second migrator waits, then finds every file recorded in `schema_migrations` and applies none.
- **`lock_timeout`, not `statement_timeout`.** It bounds the *wait* and leaves the *work* unbounded,
  which is the correct shape: a `CREATE INDEX` may legitimately build for minutes once it holds its
  lock, and capping that would break migrations that are behaving correctly. The module still
  connects with no statement timeout.
- **`activeDeadlineSeconds` on the Job**, so a failing migration ends as a reported failure rather
  than an open-ended release, with the recovery written down (`runbook.md` §(xi)).

## Why not the alternatives

**One timeout for both waits.** It cannot be right for both. At 5 s, an ordinary concurrent
`helm upgrade` fails on the advisory lock — waiting for a peer is a normal event and the correct
response is to wait for it. At 300 s, one `ALTER TABLE` sits in the lock queue for five minutes with
every later query on that table behind it, which is the outage the timeout exists to prevent. The
two settings encode that the two waits are not the same kind of thing.

**`pg_try_advisory_xact_lock` and fail fast.** Refuses a legitimate concurrent deploy instead of
waiting three seconds for it, and turns a benign race into a failed release. Blocking-with-a-bound
gets the same safety and the better default.

**A session-level `pg_advisory_lock`.** It would outlive the transaction and need an explicit
unlock, which means a code path that can leak it — precisely the state that would make a *dead*
migrator wedge the next release. The transaction-scoped variant has no such path.

**Deriving `activeDeadlineSeconds` from the lock budgets**, as the previous ADR derives the pods'
grace periods from the turn and drain budgets. Tempting for consistency and unsound here: this
bounds the Job *including* every retry, and the term it needs — how long this deployment's slowest
`CREATE INDEX` takes on its own data — is not a number the chart can know. A stated default an
operator raises is more honest than a formula that pretends to compute one. The chart says so where
the value is set, rather than leaving the inconsistency to be noticed.

**Raising the lock timeout when a migration fails on it.** Named in the runbook as the thing *not*
to do, because it is the obvious move and it converts a failed deploy into an outage. The fix is to
find the session holding the lock.

## Consequences

- A migration that cannot get a table lock fails in ~5 s and retries, instead of queueing in front
  of every query on that table.
- Two migrators are safe: the second waits and applies nothing.
- A stuck release is now a failed Job with logs and a documented recovery.
- `migrate.py`'s docstring no longer claims a startup path that does not exist. Its one caller is
  its `__main__`, run by `make db-migrate` and by the Helm hook.
- The lock ordering is pinned by tests that run offline against a recording connection rather than a
  database. That is deliberate: the property that can be wrong is the *sequence* — which budget is
  in force when — and it is silently wrong against a live database and invisible against an idle
  one, so an integration test on an empty CI database could never have caught it.
