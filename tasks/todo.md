# Task: deep review of the database integration concept

Branch: `claude/database-integration-review-zez7zo`.

The review found the connection pool's *demand* side unbounded, invisible and undeclared, and the
database with no privilege model at all. What held up under measurement is recorded below too,
because a review that lists only defects misrepresents the thing reviewed.

_(The previous occupant of this file was the live-test lane for Temporal + durable workflows + LLM
+ Postgres, `docs/decisions/D-2026-08-04-a-lane-that-only-runs-where-docker-runs.md`; it is in
`git log`.)_

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
      `core/`; `infra/sql/036_privileges.sql`; a test deriving the grant matrix from `src/`; the
      migration credential mounted only on the hook Job.
- [ ] **6. The record.** `infra/sql/README.md` inventory checked by a test; BACKLOG rows for the
      unlisted retention tables, the `session_owners` orphan and the audit-append ceiling; close
      the two stale rows; fix `bit_hamming_ops` and the Appendix B migration numbers.

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

(filled in at the end)
