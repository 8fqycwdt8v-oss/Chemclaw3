# D-054 — Per-source ELN cursors + a per-scope token lock (close the two F-review deferrals)

**Context.** Two consciously-deferred items from the F4–F7 review (D-051) were re-examined under a
"close all found gaps" pass and found genuinely implementable offline against the *existing*
contracts — no live infrastructure, no speculative abstraction:

1. **Shared ELN cursor (F7 review F-1/F-2).** The durable sync tracked one high-water cursor
   (keyed by the now-dead `eln_sync_adapter` label) while F7/DUP-1 made a *multi*-ingest-source
   config reachable. Two sources whose newest entries differ would let the furthest `max()` cursor
   skip the lagging source's entries — silent data loss. D-053 shipped an interim fail-fast guard
   (>1 ingest source → non-retryable error); this ADR removes the guard and does the real fix.
2. **Thundering-herd token exchange.** On a cold/stale cache, N concurrent
   `WorkloadTokenProvider.get_service_token(scope)` callers each fired the federation exchange —
   correct (never a stale token) but wastefully redundant.

The deferral reasoning for (1) was "wait for the second real source (Snowflake), which brings its
own pipeline cursor." Re-checked: **both** current ingest adapters are datetime-cursored because the
`ElnAdapter` contract *is* `fetch_new_entries(since: datetime)`. Per-source datetime cursors is
therefore the faithful generalization of the contract that exists today, not a guess about a source
that doesn't. A future non-datetime cursor source would generalize the `ElnAdapter` contract itself,
at which point the cursor storage generalizes with it. So the gap is closable now.

**Decision.**
- `sources/registry.py` gains `active_ingest_source_names()` (registry names of active sources with
  an ingest half). `ElnSyncWorkflow` iterates those names: for each source it loads that source's own
  cursor (scheduled runs), syncs it via `sync_eln_entries(source, since)`, and stores the advanced
  cursor per source. The `sync_cursors` table already keys by source name — no schema change. A
  manual backfill (explicit `since`) runs every source from that point and touches no stored cursor.
- The interim multi-ingest guard is removed; multiple ingest sources are now first-class.
- `settings.eln_sync_adapter` is **deleted** (audit DUP-2): it was only the single shared-cursor
  label, which no longer exists. `.env.example` and the runbook (iii) are updated to the
  `data_sources` reality.
- `WorkloadTokenProvider` gains a per-scope `asyncio.Lock`; `get_service_token` re-checks the cache
  under the lock (double-checked), so N concurrent misses on one scope do a single exchange while
  distinct scopes never block each other.

**Consequence (contract note — dev-stage, no live cluster yet).** The sync's stored-cursor keying
changes from one `eln_sync_adapter` label to per-source registry names; on a live system the first
scheduled run after the change re-ingests each source from its epoch once (harmless — ingestion is
idempotent, id-keyed upserts + idempotent note branches). Removing `eln_sync_adapter` is a config
surface change: a deployment that set `CHEMCLAW_ELN_SYNC_ADAPTER` must drop it (`extra="forbid"`).
Both are acceptable now because the F-layer live edges are still open (no in-flight workflows, no
real deployment).

**Result.** `make lint type test` green; `mypy --strict` clean. `tests/test_eln_workflow.py` adds
offline unit tests (named-source activity, `active_ingest_source_names`, the summary fold) and a
server-backed test proving each active ingest source gets its own stored cursor;
`tests/test_workload_identity.py` adds a concurrency test asserting 10 concurrent misses do exactly
one exchange.
