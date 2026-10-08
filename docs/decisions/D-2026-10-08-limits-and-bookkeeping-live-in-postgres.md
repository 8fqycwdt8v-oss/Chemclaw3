# D-2026-10-08-limits-and-bookkeeping-live-in-postgres — the request budget, the turn ceiling and a finished turn's bookkeeping are kept where another replica or a SIGKILL cannot lose them

**Status:** accepted · **Date:** 2026-10-08

## Context

Programme items W3.2, W3.4 and W3.7 (`D-2026-10-07-the-architecture-programme` fixed the substrate:
Postgres only). Measured with real front-door processes on one database, before any change:

- **Limits multiplied.** Two replicas, one person, a burst of 10: 40 requests admitted 20. Two
  replicas under a turn ceiling of 3 ran 6 turns at once; a per-person cap of 2 let one person run 4.
  The chart's `maxReplicas: 6` multiplies each by 6.
- **A kill lost the bookkeeping.** A replica killed with SIGKILL the moment the client read the
  `answer` frame (the cost ledger and budget writes slowed to widen the window) left the transcript
  and **no `turn_costs` row and no durable budget booking**. Both were `create_task` writes scheduled
  after the answer left (`api/budget.py`, `agent/turn_cost.py`), as were a torn-down turn's spent
  approval (`agent/plan_gate.py`) and its question's settle (`api/runner.py`). Each exists so the
  event loop keeps moving and a bookkeeping error never fails a turn that already answered.
- **`Session.state`** is written by one thing: the in-memory history provider's transcript, plus the
  rollback's snapshot of it. The durable provider ignores it, so under Postgres it is always `{}`.

## Options

**The limits.**

1. **Per process, with the fleet limit at the ingress.** Nothing to build, and the ingress cannot see
   a person (the identity is a token claim) or a turn (a stream). Declined.
2. **Redis.** Declined by the programme: a second stateful dependency to run, secure and back up for
   decisions per second that Postgres serves.
3. **Postgres.** A token bucket in one `INSERT … ON CONFLICT DO UPDATE … RETURNING` per principal,
   refilled lazily in the statement, on the database's clock. For the turn ceiling, three shapes:
   a counter row incremented at start and decremented at end (leaks on SIGKILL, so it needs its own
   lease and reaper); one pre-created row per slot taken with `SKIP LOCKED` (a second lease table, and
   the ceiling becomes a row count); or **counting the turn leases that already exist** under an
   advisory lock.

**The bookkeeping.**

1. **Leave the tasks, and drain them at shutdown.** Covers SIGTERM and not SIGKILL; the measured loss
   stays.
2. **A transactional outbox drained by the background worker.** It survives a kill only if the outbox
   row is written before the answer leaves, which is the same database write the turn could do
   directly; it adds a table, a drainer and a retry policy, and does not help the case that matters,
   a database that is down.
3. **Write before the terminal frame, awaited and bounded.** The writes are issued and waited for, at
   most `service_turn_bookkeeping_timeout_seconds`, before the `answer` or `error` event is yielded.

**`Session.state`.**

1. **Delete it.** The in-memory provider would own its threads by session id, and the disconnect
   rollback becomes provider methods (`snapshot`/`restore`, no-ops under Postgres): more surface than
   the dict, for no behaviour under the durable store, where nothing reads it.
2. **Keep it, say what it is, and prove the durable path does not use it.**

## Decision

**Limits: Postgres, and the turn ceiling counts leases.** `request_buckets` (unlogged, since what a
crash discards is a full bucket, which is what a missing row means) holds the per-principal buckets.
`session_turns` gains `admitted`, set by an admission statement that runs under an advisory lock and
counts unexpired admitted leases against `service_fleet_max_concurrent_turns` and the person's
admitted turns against `service_max_concurrent_turns_per_actor`; the slot is the lease, so a pod
killed mid-turn frees it when the lease lapses and no second lease exists. The process's own permit
stays as the CPU bound and the fast path. A person who has just been refused is remembered locally
until their next token (only their own spends drain their bucket). An unreachable database leaves the
replica's own bucket, so the limit degrades to per replica and never to none; after one failed spend
the database is not asked again for `service_readiness_cache_seconds` (5 s), so an outage costs
each replica one pool timeout per window rather than one per request (requests already in flight
when it starts each wait their own). The status codes and
headers are unchanged: 429 + `Retry-After` for the request budget and the early per-person check; the
`at_capacity` frame for a turn that finds no slot within the admission timeout.

