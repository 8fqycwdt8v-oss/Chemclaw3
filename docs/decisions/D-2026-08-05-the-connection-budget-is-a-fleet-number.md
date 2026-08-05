# D-2026-08-05-the-connection-budget-is-a-fleet-number — the connection budget is a fleet number, and the pool's witness belongs to the pool

**Status:** accepted · **Date:** 2026-08-05

## Context

`core/config/store.py` has carried this sentence since the pool landed (D-119):

> `max_size` bounds one process; the deployment total is `max_size × distinct DSNs × processes`
> (front-door replicas × `service_uvicorn_workers`, plus the workers), which must stay under the
> server's `max_connections`.

Nothing computed the left-hand side. The Helm chart set **no** pool key at all, so every pod ran
the code default `pg_pool_max_size=16`, and the shipped values render **seventeen** processes that
call `chemclaw.core.db.pooling` — the front door at `autoscaling.maxReplicas: 6`, the background
worker, six connector servers, and four connector workers (`calc`, `bo`, and `qm` at two). The
fleet's ceiling was therefore **272 connections**, against the `max_connections=100` the load test
behind D-119 measured against. Even at rest the floor was 17 × `pg_pool_min_size` = 34 sockets held
open.

This is the same shape as the admission cap before `service_fleet_max_concurrent_turns`: a
per-process bound that is correct inside every pod while the fleet total is not, and which no
single pod can see. That one was found and fixed; this one sat beside it in the same file.

**The second half was worse, because the signal already existed.** D-119 introduced
`chemclaw_pg_pool_requests_waiting` precisely because an undersized pool and an unreachable
database are indistinguishable from the outside — the failure it was written for was connect
timeouts against an *idle* server. `core/metrics.py` says so at the declaration. And:

- All three pool gauges were bound in `api/app.py`. In fact **every** `bind_gauge` call in the tree
  was in `api/app.py` — so the eleven of seventeen pooled processes that are not the front door
  served `/metrics` with no pool reading of any kind. That includes the background worker, which
  runs the retention sweep, the note reindex and the audit-chain verification: the three longest
  database operations in the deployment.
- No `PrometheusRule` consumed `chemclaw_pg_pool_requests_waiting` anywhere. The signal was
  collected, tested (`tests/test_db_pool.py::test_pool_saturation_is_visible_as_a_gauge`), and
  never watched.

D-2026-08-01-every-process-carries-its-own-witness made exactly this argument for probes and
metrics surfaces, and gave every worker and connector server a `/metrics` endpoint. It left the
pool behind: the endpoint arrived, the reading did not.

## Decision

**The connection budget is declared, checked at startup, and watched at runtime — the same two
halves the turn ceiling has, and deliberately not a third idiom.**

`pg_fleet_pooled_processes` × `pg_pool_max_size` must not exceed `pg_fleet_max_connections`. The
check lives beside the turn-ceiling check in `Settings._guards_that_the_comments_already_demand`,
raises with both numbers and both levers in the message, and is inert while the ceiling is `0` —
the same self-disabling convention, for the same reason: a laptop has one process and no fleet, and
a guard that fires there is a guard people switch off in production too.

The chart derives the process count in `chemclaw.pooledProcesses` from the same values that render
the Deployments, exactly as `chemclaw.connectorUrls` derives the address map from the same enabled
set as the connector Services. A hand-written count is a second declaration of the topology, and
this chart has already watched one of those go stale.

**`pooling()` binds the pool gauges.** A process cannot acquire a pool without acquiring its
witness. This is the rule, not the current call sites: it holds for the next process that pools,
without anyone remembering to add a line.

**Two alerts.** `ChemclawPgPoolSaturated` on `max(chemclaw_pg_pool_requests_waiting) > 0` — `max`
and not an average, because one saturated process is one process whose callers are queueing and
averaging that across a healthy fleet is how a per-pod saturation stays invisible. And
`ChemclawFleetAboveItsConnectionCeiling` comparing `sum(chemclaw_pg_pool_max_size)` against the
declared gauge, which is the only thing that can see a `kubectl scale`, an in-cluster HPA edit, or
a rollout leaving both generations up — startup validation checks the shape the chart rendered and
never re-runs.

## What the shipped values now say

`CHEMCLAW_PG_POOL_MAX_SIZE: "8"` and `postgres.maxConnections: 136`, which is 17 × 8 exactly. The
number ships as a *statement of the current shape rather than as slack*, the same way
`CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS: "48"` does, and
`tests/test_deploy_chart.py` recomputes it from the topology so the chart cannot ship values its
own validator would reject.

8 rather than 16 because 16 was chosen for one process and this chart runs seventeen. It matches
the front door's own admission cap: no turn holds a connection across the model call, so a pool the
width of `service_max_concurrent_turns` already carries headroom.

**136 is a provisioning requirement and is stated as one.** A stock Postgres ships
`max_connections=100` and will not serve this release at full scale. That was true before this ADR
too — the difference is that it is now said out loud in `values.yaml`, checked in every pod at
startup with both numbers in the message, and alerted on, instead of being discovered under load as
connect failures against a server that is not busy.

## Consequences

**A deployment that scales past its database now fails at startup rather than under load**, in
every pod, naming the product and both levers. That is a louder failure than the one it replaces
and a much cheaper one: D-119's presentation was 32 connect timeouts with the database idle, and
the guard it silently disarmed was the rollback watermark (D-107).

**One more number an operator has to keep true.** `postgres.maxConnections` is a claim about
someone else's server — this chart deploys neither Postgres nor Temporal (`docs/planning/BACKLOG.md`)
— so it can be wrong in the one direction validation cannot see: declared higher than the server
will actually serve. The runtime alert does not close that either. What closes it is the ownership
row in the backlog, and this ADR does not pretend otherwise.

**`api/app.py` is no longer the only place gauges are bound**, which is the point. The front door
still binds its own — turns in flight, live sessions, connector health — because those describe
structures only it has.

**Not decided here:** what bounds the *demand* on a worker's pool. A Temporal worker with no
activity concurrency limit can ask for far more connections than it can borrow regardless of how
the budget is declared; that is D-2026-08-05-a-worker-may-not-outrun-its-pool.
