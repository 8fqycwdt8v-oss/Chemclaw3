# D-2026-09-13-a-loop-that-abandons-its-pool-can-fail-to-end — a pool is closed before its loop ends, not reclaimed after

`core/db._forget_pools_of_ended_loops` exists because nothing used to remove a `_POOLS` entry except
`pooling()`'s `finally`, which runs once at process shutdown — so every `asyncio.run` on a fresh loop
inside a pooled process built a pool, opened it to `pg_pool_min_size` backends, and abandoned it for
the life of the process. That function reclaims such a pool, and its docstring says correctly that
dropping is "the only thing available" *by then*: psycopg schedules a pool's shutdown on the loop it
was opened in, so `close()` on an ended loop raises `RuntimeError: Event loop is closed`.

What it also did was institutionalise the abandon-and-reclaim shape as the plan, and the shape has a
failure mode worse than the leak it was written for.

## What was measured

`asyncio.run` closes its loop through `asyncio.runners._cancel_all_tasks`, which cancels every
remaining task and then **awaits** them all. `psycopg_pool`'s background connect and health-check
workers are tasks on that loop. Driven inside a pooled process with a nested `asyncio.run` on a second
thread — exactly the shape `evals/retrieval._run_sync` and `durable/eval_drift` produce on purpose —
the nested thread never returned. `faulthandler.dump_traceback(all_threads=True)` put its stack in

    asyncio/runners.py:201 in _cancel_all_tasks
    asyncio/runners.py:71  in close
    asyncio/runners.py:189 in run

with `error connecting in 'pool-1': connection timeout expired` on stderr beside it.

The rate is a function of `pg_pool_min_size`, over 8 rounds per setting:

| `pg_pool_min_size` / `pg_pool_max_size` | nested runs that never returned |
| --- | --- |
| 1 / 1 | **0** of 8 |
| 2 / 16 — the shipped defaults | **3** of 8 |
| 8 / 16 | **8** of 8 |

**The one configuration that does not hang is the one the suite pinned.**
`tests/test_db_pool.py::test_a_pool_whose_loop_has_ended_is_neither_counted_nor_left_holding_backends`
covers this path by name; `test_pool_exhaustion_surfaces_as_a_connection_error` pins
`min_size = max_size = 1`; every other pool test in that file sets `min_size` to 0 or 1. So a defect
reachable on the shipped defaults had a green test sitting directly on top of it, for the same reason
`D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` records one level up: a
fixture that differs from the deployment in exactly the dimension under test measures a different
system.

## The decision

**`core/db.close_pools_of_this_loop()`** closes and forgets every pool keyed on the *running* loop —
while that loop is still alive, which is the only moment `close()` is available. It is the pair of
`_forget_pools_of_ended_loops`, not a duplicate of it: that one is the fallback for a loop nobody
closed, this one is the plan.

Two callers, which is what makes it a function rather than a line:

- `pooling()`'s `finally`, whose own body this is — it already popped the pools keyed on `here` and
  awaited `close()` on each, so the extraction is exact and the other half (dropping pools belonging
  to loops that are already gone) stays where it was.
- `evals/retrieval._run_sync`, through a three-line `_closing_this_loops_pools` wrapper so that
  `evals` learns nothing about `_POOLS`. The wrapper is a `try`/`finally`, because the hang is a
  property of the loop's *teardown* and happens whether the coroutine answered or raised.

Measured with the fix, the same probe: **0 of 8 at every one of the three settings**, including
`min_size=8` where it was 8 of 8.

**Both arms of `_run_sync` are wrapped, and the first is the one production takes.**
`durable/eval_drift` runs `run_eval` through `asyncio.to_thread` inside the background worker's
`pooling()`, so the thread `_run_sync` lands on has no running loop and goes down the
`asyncio.run(...)` arm directly; the second arm, which spins a further thread, is the defensive one
for a metric called from a coroutine.

## What was not done

**No `db.pooling()` round the nested loop**, which is the general form the backlog row reached for.
`pooling()` sets the process-wide `_POOLING` flag and binds the pool gauges: entering it on a nested
loop inside a pooled process would turn pooling **off** for the parent loop when the nested one
exited, which is a worse defect than the one being fixed and would be invisible until the parent's
next `connection()` opened a dedicated socket. The narrow function is not a concession to KISS here —
the wide one is wrong.

The other 30 `asyncio.run` call sites in `src/` are CLI `main()` entry points: one loop per process,
which exits immediately afterwards. They are outside this because there is no parent loop whose
pooling they can be nested inside — a hang there is a process that was about to exit anyway, and
`_POOLING` is off in all of them.

## What keeps it true

| property | test |
| --- | --- |
| a nested `asyncio.run` that opened a pool inside a pooled process still returns | `tests/test_db_pool.py::test_a_nested_loop_that_opened_a_pool_still_ends` |
| a pool whose loop has already ended is still reclaimed, and still counted as gone | `tests/test_db_pool.py::test_a_pool_whose_loop_has_ended_is_neither_counted_nor_left_holding_backends` |
| `pooling()` still closes what it opened and drops what it did not | the rest of `tests/test_db_pool.py`, 16 passing with Postgres up |

The new test sets `min_size=8` deliberately, and its docstring says why: at the shipped defaults it
would be red three times in eight, and at the value the rest of the file pins it could not be red at
all. It drives **`_run_sync` itself** rather than the wrapper — the arm a test calling
`_closing_this_loops_pools` directly cannot see is `_run_sync` stopping to call it — and asserts only
that the thread joins inside a wall clock. It deliberately does not inspect `_POOLS`: a registry
empty because the pool was closed and one empty because `_forget_pools_of_ended_loops` reclaimed it
afterwards look identical, and only one of them is a thread that came back.

Two mutations, each restored from a `.bak`:

| mutation | result |
| --- | --- |
| `_run_sync` calls `asyncio.run(coro)` again instead of going through the wrapper | red — 1 failed, 15 passed, the hang reported as the assertion rather than as a timeout in whatever ran next |
| `close_pools_of_this_loop` drops its pools instead of awaiting `close()` | worse than red: the whole `tests/test_db_pool.py` run wedged and was killed at 300 s, because `pooling()`'s own teardown goes through the same function. That `close()` and not the drop is the operative act is the thing this shows |
