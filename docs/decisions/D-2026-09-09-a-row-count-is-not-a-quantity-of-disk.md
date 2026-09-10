# D-2026-09-09-a-row-count-is-not-a-quantity-of-disk — A row count is not a quantity of disk

**Status:** accepted · **Date:** 2026-09-09

## Context

The retention sweep's row-level register was tested against 14 adversarial cases — straddling
tool-call pairs, orphaned messages, unconsumed events pinning owners, FK-protected blobs, three-deep
checkpoint chains, live leases — and **every `_PRUNABLE` table was reached exactly as documented**.
That half is sound and this ADR is not about it.

What nothing measured is bytes. One `_sweep_once`:

```
1,900 rows deleted   ·   total relation size 2,646,016 B before and after   ·   0 reclaimed
                                                     1,919 dead tuples left behind
```

`RetentionOutcome.deleted` is a **row count**, which an operator watching a filling disk reads as
progress. The word `VACUUM` appeared nowhere in `src/`, `infra/`, `deploy/` or the runbook outside
EXPLAIN commentary; the sweep ran `ANALYZE checkpoints` and nothing else, leaving reclamation to an
autovacuum this repository neither configures nor checks. And the module's own argument that no
migration can reach the checkpoint tables — `setup()` creates them after migrations run — applies
verbatim to per-table autovacuum storage parameters, so the three highest-churn tables in the system
are exactly the ones no migration can tune.

Steady state, six cycles of *insert 500 / sweep 500* with the live set constant at 500 rows:

| | cycle 1 | cycle 6 |
|---|---|---|
| without a vacuum pass | 319,488 B | **1,277,952 B** (4.0×, 3,000 dead) |
| with it | 327,680 B | **344,064 B** (flat from cycle 2, 0 dead) |

**A sweep without it does not bound growth at all.** And nothing anywhere could see that:
`core/metrics.py` had zero series matching retention, disk, table size or prune; `retention.py`
imported no metrics module; 28 alert rules, none about the store filling; and of twelve Temporal
Schedules only two had liveness alerts — `retention` and `artifact-eviction`, the only two disposal
jobs, had neither.

## Decision

**The sweep vacuums what it deleted from, and it is not opt-in.** `VACUUM (SKIP_LOCKED)` over the
five swept tables costs 27.1 ms. Three measured reasons for making it unconditional: a plain
`VACUUM` takes `SHARE UPDATE EXCLUSIVE` and blocks neither readers nor writers, with `SKIP_LOCKED`
turning an unavailable table into a skip rather than a wait; a role that may not vacuum a table gets
a `WARNING` and the command still succeeds, verified against a role holding only SELECT/DELETE —
the posture that already licenses this module's unconditional `ANALYZE`; and the measurement above
says an off-by-default knob behind an already-off-by-default `retention_enabled` ships the defect.

**`chemclaw_table_bytes{table}` is published on every pass**, for every table the register names —
`_PRUNABLE | _NOT_PRUNED`, because the table filling the volume is often one nothing prunes — and
republished even by a pass that disposed of nothing, because its absence is the alert.
`ChemclawRetentionNotSweeping` is that absence, on the `ChemclawOutboxBacklogUnreported` pattern,
rendered only for a release that states `retention.windows`.

**Two of the three proposed series were written and then dropped**, which is a result rather than a
shortcut. Reclaimed bytes is **structurally near-zero**: retention deletes the *oldest* rows, which
sit at the front of the relation, and a plain `VACUUM` truncates only trailing pages — every
measurement reclaimed 0 bytes to the filesystem while making the space reusable, and a counter that
is almost always 0 is not a signal. The row count is already in `RetentionOutcome`. And neither
could be given a rule that fires only on a fault: three were drafted and each fires on a healthy
deployment — "a pruned table is growing" fires on one that is simply growing, and "the sweep
deleted nothing this week" fires *permanently* on a young deployment with a 365-day window. The
repository's own `test_every_declared_metric_has_a_consumer` is right; the consumer for a rate
counter is a dashboard panel, not a rule.

**The orphan repair ships.** `checkpoint_blobs`/`checkpoint_writes` rows for a thread with no
`checkpoints` row were reached by neither pass — both are restricted to candidates drawn from
`checkpoints` — so they were permanent *and* pinned their session's ownership row forever, the exact
outcome the ordering rule exists to prevent. One `ctid`-batched unrestricted anti-join per pass:
**43.9 ms with nothing to repair** at 50,000 live threads, three orders below the timeout.

The trade is stated: the safety premise is that no live thread holds blob or write rows before its
`checkpoints` row. That is already measured in this module — `aput` runs its statements in a psycopg
pipeline, and a pipeline on an autocommit connection is one transaction — and it is the same premise
`_DELETE_ORPHANED` already rests on. It is **upstream's behaviour, not an invariant this repository
enforces**, so a future LangGraph release that stops pipelining would let this pass take a live
thread's blobs. A whole live thread sits beside the orphan in the fixture for exactly that reason.

**`_NOT_PRUNED["store"]` now says nothing bounds it.** It read "erasure reaches it per actor" — a
disposal route that fires only on a leaver request, which is the reasoning the register's own
`session_owners` entry rejects in its own words ("which a deployment that no one leaves never
runs").

## The proposed `work_mem` fix was measured worse

600,000 audit rows, one-year window:

| | plan | time |
|---|---|---|
| shipped `count(DISTINCT actor)` | `GroupAggregate ← Sort`, external merge, 25,456 kB | 1,581.8 ms |
| `+ SET LOCAL work_mem = 64MB` | quicksort, 52,933 kB **in memory** | **2,005.5 ms** — 27% *slower*, 53 MB per caller |
| split DISTINCT | `HashAggregate`, Batches: 1, 337 kB, no temp | **176.0 ms** — 9.0× |

The disk spill was a symptom; sorting 600,000 rows to answer a 24-row question was the cost.

## Left open

`durable/artifact_eviction.py`, the other disposal job, publishes no metric and so still has no
liveness signal. A genuine "the store is filling" *threshold* alert needs a configured budget and a
`predict_linear` rule; every thresholdless form measured fires on a healthy deployment.
