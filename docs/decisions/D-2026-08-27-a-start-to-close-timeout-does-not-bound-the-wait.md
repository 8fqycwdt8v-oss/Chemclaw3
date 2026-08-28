# D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait — every durable activity bounds the queue, and every Schedule bounds the run

## Status

Accepted. Found in a blind-spot audit of the durable layer and fixed in the same pass.

## Context

Every `workflow.execute_activity` call under `src/chemclaw/durable/` passed exactly one timeout:
`start_to_close_timeout`. Thirty-one call sites, sixteen modules — the ELN, corpus, document and
label syncs, retention, the digest, the reindex, artifact eviction, the observation and memory
miners, the report fan-out, the result publisher, the template steps and the connector-job wrapper.

`start_to_close` does not bound the call. It starts counting when a **worker picks the task up**, so
a task nobody polls is not late — it simply waits. Three ordinary ways to get there: the background
fleet scaled to zero, a rolling update with no worker serving the queue for a window, or a queue
named in config and served by no pod at all.

This was already measured once, on one call. `durable/notify.py` carries the number in a comment: a
workflow calling `notify_session_best_effort` against an unserved queue was still RUNNING after
75 s with its start-to-close at 30 — so the wrapper whose whole contract is "never fail the job
whose scientific result is already done" was instead holding it open indefinitely, and its `except
ActivityError` was unreachable in exactly the case it exists for. That fix was applied to that one
call and the reasoning stopped there.

**What makes the general case worse than the measured one is `ScheduleOverlapPolicy.SKIP`.** Every
Schedule this repository creates uses it, correctly: these jobs are full re-scans, and a run that
overruns its interval must finish rather than have the next fire queue behind it. The consequence
when a run never finishes at all is that *every* subsequent fire of that job family is skipped,
indefinitely — and a skipped fire is an error nowhere. Not in `describe_schedules`, not in a log,
not on a dashboard. The ELN sync, retention, the reindex and the labelling drain simply stop
running, and the deployment's first evidence is a stale corpus.

## Decision

**Bound the wait with `schedule_to_start_timeout`, from one shared helper, at every call site.**

`durable/publish.py::queue_wait_timeout()` reads the one new setting
(`activity_queue_wait_seconds`, default one hour) and every `execute_activity` under `durable/`
passes it. It sits beside `BAD_DATA_RETRY` because that is already where this layer's shared
activity discipline lives and is imported from.

**Schedule-to-start rather than schedule-to-close, and the difference was measured rather than
argued.** Generalising `notify.py`'s `schedule_to_close_timeout` was the obvious move and is wrong:
schedule-to-close caps every attempt *together*, so applying `start_to_close + slack` across
thirty-one sites would have silently deleted the retry budget at each of them. What is wanted is a
bound on the *wait*, leaving `start_to_close` and the retry policy meaning exactly what they meant
before.

The retry interaction is the thing that decides whether that is safe, so it was measured against
the time-skipping test server rather than read off documentation: an activity with
`maximum_attempts=3` and a 10 s `schedule_to_start_timeout` against an unserved queue **failed
once, at 10.028 s**. A ScheduleToStart timeout is not retried — which is the behaviour wanted, since
retrying a task onto the same unserved queue finds the same absence.

`notify.py` keeps its own `schedule_to_close_timeout` unchanged. It is strictly tighter, it was
measured for that call, and its caller wants the stricter thing: a best-effort notification that
cannot delay a finished job. The invariant the test enforces is "the wait is bounded", which either
timeout satisfies.

**An hour, deliberately generous.** A wait on `background-jobs` is ordinary backpressure — eight
concurrent activity slots, some holding one for up to a quarter of an hour — and converting
backpressure into a non-retryable failure would be a worse defect than the one being fixed. What an
hour cannot be is normal, so a task that hits it is a fleet fault and now says so.

**Bound the scheduled run too.** `ScheduleActionStartWorkflow` now carries
`execution_timeout=schedule_run_timeout_seconds` (default a day). The activity bound cannot see a
workflow that hangs on a child, a timer or a wait; this is the backstop that turns "this job family
silently stopped running" into a failed run. A day is safe here specifically because every job on a
Schedule is cursored or idempotent — the ELN and corpus syncs store their high-water mark per
chunk, retention and the reindex are idempotent, the digest advances a watermark — so a terminated
run loses at most the chunk in flight and the next fire resumes.

**Local activities are deliberately untouched.** `execute_local_activity` runs inside the workflow
worker's own task and is never dispatched to a queue; Temporal rejects a schedule-to-start timeout
on one. The rule is stated over dispatched activities only.

**Connector bundles are deliberately out of scope.** `connectors/*/workflows.py` schedules onto a
bundle's own queue, where a wait genuinely is backpressure: a CREST search holds its slot for hours,
so the next one queued behind it is working as designed. The bound belongs where a wait beyond an
hour means a missing worker rather than a busy one.

## Consequences

- One new pair of settings, both ENV-overridable, both documented in `.env.example`.
- `tests/test_activity_queue_bound.py` holds the rule two ways. An AST walk over `durable/` fails
  on any dispatched activity call with neither queue bound — so the next durable job cannot be
  written without one — and a Temporal run proves the bound *does* something: a workflow whose
  activity queue is served by no worker now fails with `SCHEDULE_TO_START` where, measured before
  the change, the same workflow was still running when the test was killed at 90 s.
- `tests/test_schedules.py` asserts the execution ceiling for every planned Schedule, beside the
  test that asserts the SKIP policy — the pair is the point, since SKIP is what makes an endless
  run invisible.
- A deployment that genuinely wants an unbounded wait raises the setting; there is no way to ask
  for infinity, which is the intent.