**Bookkeeping: option 3.** `core/bookkeeping.py` tracks the writes in one registry, waits for them
with `asyncio.wait` (cancelling the waiter leaves them running), and `run_turn` waits once before the
terminal frame, for an answered turn and a failed one alike. A write that fails is counted and logged
by its own site and never fails the turn; one that outlasts the bound finishes in the background and
is counted on `chemclaw_bookkeeping_unsettled_total`. A torn-down turn (Stop, deadline, disconnect)
still books on tracked tasks without awaiting, because `run_turn`'s `finally` must not await
(`tests/test_disconnect_teardown.py`), and the shutdown drain now covers every kind of write, not
only the budget's. The question's settle after a teardown (`_PENDING_SETTLES`) is unchanged: an
answered or failed turn settles it inline, and for a torn-down one the route already waits on it
before releasing the claim. The outbox is not built.

**`Session.state`: option 2.** `tests/test_turn_write_ahead.py` shows a durable turn leaves it empty
and `tests/test_session_across_replicas.py` shows a replica that never saw the session serving the
other's turns. Deleting it is declined for now.

## Consequences

- Measured after, same harness: the request budget admits 10 of 40 (was 20), the ceiling runs 3
  turns (was 6), the per-person cap lets a person run 2 (was 4). With SIGKILL on the `answer` frame,
  and on the `error` frame, the cost row, the budget booking and the transcript are all present.
- Per request the shared limiter adds 2.0 ms at p50 and 3.2–3.9 ms at p95 (HTTP, alternating
  requests to a memory-store and a Postgres-store replica); one decision is 1.3 ms p50 / 2.0 ms p95.
  A saturated process makes about 1.1k decisions/s. The bookkeeping writes take 3.6 ms p50 / 7 ms p95
  together; time to the answer frame did not move beyond noise (263–270 ms against 266–270 ms).
- A slow database delays a finished turn's terminal frame by at most the bookkeeping bound: the cost
  row, the budget booking and the spent approval share it. A timed-out turn waits the same bound for
  the writes in flight before its `turn_timeout` frame; a Stop sends no frame. A turn killed before
  its end still has no cost row of its own; the next toucher books it `interrupted`
  (`D-2026-10-03-a-turn-is-written-ahead-and-an-interrupted-one-says-so`).
- The turn slot is a lease, so its bound is the lease's. A holder whose refresh lands after the
  lease lapsed (the database was unreachable for longer than `service_turn_claim_lease_seconds`)
  keeps its session claim but comes back unadmitted: the slot was free meanwhile, and re-asserting
  it would push the count past the ceiling for the rest of the turn. The overshoot is bounded by the
  number of turns in flight during an outage longer than a lease, each finishing uncounted;
  `chemclaw_turn_claim_refresh_failures_total` shows the outage. A turn waiting for a slot holds its
  process permit while it polls, so it can wait up to `service_turn_admission_timeout_seconds` for
  the permit and again for the slot.
- Rolling update: a pod still on the previous image never sets `admitted`, so its running turns are
  not counted against the ceilings until it is replaced, and its upsert does not clear `admitted`
  when it re-claims a lapsed row, which can leave one phantom slot until that turn ends. The
  ceilings are exact once every pod runs the new image; the window is one rollout.
- A spent approval for a turn killed mid-turn after acting is not written until the turn ends; moving
  the spend to the first state-changing call is a plan-gate change this record does not make.
- `service_uvicorn_workers>1` stays refused; its message is pinned by a test and still lists the
  limiter among the broken guarantees.
- The tests: `tests/test_shared_limits.py` (two real processes, each claim measured on in-memory
  replicas first, and the statements under concurrency), `tests/test_bookkeeping_survives_kill.py`,
  `tests/test_bookkeeping.py`.

Revisit when: the limiter needs more than ~1k decisions/s (`chemclaw_requests_rate_limited_total`
and the saturated-process rate above), when `chemclaw_bookkeeping_unsettled_total` is non-zero in
steady state (move the writes to an outbox), or when a second writer of `TurnSession.state` appears
(delete it and give the in-memory provider its own threads).
