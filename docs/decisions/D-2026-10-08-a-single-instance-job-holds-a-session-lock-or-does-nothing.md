# D-2026-10-08-a-single-instance-job-holds-a-session-lock-or-does-nothing — the background worker scales to any replica count, and the one pass that must not overlap itself takes a Postgres advisory lock

**Status:** accepted · **Date:** 2026-10-08

## Context

`workers.background.replicas` was 1, a single point of failure for retention, reindex, sync, digests
and memory jobs. `D-2026-08-27-what-a-second-background-worker-would-race-on` audited the queue and
found one job unsafe at two replicas, the note reindex, and the later fixes (`D-2026-09-14-a-prune-needs-the-corpus-two-pods-disagree-about`,
`D-2026-09-16-a-fingerprint-that-names-a-checkout-is-not-a-fingerprint-of-a-note`) closed its two halves. The pin outlived them because nobody had driven two.

Re-inventorying what the process runs (W3.5): fifteen periodic jobs are Temporal Schedules under
`SKIP`, so the server allows one run each whatever the worker count; the Schedules are applied by a
Helm hook Job, not by a worker; the only in-process loop is a read-only gauge refresh; every other
activity is idempotent or claim-based (the outbox claims with `FOR UPDATE SKIP LOCKED`, cursors
advance with `GREATEST`, retention re-checks its predicate inside each `DELETE`, the git writer
and the checkpoint setup already take advisory locks). What remains is a pass that overlaps itself
rather than a peer: the reindex run by hand (`python -m chemclaw.retrieval.vector_index`) beside
the scheduled one, or an attempt retried while a partitioned pod's attempt still runs. Overlapping
costs embedding calls twice and writes the same rows twice.

## Options

1. **Leave it at SKIP.** The server serialises Schedules, so nothing else is needed. A hand-run or
   zombie pass still overlaps.
2. **A Postgres advisory lock tried, never awaited** (`core/job_lock.py`). A pass that finds it held
   does nothing; the lock dies with the holder's connection, so a killed pod needs no cleanup. One
   connection held for the pass, no table, and the same mechanism the git writer uses.
3. **Partition the work** with per-batch `FOR UPDATE SKIP LOCKED` claims. The index is a derived
   table with no claimable unit of work, so this needs a queue table to claim from.
4. **Compare-and-set the upsert on `corpus_commit_count`**, so a pod on an older checkout cannot
   overwrite a newer row. For `ExternalVectorNoteIndex` the vector is written to the store before the
   catalogue row, so a refused catalogue write leaves a stale vector under a fingerprint that
   matches the new text, and the note is never re-embedded.

## Decision

**Option 2 for the reindex, option 1 for everything else.** `exclusive_job(name)` yields whether the
caller holds the lock and counts `chemclaw_job_lock_skipped_total{job}` when it does not;
`reindex_exclusively` (the activity and the command line) returns 0 when it is not held. The key folds in `current_schema()` so deployments
sharing a database do not contend. The connection comes from the session layer's DSN, so behind a
transaction pooler it is the session-mode endpoint (`D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint`).
The chart ships two replicas with a `minAvailable: 1` PodDisruptionBudget, rendered only from two,
and keeps `Recreate`, which depends on replay compatibility and not on the replica count.

Option 4 is declined: it protects the pgvector backend and corrupts the external one, and the stale
write it prevents is bounded by the knowledge sync interval.

## Consequences

- Alerts over a per-pod gauge must read the freshest pod: `ChemclawIngestCursorStalled` now takes
  `min by (source)`, since a replica that did not run the last sync reports an ever-growing lag.
- `chemclaw_job_lock_skipped_total` is on the durable dashboard; steady skips beside a pass that
  never completes mean a wedged holder.
- Held by `tests/test_job_lock.py` (separate processes against Postgres: two reindexers embed each
  note once, a SIGKILLed holder's lock is taken over) and the chart tests (two replicas, the PDB,
  its absence at one).

Revisit when: a measured run of `tests/test_job_lock.py`-style concurrent reindexers on the external
backend shows rows written backwards by an older checkout, or `ExternalVectorNoteIndex` writes the
catalogue row before the vector.
