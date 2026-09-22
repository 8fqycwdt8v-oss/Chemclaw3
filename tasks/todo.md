# Wave 11 — what a worker holds between tasks

## Items

- [x] **Bound the workflow cache, from a measurement.** `durable/background_worker.py` set
      `max_concurrent_activities` and stopped there, and a **child workflow is not an activity** —
      so the one ceiling this repository chose never reached the bundle children core starts. Two
      of the facts needed were wrong in the obvious reading: the SDK default is **100**, not the
      500 its own constructor docstring names (that is the resource-based tuner), and the
      workflow-task ceiling is not what holds memory. `max_cached_workflows` is.
      `worker_max_cached_workflows` ships at 1,000, held as an inequality against the chart's
      memory request rather than as a literal, with a second test reading the `Worker(` call so a
      declared-but-unarmed setting reds. ADR + ledger + topic row; the old row replaced by what is
      left of it.
- [ ] Full serial `make cov`, fresh-context subagent review, PR, merge on green CI.

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
So **~75 KiB plus 1.35x what the workflow holds**, the excess being the replay history beside it.

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
