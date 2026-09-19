# D-2026-09-19-the-ledgers-own-argument-splits-its-two-branches — an absent migration ledger drains the pod; an unreadable one does not

**Status:** accepted · **Date:** 2026-09-19 · **Supersedes, in part:**
D-2026-09-09-a-migration-run-reports-what-it-applied-not-that-the-schema-matches (its readiness
branch only; the asymmetric schema-behind check and the migrator's own reporting stand unchanged)

## Context

`D-2026-09-09` decided that `/readyz` "gates on positive evidence only", and named both shapes it
admits in one sentence: *"A ledger that cannot be read at all — no `schema_migrations`, or a role
that cannot select it — stays ready and logs."* Both were implemented as one `except` clause
returning ready.

Measured on an un-migrated database under the chart's own shipped `CHEMCLAW_SESSION_STORE=postgres`:

```
$ curl -s .../readyz
{"status":"ready","connectors_unhealthy":8}      HTTP 200
WARNING readiness: cannot read schema_migrations, so the schema is not being checked against
  this image (newest shipped: 106_drop_the_index_nobodys_query_uses.sql)
psycopg.errors.UndefinedTable: relation "schema_migrations" does not exist
```

The pod joins the Route and fails every session write. Reachable by the three paths that ADR's own
neighbouring docstring names — `--no-hooks`, `kubectl set image`, an ArgoCD sync past a failed hook —
and by the two-releases-one-database hazard the chart's `temporal.namespace` error message admits no
guard can cover.

## Decision

**`UndefinedTable` makes the pod unready. `InsufficientPrivilege` keeps it ready.** Two branches,
two log lines, matching the shape the schema-*behind* case already answers with (`503
{"status":"schema behind image"}`, naming the migration and both remedies).

## Why this is a re-decision and not a defect fix

It would be convenient to call this a defect — the implementation admitting more than the argument
justified — and that reading is available: the paragraph's reasoning is entirely about a *split
session store*, where the probe reads `session_store_dsn or postgres_dsn` and therefore a different
server from the one `migrate()` ran against, so a refusal would be "being right about somebody
else's operations" at the wrong moment.

But the sentence names the missing table explicitly, and the experiment recorded beside it flipped
exactly the `UndefinedTable` branch. The decision was taken, not overlooked. So this supersedes it
rather than repairing it, per this repository's rule that a changed decision gets a new record.

**What changes the answer is following that same argument one step further.** The ADR justifies
staying ready on the ground that *"every database that can serve as the session store was migrated
by this same runner and so carries the ledger"*. Take that seriously and it splits the two shapes it
grouped:

- a role that cannot `SELECT` the ledger is a **privilege** fact about a database that does carry
  one — the split-session-store case exactly, and the pod should serve;
- a database with **no ledger at all** is, by the ADR's own premise, a database this runner never
  migrated. That is not somebody else's operations being opaque; it is positive evidence that the
  expected migration did not happen here.

So the argument that was offered for both supports only one, and supports refusing on the other.

## What this costs, stated

A deployment whose session store legitimately holds no `schema_migrations` — some future store this
runner does not migrate — would now drain. Nothing in the tree is that today, and the failure is
loud, bounded to readiness rather than a crash-loop, and undone by the next passing probe. That is
the trade `D-2026-09-09` chose in the other direction on the same page, for the schema-behind case,
and the reasoning there applies unchanged.

**Revisit when:** a session store that this repository's migrator does not own is added — that is,
when `session_store_dsn` may legitimately point at a database with no `schema_migrations` table. The
file that would show it is `core/config/database.py`; the test that would red is
`tests/test_service.py::test_a_database_with_no_migration_ledger_takes_the_pod_out_of_the_route`.

## What keeps it true

- `tests/test_service.py::test_a_database_with_no_migration_ledger_takes_the_pod_out_of_the_route`
  — rewritten from the test that asserted the opposite, which is why the old citation is in
  `_RETIRED_TEST_CITATIONS` with this ADR's id beside it.
- `tests/test_service.py::test_a_ledger_this_role_may_not_select_does_not_take_the_pod_out_of_the_route`
  — **new, and the half that had no test at all.** `D-2026-09-09`'s privilege case was proved by a
  missing *table*, which is the branch this record moves; it is now driven through a real
  `InsufficientPrivilege` — a real ledger, a real `NOLOGIN` role holding `USAGE` and no `SELECT`,
  reached with `-c role=` in the DSN and dropped in the `finally`.
- Four mutations watched failing, one a deletion and three rewords: both exceptions folded back into
  one ready branch; the split inverted; the refusal widened to `psycopg.Error`, which the privilege
  arm catches; and the missing ledger logged without being acted on.
