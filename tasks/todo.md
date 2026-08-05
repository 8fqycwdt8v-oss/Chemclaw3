# Task: deep review of the database integration concept

Branch: `claude/database-integration-review-zez7zo`.

The review found the connection pool's *demand* side unbounded, invisible and undeclared, and the
database with no privilege model at all. What held up under measurement is recorded below too,
because a review that lists only defects misrepresents the thing reviewed.

_(The previous occupant of this file was the deep review of the agentic engine, harness and
deep-research path (#128, `docs/decisions/D-2026-08-05-one-rule-in-three-places-is-three-rules.md`),
which landed on `main` while this branch was in flight; before that the tool/skill seam (#129) and
the live-test lane for Temporal + durable workflows (#124/#127). All are in `git log`.)_

---

## Plan

- [x] **1. The connection budget is a fleet number.** `pg_fleet_pooled_processes` ×
      `pg_pool_max_size` ≤ `pg_fleet_max_connections`, checked in `Settings` beside the turn
      ceiling; `chemclaw.pooledProcesses` derives the count from the topology the chart renders;
      `pooling()` binds the pool gauges so every pooled process reports on its pool;
      `ChemclawPgPoolSaturated` and `ChemclawFleetAboveItsConnectionCeiling`.
      ADR `D-2026-08-05-the-connection-budget-is-a-fleet-number`.
- [x] **2. A worker may not outrun its pool.** `worker_max_concurrent_activities` on both
      `Worker(...)` constructors; temporalio's default of 100 against a pool of 8 turns saturation
      into retry churn instead of backpressure.
- [x] **3. Readiness answers for the store it cannot serve without.** A bounded, cached database
      probe in `/readyz`, gated on `session_store="postgres"`; `/healthz` untouched.
- [x] **4. A sweep that commits once can lose everything it did.** Per-session commit in
      `_prune_session_messages`; a bounded batch per pass, with the remainder reported.
- [x] **5. Append-only by grant, not by contract.** `postgres_migration_dsn`; the runner moves to
      `chemclaw.core.migrate`; `infra/sql/grants/` applied by `make db-grants` (*not* a numbered
      migration — see the review below); `tests/test_database_privileges.py` derives the matrix
      from `src/`; the migration credential mounted only on the hook Job.
- [x] **6. The record.** `infra/sql/README.md` inventory checked by `tests/test_schema_inventory.py`
      in both directions; BACKLOG rows for the unlisted retention tables, the `session_owners`
      orphan and the audit-append ceiling; closed the two stale rows; fixed `bit_hamming_ops` and
      the Appendix B migration numbers.

## Measured, and not defects

Recorded so they are not re-litigated:

- **SSE polling against the pool.** 200 streams ÷ `session_event_poll_seconds=2.0` = 100 borrows/s
  per front-door process, each a sub-millisecond indexed `SELECT` on
  `session_events_unconsumed_idx` ≈ 0.1 connection-seconds/s. The pool is not the constraint; the
  event loop is, which is D-119's original finding.
- **The audit chain's global advisory mutex.** Every append across the fleet serializes on
  `pg_advisory_xact_lock(0x43484D4157_00_01)` for ~4 round trips, so the ceiling is a few hundred
  appends/s deployment-wide — far above current demand, and correct by design, since a forked chain
  cannot be repaired. A ceiling worth stating, not a defect worth fixing.
- **The SQL surface itself.** Every application statement binds its values; the four sites that
  interpolate an *identifier* are each guarded (a closed `_PRUNABLE` map, `table.isidentifier()`,
  an int from config, a validated identifier regex), and the one un-parameterized surface — a
  warehouse binding's `where:` — is a documented operator-authored trust boundary.

## Review

**What the review actually changed.** Five defects, each verified against the tree before being
believed and each pinned by a test that fails without its fix (checked by reverting the source file
and re-running): the fleet connection ceiling, the unbounded worker concurrency, the pool gauges
missing from eleven of seventeen pooled processes, the readiness probe that skipped the store it
cannot serve without, the retention sweep that committed once, and the absent privilege model.

**Two things the plan got wrong, corrected while building.**

*The grants started as a numbered migration.* They cannot be. `infra/sql/*.sql` is applied exactly
once per file and tracked by checksum, which is right for a schema change and wrong for a grant in
two ways at once: a deployment creating its runtime role after the first `db-migrate` would never
have grants applied, and every table added by a later migration would ship ungranted and break the
application on first use of it. Caught by applying it to a live Postgres, creating the role, and
watching the second `db-migrate` do nothing. Now `infra/sql/grants/`, re-applied on every deploy by
`make db-grants`, invisible to the runner's non-recursive glob by construction.

*The grant matrix was written by hand and was wrong three times.* `tests/test_database_privileges.py`
derives it from the SQL literals in `src/` and caught all three: `turn_costs` upserts and so needs
`UPDATE`, and `job_records`/`note_index` were reported as over-grants until the derivation learned
to read f-string SQL — which is exactly where the two genuine upserts had been hiding from it. That
is the argument for deriving rather than maintaining, made by the thing itself on its first run.

**One test-suite property surfaced and worked around rather than fixed.** The two new retention
tests initially failed only when run with the rest of their file: the sweep selects expired sessions
globally, so rows another test left behind land inside the batch under test. The suite isolates one
schema per *run*, not per test — a known limitation (`tests/pg.py`; BACKLOG LIVE-6). The helper now
clears the table it counts, which is correct locally; the general fix is still LIVE-6's.

**What was checked and deliberately not changed.** Recorded above under "Measured, and not
defects": the SSE poll rate (0.1 connection-seconds/s — the event loop is the constraint, not the
pool), the audit chain's fleet-wide append mutex (a few hundred appends/s, correct by design, now a
BACKLOG row so it is not rediscovered as a bug), and the SQL surface itself (every value bound;
every identifier interpolation guarded).

**What remains open**, as BACKLOG rows rather than as work done badly: the eight tables retention
neither prunes nor refuses, the `session_owners` row that outlives its session's history, and the
backup/ownership question for a Postgres this chart does not deploy. Each needs a *policy* decision
first, and inventing one inside a sweep is precisely what this module's three documented refusals
exist to prevent.

**A note on what could not be verified here.** `make helm-validate` needs `helm`, whose download the
sandbox proxy blocks, so the chart's template changes are covered by `tests/test_deploy_chart.py`
and `tests/test_helm_chart.py` (which read the templates as text) and by CI's separate `chart` job —
not by a local render. Everything else, including the privilege boundary, was verified against a
real Postgres 16 + pgvector 0.8.0 running in this session.
