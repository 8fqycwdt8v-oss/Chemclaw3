# D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint — the front door keeps three pools, an early alert fires at 80% of the declared ceiling, and PgBouncer's transaction mode is for the stateless connections only

**Status:** accepted · **Date:** 2026-10-08

## Context

W3.8 asked for one pool per role and an alert when the fleet's pools approach `max_connections`.
Measured on one process of each role against a scratch database: a front door holds three pools
(the stores', `/readyz`'s own, the checkpointer's), five backends at rest and 18 at a 32-wide burst;
a worker holds one, no backend until its first query, three after, eight at eight concurrent
activities. The only call site that mints a pool beyond the role's two is `/readyz`
(`tests/test_fleet_pools.py` holds the list). With the shared pool saturated by sixteen 4-second
queries, a probe on the shared pool waited 3.04 s for a connection and the probe's own one-wide pool
answered in 0.02 s. The `ChemclawFleetAboveItsConnectionCeiling` alert already fires at 100% of the
declared ceiling.

## Options

1. **Merge the readiness pool into the stores' pool** and set the probe's statement timeout on the
   borrowed connection. This saves one connection per front door and removes the isolation: a
   saturated pool answers readiness late, and the probe then reports an overloaded pod as unready.
2. **Merge the checkpointer's pool** into the stores'. It is autocommit and its `setup()` runs
   `CREATE INDEX CONCURRENTLY`, so it needs its own connection settings and, behind a pooler, a
   session-mode endpoint.
3. **Keep the pools; add an early alert and document the pooler.**

## Decision

**Option 3.** `ChemclawFleetNearItsConnectionCeiling` fires at
`monitoring.alerts.connectionsWarningFraction` (0.8) of each declared ceiling, with the same two
per-server comparisons as the 100% alert. The ceiling is the declared `postgres.maxConnections`
that `chemclaw.fleetPools` is checked against, since the chart cannot ask the server.

PgBouncer in transaction mode is the default above three replicas. Connections that hold session
state (the checkpointer pool, the session-level advisory locks of the git writer, the checkpoint
setup and `core/job_lock.py`, and any future `LISTEN`) dial `session_store_dsn`, which is a
session-mode or direct endpoint; the migrator dials the server. `pg_advisory_xact_lock`, `SKIP
LOCKED` in one transaction and `set_config(..., true)` are safe in transaction mode. The git
writer's lock now dials the session DSN.

## Consequences

- Pool counts are unchanged: three per front door, one per worker.
- New coordination that needs a session-level lock or `LISTEN/NOTIFY` dials
  `settings.session_store_dsn or settings.postgres_dsn` on a dedicated connection.
- The `options` startup parameter carries the statement bounds and PgBouncer refuses or drops it, so
  the same values go on the database role (`deploy/README.md`).

Revisit when: `/readyz` is answered without borrowing a connection, or the pool library gives a
reserved connection per caller class; then option 1 saves a connection per pod without losing the
isolation that `tests/test_fleet_pools.py` and the probe-isolation measurement hold.
