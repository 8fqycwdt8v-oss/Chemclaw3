# Wave 11 — what a worker holds between tasks

## Items

- [x] **Bound the workflow cache, from a measurement.** `durable/background_worker.py` set
      `max_concurrent_activities` and stopped there, and a **child workflow is not an activity** —
      so the one ceiling this repository chose never reached the bundle children core starts. Two
      of the facts needed were wrong in the obvious reading: the SDK default is **100**, not the
      500 its own constructor docstring names (that is the resource-based tuner), and the
      workflow-task ceiling is not what holds memory. `max_cached_workflows` is.
      `worker_max_cached_workflows` ships at 750, held as an inequality against the chart's
      memory request rather than as a literal, with a second test reading the `Worker(` call so a
      declared-but-unarmed setting reds. ADR + ledger + topic row; the old row replaced by what is
      left of it.
- [x] Fresh-context subagent review; five blockers, three of them factual and all three reproduced
      independently before acting: the `1.35` multiplier's arithmetic, the falsified "replay
      history" mechanism, the 500 (it is `workflow_task_executor`'s thread pool and it *does*
      apply, because the task ceiling is deliberately unset), `connectors/worker.py` never getting
      the ceiling at all, and an `ast` assertion that checked a keyword's name rather than its
      value. All five fixed; the ceiling moved 1,000 → 750 as a consequence of the third term.
- [ ] Full serial `make cov`, PR, merge on green CI.

## Measurements this wave rests on

Against the broker `make up` runs — the downloaded dev server is not fetchable here, which
`tests/temporal_env.py` already records. N workflows parked in `wait_condition`, RSS from
`/proc/self/status` against a 65.8 MiB idle baseline:

| cached | RSS | over idle | each |
|---|---|---|---|
| 50 | 72.5 MiB | +6.7 | 137 KiB |
| 100 | 76.9 | +11.2 | 114 |
| 250 | 85.7 | +20.0 | 82 |
| 500 | 101.5 | +35.7 | 73 |
| 1,000 | 135.1 | +69.4 | **71** |

And at 200 cached, scaling the workflow's own state: 16 KiB → 91 KiB each, 64 → 142, 256 → 347.

**The two-term model those numbers were first fitted to was wrong twice**, and the fresh-context
review caught both. `~75 KiB plus 1.35x state` took `1.35` as `347 / 256` — the *total* per-workflow
cost over the state — so the fixed overhead sat inside the quotient and was then added again.
Against this table's own figures the state coefficient is `(91−75)/16 = 1.00`, `(142−75)/64 = 1.05`,
`(347−75)/256 = 1.06`: **~1.05**, not 1.35. The bad model overstates a 256 KiB workflow by ~18%
(411 MiB per 1,000 against 339 measured) — safe-direction, and still a number nothing produced.

And "the excess being the replay history beside it" is falsified by this table: the excess over
state is *flat* in state, so it cannot be the state term's residual. History is its own axis, and
measuring it on its own at zero state and 200 cached:

| signals per workflow | each | signals delivered |
|---|---|---|
| 0 | 69.1 KiB | 0 |
| 20 | 198.9 | 4,000 |
| 100 | 245.9 | 20,000 |

Slopes 6.5 and 1.77 KiB/signal here against the reviewer's 0.95, so **the axis is established and
its coefficient is not** — which is why the third constant is named an allowance.

Three terms, then: `85 + 1.05×state + history`. At the asserted shape (256 KiB of state, a hundred
signals) that is ~529 KiB, so the SDK's 1,000 comes to ~516 MiB — over half the worker's 1Gi
request. **The ceiling ships at 750**, ~387 MiB. Lowering it beats raising a request every worker
Deployment shares, and an evicted workflow replays rather than fails.

## The measurement that was wrong first

The state-scaling arm initially read 0 KiB per workflow at every size — "state is free". The
fixture was `["y" * 1024 for _ in range(n)]`, which CPython constant-folds into *n* references to
**one** string, so 200 workflows "holding 256 KiB" held 1 KiB between them. A live-instance count
found it: 200 instances, 51,200 entries, 15 MiB of RSS — three numbers that cannot all be true.
The figure only became a measurement once the fixture allocated what it claimed.

That is the third time this session a measurement's *instrument* was the defect rather than its
logic: a tokenizer that matched quotes and missed YAML scalars, a sweep that never reached the leg
it was about, and now a fixture that allocated one object and reported 51,200.

## Review

Pending the gate and the subagent review.
