# D-2026-08-05-a-worker-may-not-outrun-its-pool — a worker may not admit more activities than its pool can serve

**Status:** accepted · **Date:** 2026-08-05

## Context

Neither `Worker(...)` call in this repository set `max_concurrent_activities`:

- `durable/background_worker.py` — core's `background-jobs` worker.
- `connectors/worker.py` — the one constructor every bundle's worker goes through (D-118).

temporalio's default is **100 concurrent activities**. The Postgres pool those activities borrow
from is `pg_pool_max_size`, which the shipped chart now sets to 8
(D-2026-08-05-the-connection-budget-is-a-fleet-number) and which was 16 before that. So a worker
could admit an order of magnitude more concurrent work than it had connections for.

**The reason this was invisible is that it does not fail as a shortage.** `db.connection` raises
`ConnectionError` after `pg_pool_timeout_seconds`, `ConnectionError` is deliberately absent from
`publish._BAD_DATA_TYPES` (D-119: pool exhaustion and an unreachable database are one transient
infrastructure fault from the caller's side), so Temporal retries the activity and the work
eventually completes. What is lost is not correctness but the shape of the failure:

- A starved activity burns one of `activity_max_attempts` **before it has computed anything**, so a
  pool shortage consumes the retry budget that exists for network blips. Five attempts is not many
  to spend queueing.
- The queue does not push back. Temporal's own backpressure mechanism *is* the concurrency limit —
  unset, the worker keeps taking tasks it cannot start, and the signal an operator would read
  (`chemclaw_pg_pool_requests_waiting`) was not exported by a worker process at all until the ADR
  above bound it there.
- `background-jobs` is the worst place for this, because its work is almost entirely database
  work: the retention sweep, the note reindex, the audit-chain verification, every job record.
  Those are also the three longest-running database operations in the deployment.

The chart already sized the `qm` bundle's *memory* for "one activity slot per in-flight job with an
open Nextflow poll for up to 24 h" — so the concurrency that mattered had been reasoned about for
one bundle, in a comment, next to a knob that did not exist.

## Decision

**`worker_max_concurrent_activities`, defaulting to `pg_pool_max_size`'s shipped value, set on both
`Worker(...)` constructors.**

Equal to the pool, not below it, and that is the whole of the sizing argument: an activity borrows
a connection for a fraction of its runtime, so a ceiling at the pool's width already leaves the
pool mostly idle. Going under it would cap throughput on a resource that is not the constraint;
going over it is where a shortage becomes retry churn. Equal is the point at which no activity can
ever be the one that has to wait.

**A bundle whose activities are waits rather than work overrides it in the chart.** `qm` sets
`maxConcurrentActivities: 32` beside the `resources` block that already explains why it is shaped
differently: a QM activity touches Postgres twice — a cache lookup and a result write — across as
much as 24 hours of polling, so what bounds it is the pod's memory, not the connection pool. The
override is a per-bundle key on the same `connectors:` entry that already carries `resources`,
because a capability shaped differently from the rest says so in its own entry rather than adding a
key to a global block.

`tests/test_worker_observability.py` asserts both halves: that each entrypoint passes the setting
(source-level, beside the existing `serve_worker`/`graceful_shutdown_timeout` assertions, because
starting a real worker needs a broker), and that the shipped default does not exceed the shipped
pool.

## Consequences

**Throughput on `background-jobs` is now explicitly 8, where it was nominally 100 and effectively
whatever the pool allowed.** That is a reduction on paper and not in practice — the previous 100
could not be served — but it is a real ceiling now, and a deployment that genuinely needs more must
raise the pool and the fleet budget with it, which is the conversation this makes possible.

**One more pair of numbers that only mean anything together.** `CHEMCLAW_PG_POOL_MAX_SIZE` and
`CHEMCLAW_WORKER_MAX_CONCURRENT_ACTIVITIES` are stated adjacently in `values.yaml` and in
`.env.example` for that reason, and the test fails if they drift apart in the shipped defaults.

**Workflow task concurrency is left at temporalio's default,** deliberately. A workflow task is CPU
and replay, not database work; it borrows no connection, so it is outside the invariant this ADR is
about. Bounding it would be a different decision needing a different measurement.
