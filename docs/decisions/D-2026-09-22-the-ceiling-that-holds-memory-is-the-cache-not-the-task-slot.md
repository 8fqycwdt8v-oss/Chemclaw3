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

**Two of the three facts needed turned out to be wrong in the obvious reading**, and the SDK's two
defaults are not the ones its own docstring makes obvious.

- **Workflow-task slots default to 100, not 500.** `Worker.__init__` passes `None` through to
  `WorkerTuner.create_fixed`, whose `or 100` is the real number.
- **The 500 in that docstring is a thread pool, and it does apply here.** It is
  `workflow_task_executor`'s: `temporalio/worker/_workflow.py` builds
  `ThreadPoolExecutor(max_workers=max_concurrent_workflow_tasks or 500)`, so leaving the task
  ceiling unset — which this decision does, deliberately — gives a pool sized 500. A first draft of
  this ADR dismissed that 500 as belonging to the resource-based tuner. That is a *different* 500,
  in a different file (`_tuning.py`'s `_DEFAULT_RESOURCE_SLOTS_MAX`), and it is not in any
  constructor docstring. `max_workers` is a ceiling on threads created on demand rather than an
  allocation, so it is recorded rather than acted on.
- **Neither is what holds memory.** A task slot is occupied only while a workflow is being
  advanced — short, CPU-bound work. `max_cached_workflows` (SDK default 1,000) is what keeps a
  started workflow *resident between its tasks*, and it was equally unset. Driven: with the cache
  off, 200 started-and-parked workflows leave **zero** instances resident and the RSS delta falls
  from 70 MiB to 12.

## Decision

**Bound the cache, from a measurement, and decline to invent a task ceiling.**

- **Measured against the broker `make up` runs, not the downloaded dev server** — which this
  sandbox cannot fetch, as `tests/temporal_env.py` already records. A worker with N workflows
  parked, RSS read from `/proc/self/status` against an idle baseline of ~66 MiB.

- **The model has three terms, because two were not enough.**
  - *fixed*, converging as it amortises: 50 cached → 137 KiB each, 100 → 114, 250 → 82, 500 → 73,
    1,000 → 71. A second run with a timer park rather than a `wait_condition` one gave
    152/114/80/69/64, so the park shape does not matter and the asymptote is ~65–85.
  - *state*, at 200 cached: 16 KiB of state → +17 KiB over the zero-state figure, 64 → +66,
    256 → +275. That is **~1.05×**, the residual being the 1 KiB strings' own object headers.
  - *history*, which the first draft had no variable for. At zero state and 200 cached: no signals
    → 69 KiB each, twenty → 199, a hundred → 246. Two independent runs put the slope at 0.95 and at
    ~1.8 KiB per signal, so what is established is that **the axis is real and can triple a
    low-state workflow**, not its coefficient — which is why the constant in the test is named an
    *allowance*.

- **The first draft's model was `75 + 1.35 × state`, and the 1.35 was arithmetic on a mistake.** It
  is `347 / 256` — the *total* per-workflow cost divided by the state, so the fixed overhead was
  counted inside the quotient and then added again on top. That model overstated a 256 KiB workflow
  by ~18% (411 MiB per 1,000 against 339 measured) while this ADR quoted 340 MiB for the same
  1,000, a figure the model it stated does not produce. Both are gone: the constants are
  `_CACHED_WORKFLOW_OVERHEAD_KIB = 85`, `_CACHED_WORKFLOW_STATE_MULTIPLIER = 1.05` and
  `_CACHED_WORKFLOW_HISTORY_ALLOWANCE_KIB = 175`, and the ADR states no product they do not.

- **An earlier version of the state measurement said state was free**, and it was the fixture:
  `["y" * 1024 for _ in range(n)]` is constant-folded into `n` references to **one** string, so
  200 workflows "holding 256 KiB" held 1 KiB between them. A live-instance count found it — 200
  instances, 51,200 entries, 15 MiB of RSS — and the number only became a measurement once the
  fixture allocated what it claimed.

- **`worker_max_cached_workflows` ships at 750, not at the SDK's 1,000, and the history term is
  what moved it.** Under the two-term model 1,000 workflows at 256 KiB came to ~340 MiB and fitted
  the shipped 1Gi request comfortably. With the history allowance the same 1,000 come to ~516 MiB —
  over half the worker's whole request before it has done anything else. 750 of that shape is
  ~387 MiB. `tests/test_workers.py` holds it as an **inequality against the chart's memory
  request** rather than as a literal, and the share the cache may claim is its own named constant
  (`D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared`).

- **Lowering the ceiling rather than raising the request**, because `resources.worker` is the
  default for *every* worker Deployment, core's and each bundle's: raising it charges scheduling
  density across the fleet for cache slots no queue has been shown to need. What a slot buys is
  avoiding one replay — an evicted workflow is re-created from its history on its next task, which
  is broker traffic and CPU, never a wrong answer. And the working set this cache is for is
  workflows being *advanced*: a durable wait parked for weeks under `awaiting_max_days` should be
  evicted, which is behaviour a smaller cache gets right rather than a regression. 750 and not the
  991 the inequality permits, because the history coefficient is the least certain term and a
  ceiling should not sit at the bar it is checked against.

- **Both worker entrypoints arm it.** `connectors/worker.py` had only the activity ceiling, and it
  is the process that caches a bundle's job — the child workflow core starts on
  `connector-<name>`. Arming core alone would have left the half of the fleet the row is actually
  about on the SDK's default.

- **`max_concurrent_workflow_tasks` is deliberately left to the SDK, and that is the declined
  half.** The row is right that an unexamined default is a posture; the answer is not to replace it
  with an equally unexamined one. The bound that matters for this ceiling is **CPU**, the worker's
  limit is 2 cores, and nothing here has measured what a workflow task costs — so any number would
  be chosen to look deliberate. What was open was whether memory was unbounded, and it was; that
  half is closed.

## Consequences

**A setting nothing passes to the SDK is a number in a file**, so a second test reads both
entrypoints' `Worker(` calls with `ast` and fails if the ceiling is declared and not armed. It
checks the *value* rather than the keyword's presence, and reads only the call inside the function
the deployment runs — a name-only assertion is satisfied by a literal somebody pasted, or by a
`Worker(` in a helper nothing calls. That is the shape this change was written to prevent one level
down, applied to itself.

**The measurement is in the test, not only in the prose.** The three constants are the model;
`_STATE_THE_BOUND_IS_ASSERTED_AT_KIB` is the per-workflow state it is checked at, and it is *not* a
measurement of this system's workflows — nobody has made one. Paired with the history allowance it
is the shape of a long-lived campaign parent: the most expensive workflow this repository
plausibly parks, rather than the average one.

**Revisit when:** somebody measures what this system's own workflows carry. `BoCampaignWorkflow`
and the durable calc workflows are the ones to sample, and the figures to replace are
`_STATE_THE_BOUND_IS_ASSERTED_AT_KIB` and `_CACHED_WORKFLOW_HISTORY_ALLOWANCE_KIB` — placeholders
wearing names that say so, and the second one is where a factor-of-two disagreement between two
runs is still parked. The other trigger is a CPU measurement of a workflow task, which is what the
declined half needs before a task ceiling can be anything but a guess. The file that would show the
first is `tests/test_workers.py`, which fails the day the chart's worker request shrinks under the
model it already holds.
