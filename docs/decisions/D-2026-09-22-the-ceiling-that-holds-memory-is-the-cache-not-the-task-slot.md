# D-2026-09-22-the-ceiling-that-holds-memory-is-the-cache-not-the-task-slot — bounding the worker

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Closes the `BACKLOG.md` row
*"`max_concurrent_workflow_tasks` is set nowhere, so nothing this repository chose bounds
workflow-task concurrency"*, and declines the half of it that asks for a task ceiling.

## Context

`durable/background_worker.py` sets `max_concurrent_activities` and stops there, so every other
worker ceiling was whatever the SDK picks. The row put it sharply: **a child workflow is not an
activity**, so the activity ceiling does not reach the bundle children core starts at all. It also
set the bar for answering it — "measure what a saturated worker actually holds before choosing a
number: a ceiling set from the SDK's default is the same unexamined posture this row is about, one
value further on."

**Two of the three facts needed turned out to be wrong in the obvious reading.**

- **The SDK default is 100, not 500.** `Worker.__init__` passes `None` through to
  `WorkerTuner.create_fixed`, whose `or 100` is the real number for workflow-task slots. The 500 in
  the constructor's own docstring belongs to the *resource-based* tuner, which this worker does not
  use.
- **The workflow-task ceiling is not what holds memory.** A task slot is occupied only while a
  workflow is being advanced — short, CPU-bound work. `max_cached_workflows` (SDK default 1,000) is
  what keeps a started workflow *resident between its tasks*, and it was equally unset.

## Decision

**Bound the cache, from a measurement, and decline to invent a task ceiling.**

- **Measured against the broker `make up` runs, not the downloaded dev server** — which this
  sandbox cannot fetch, as `tests/temporal_env.py` already records. A worker with N workflows
  parked in `wait_condition`, RSS read from `/proc/self/status` against an idle baseline of
  65.8 MiB: 50 → 137 KiB each, 100 → 114, 250 → 82, 500 → 73, 1,000 → **71 KiB each**, converging
  as the fixed cost amortises. Scaling the workflow's own state at 200 cached: 16 KiB of state →
  91 KiB each, 64 → 142, 256 → 347. So a cached workflow costs about **75 KiB plus 1.35× whatever
  it holds**, the excess being the replay history the cache keeps beside it.

- **The first version of that second measurement said state was free**, and it was the fixture:
  `["y" * 1024 for _ in range(n)]` is constant-folded into `n` references to **one** string, so
  200 workflows "holding 256 KiB" held 1 KiB between them. A live-instance count found it — 200
  instances, 51,200 entries, 15 MiB of RSS — and the number only became a measurement once the
  fixture allocated what it claimed.

- **`worker_max_cached_workflows` ships at 1,000** — the same value the SDK would have picked, and
  that is the point rather than an oversight: it is now a number this repository chose and can
  defend, with `tests/test_workers.py` holding it as an **inequality against the chart's memory
  request** rather than as a literal. At the shipped 1Gi request, 1,000 cached workflows carrying
  256 KiB apiece is ~340 MiB, a third of the request before the worker does anything else; the
  guard fails at half. Raising the ceiling, shrinking the request, or a workflow that carries more
  state all move the same comparison, and only one of the three is a config edit anyone would think
  to check (`D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared`).

- **`max_concurrent_workflow_tasks` is deliberately left to the SDK, and that is the declined
  half.** The row is right that an unexamined default is a posture; the answer is not to replace it
  with an equally unexamined one. The bound that matters for this ceiling is **CPU**, the worker's
  limit is 2 cores, and nothing here has measured what a workflow task costs — so any number would
  be chosen to look deliberate. What was open was whether memory was unbounded, and it was; that
  half is closed.

## Consequences

**A setting nothing passes to the SDK is a number in a file**, so a second test reads the `Worker(`
call with `ast` and fails if the ceiling is declared and not armed. That is the shape this change
was written to prevent one level down, applied to itself.

**The measurement is in the test, not only in the prose.** `_CACHED_WORKFLOW_OVERHEAD_KIB` and
`_CACHED_WORKFLOW_STATE_MULTIPLIER` are the model; `_STATE_THE_BOUND_IS_ASSERTED_AT_KIB` is the
per-workflow state it is checked at, and it is *not* a measurement of this system's workflows —
nobody has made one. It is the size at which the shipped cache takes a third of the request, which
is where the inequality is worth asserting.

**Revisit when:** somebody measures what this system's own workflows carry. `BoCampaignWorkflow`
and the durable calc workflows are the ones to sample, and the figure to replace is
`_STATE_THE_BOUND_IS_ASSERTED_AT_KIB` — which is a placeholder wearing a name that says so. The
other trigger is a CPU measurement of a workflow task, which is what the declined half needs before
a task ceiling can be anything but a guess. The file that would show the first is
`tests/test_workers.py`, which fails the day the chart's worker request shrinks under the model it
already holds.
