# D-2026-10-24-a-cached-bo-campaign-grows-with-its-rounds-in-the-sdk-core — what bounds a cached BO campaign's memory

**Status:** proposed · **Date:** 2026-10-24

## Context

`tests/test_workers.py::test_the_workflow_cache_fits_the_memory_the_chart_asks_for` holds
`worker_max_cached_workflows` (750) times an asserted per-workflow cost (about 530 KiB) under half
the worker's 1Gi request. The backlog row said a BO campaign's footprint grows with its rounds
"whether or not the run stays cached", and asked who holds that memory.

Measured on the `make up` broker (Temporal auto-setup 1.25.2, temporalio 1.34.0, macOS, CPython
3.11). One process runs the workflow-only worker with the production sandbox and data converter.
Stub activities run in another process. Twenty `BoCampaignWorkflow` runs (Reizman problem, batch
1) are parked on an unserved `propose_next` after R rounds. Each reading is per run after
`gc.collect()`, as the delta over a warmed baseline, two repetitions each:

| R | cache | live malloc (KiB) | Python heap, tracemalloc (KiB) | phys_footprint (KiB) |
|---|---|---|---|---|
| 10 | 10000 | 620, 620 | 163, 163 | 727, 694 |
| 30 | 10000 | 1416, 1416 | 191, 191 | 1678, 1641 |
| 60 | 10000 | 2916, 2917 | 234, 235 | 3248, 3301 |
| 10 | 1 | 120, 120 | 29, 29 | 641, 607 |
| 30 | 1 | 141, 201 | 32, 32 | 2569, 2761 |

- **This is not a leak.** With the cache at 1, live memory per run is 120–200 KiB at any length.
  The footprint stays high only because the allocator keeps the pages that replays freed. So
  "whether or not cached" was a reading of `phys_footprint`, not of what is held.
- **The cache holds it, and almost none of it is Python.** The Python heap of a cached run grows
  about 1 KiB per round. Live malloc grows about 40–50 KiB per round. Python-tracked protobuf
  objects do not grow at all (one `Payloads` per run). That leaves the SDK core's native run state.
- **The shape is mildly quadratic.** A fit through the three cached points gives about
  284 + 31.5·R + 0.21·R² KiB. The R² term matches the size of the activity inputs: each round sends
  the whole history to `propose_next` and `record_campaign_run`, and every one of those inputs is
  held for as long as the run is cached. This is an inference from the fit; reading the core's
  allocations was not possible here. An earlier reading (#651), 20 rounds of batch 96, came to
  about 12 MiB per run, which is consistent with the history-size term.
- **The budget allows about 12 rounds.** It is 512 MiB / 750 ≈ 699 KiB per cached workflow, which
  the fit reaches at R ≈ 12. The default `n_rounds=10` costs 620 KiB, already above the 530 KiB
  the test asserts. `bo_max_rounds` (500) and `bo_max_evaluations` (2000) allow campaigns 50–100×
  larger. Continue-as-new triggers only when the server suggests it, so it does not bound this.

## Options

1. **Pass the history by reference.** `propose_next` and `record_campaign_run` would read the
   campaign's observations from the campaign store by key, so an activity input stays the same
   size however long the campaign runs. This removes the R² term, the term that grows with the
   batch, and the history-payload growth that continue-as-new now absorbs. It costs an activity
   signature change, which needs a versioned rollout for runs already in flight, and one store read
   per round.
2. **Continue-as-new every K rounds** (a new setting, e.g. `bo_rounds_per_run`). This resets the
   per-round state, so one cached run costs about 284 + K·(31.5 + 0.41·H) KiB with H history rows.
   It is cheap and deterministic. It does not bound H: at H = 2000 that is about 8 MiB per run even
   for K = 10.
3. **Give the bo worker its own cache ceiling**, sized from the fit at a declared campaign length.
   This is a chart value plus `CHEMCLAW_WORKER_MAX_CACHED_WORKFLOWS` on that Deployment, and it
   needs no code. An evicted campaign replays its whole history on every task, though, and
   replay is quadratic too: 20 runs × 60 rounds at cache 1 did not park within 30 minutes here.
4. **Keep everything, and let the bound cover bo only up to a stated length.** The test would
   assert the fit at R = 10. Longer campaigns would rely on the worker's 4Gi limit.

## Decision

Proposed: **option 1, with option 3 as the interim**. Option 1 removes the term that grows with
campaign size, which is the only term the deployment cannot bound by configuration. Option 3 makes
the present bo worker safe now: a ceiling of `0.5 × request / fit(R_declared)`, for example 370 at
R = 30 on 1Gi. When option 1 lands, `tests/test_workers.py` can cover `bo` with the measured
per-round coefficient rather than a placeholder.

## Consequences

- The per-workflow assertion in `tests/test_workers.py` stays as it is for the other workflows. It
  covers `bo` once option 1 or 3 is in place.
- The workflow-cache backlog row now names the holder; it closes with whichever option is taken.

Revisit when: a release of temporalio stops holding a cached run's activity inputs (re-run the
harness that produced the table above), or `bo_max_evaluations` drops below about 100.
