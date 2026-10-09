# D-2026-10-09-each-step-commits-its-checkpoint-before-the-next-step-starts — a turn runs LangGraph with `durability="sync"`, so a call in flight is always on the committed thread

**Status:** accepted · **Date:** 2026-10-09 · **Builds on**
`D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted`, whose invariant ("every call in the
thread, finished or in flight, is on the positive list") needs the call to be on the thread.

## Context

Programme item W3.11. `turn_resume.judge` decides from the committed checkpoint whether a dead turn
acted. LangGraph 1.2.11 defaults to `durability="async"`: step N's checkpoint is written while step
N+1 runs. A pod killed inside a state-changing tool body can therefore leave the model message that
made the call uncommitted; the judgement is "resumable" and the next attach runs the act again.

Reproduced both ways with real processes on the real graph. Forced: the write that records a
`probe_act` call is held and the pod is killed once the body has started; the surviving replica
answered `200` where it must answer `410`, and the body ran twice. Unforced: under four CPU burners
`test_a_turn_killed_inside_a_state_changing_call_is_not_repeated_and_says_so` failed 3 of its first
5 runs (1 of 10 parallel CI runs).

## Options

1. **Leave `async` and document it.** No cost. A dead turn's act can run twice, which the resume
   record exists to prevent. Rejected.
2. **`durability="sync"` for the turn.** Every step waits for its own checkpoint write before the
   next starts, so the call is committed before its body can run. One setting, no new component,
   and it covers every call, including classes of call added later.
3. **A barrier before each non-repeatable call.** `refuse_when_claim_lost` already runs
   immediately before such a body; it would wait until the checkpoint holding the call is stored.
   Free for a read-only turn. The tool's config does carry the id it would wait for
   (`configurable["checkpoint_map"][""]`, the checkpoint the step's tasks were prepared from,
   langgraph `pregel/_algo.py`), but the wait needs either a poll of `checkpoints` or bookkeeping
   in the saver that tolerates a barrier arriving before the write has started, and a peer's
   subgraph namespace has its own map entry. More moving parts on the path that must not fail, to
   save the cost below on turns that mostly read.
4. **Write an "act started" record before each non-repeatable body and make `judge` read it.**
   Independent of LangGraph's write timing. A second source of truth for what the thread holds, and
   a table (migration, grants, disposal) for it. Rejected for the same reason as 3, with more.

## Decision

Option 2: `api/graph_stream.TURN_DURABILITY = "sync"`, passed to `astream` for a graph that has a
checkpointer (the first run and the resume alike). Two consequences of the mode are handled at
their cause:

- **A graph with no checkpointer cannot wait for a write it never makes.** The run's durability is
  written into its config, so a helper (`task`), a peer or the evidence fan-out, all compiled with
  `checkpointer=False`, inherited `"sync"` and failed with `AttributeError:
  'AsyncPregelLoop' object has no attribute '_put_checkpoint_fut'` inside the tool that called
  them. `core/graph_durability.ignoring_inherited_durability` gives those graphs the resolution
  upstream lacks (no saver means `"async"`); it changes nothing for a graph that has one. Chosen
  over passing `durability="async"` at each call site, which upstream answers with a warning on
  every call and which a peer, being a node, has no call site for.
- **Cancellation reaches the checkpoint wait.** A turn that is stopped while it waits cancels the
  write it awaits. `psycopg-pool` before 3.3.3 caught the `CancelledError` of a connection check
  (`check=`, set on every pool here), returned the connection and looped, so about 3 in 100
  cancelled checkouts returned normally (measured 46 of 1500 with `check=`, 0 of 1500 without) and
  the stopped turn carried on. 3.3.3 re-raises it (0 of 1500). The floor in `pyproject.toml` is the
  fix; every pooled database call in a turn shared the exposure. A write wrapper that finishes the
  write and then re-raises was tried first and removed once the floor made it unnecessary. What
  remains, unexplained: 1 to 2 of 3000 cancellations landed inside psycopg's pipeline or an
  `AsyncExitStack` of cursor and transaction still returned normally, a Stop lost with probability
  about one in two thousand of those that land in the roughly 10 ms of a write. The turn deadline
  (`service_turn_timeout_seconds`) still ends such a turn.

Measured on the final code, four CPUs, a local Postgres, an idle host, a turn of six reads (13
steps; two runs per arm, 20 turns each): median 0.82 s and 0.80 s with `async`, 0.92 s and 0.92 s
with `sync`, so +0.11 s, about 8 ms per step and 13% of a turn that makes no model call. Earlier, on
a loaded host, +0.22 s. Eight concurrent turns in one process (32 turns): median 7.0 s and 6.7 s
with `async`, 6.7 s and 6.1 s with `sync`; the mean wait on the saver's lock per write
(`chemclaw_checkpointer_lock_wait_seconds`) was 0.107 s and 0.102 s with `async` against 0.077 s
and 0.072 s with `sync`. Under concurrency the mode costs nothing measurable: the writes that
overlapped the next step in one turn only queued behind other turns' writes on the lock. A real
turn spends seconds in each model call.

## Consequences

- Every step of every turn waits for its checkpoint write, about 8 ms each on a local database and
  a round trip more on a remote one. The writes are the same number as before.
- A turn killed inside a state-changing call always leaves that call on the thread; the resume
  refuses it as `acted`.
- A new compiled graph run inside a turn with `checkpointer=False` must pass through
  `ignoring_inherited_durability`; `tests/test_turn_durability.py` runs each existing kind inside a
  turn that has a checkpointer.
- The saver's one lock still serialises every checkpoint write in a process, as before; a lock per
  session would remove that for both modes and is a different change with its own measurements.

## What keeps it true

- `tests/test_turn_survives_pod.py`: `test_an_act_does_not_start_until_the_request_for_it_is_stored`
  holds the write that records a call and fails if the body starts (it fails with `"async"`);
  `test_with_the_checkpoint_written_beside_the_next_step_an_act_can_be_repeated` is the control.
- `tests/test_turn_durability.py`: a `task` helper, the evidence fan-out and a peer each run inside
  a turn that has a checkpointer (all three fail without the guard), the guard changes nothing for
  a graph that has a saver, and the same graph unguarded still fails.
- `tests/test_upstream_surface.py`: `astream(durability=)` and `_defaults`' tuple, which the guard
  overrides, and `test_a_cancelled_pool_checkout_is_not_absorbed` (the `psycopg-pool` floor).
- `tests/test_turn_write_ahead.py` cancels turns mid-write under the mode.

Revisit when: the per-step cost is shown to matter on a deployment's real database (a turn's
checkpoint waits exceed a tenth of its wall time in `chemclaw_checkpointer_lock_wait_seconds`),
which reopens option 3; or LangGraph makes `durability` per node.
