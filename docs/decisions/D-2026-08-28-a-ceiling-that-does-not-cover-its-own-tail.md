# D-2026-08-28-a-ceiling-that-does-not-cover-its-own-tail — a wrapper's headroom is its tail's own bounds, summed

## Status

Accepted.

## Context

`ConnectorJobWorkflow` is not a pass-through. After its child returns it does four things, each one
activity on the background queue: write the durable record (D-157), offer the composite to the
results store, PR-gate the note, push back to the launching session. `wrapper_execution_timeout()`
exists so that the ceiling the *template* path puts on the wrapper stays strictly above the one the
wrapper hands its child, "because anyone giving it an execution timeout must leave room for them" —
an execution timeout is not delivered to workflow code, so a wrapper killed by one runs no failure
clause, sends no push-back and writes no row.

It sized that room as `_FINISH_STEPS * activity_timeout_seconds`. **`activity_timeout_seconds`
bounds none of the four steps.**

- `_record_run` carries `schedule_to_close_timeout = job_record_timeout_seconds * 2` (60 s).
- `notify_session` carries `schedule_to_close_timeout = activity_timeout_seconds * 2` (60 s).
- `publish_result_best_effort` and `publish_note_best_effort` carried **no whole-call bound at
  all** — only `queue_wait_timeout()`, an hour by default, at the front of *every* attempt.

So the headroom was 120 s against a tail that could legitimately spend two orders of magnitude
more. The two bounded steps got their bounds on 2026-08-28, from the measurement `durable/notify.py`
records (a workflow still RUNNING after 75 s against a 30 s start-to-close) and the one
`_record_run` records (a failed job still RUNNING after 150 s on an unserved queue). That sweep
fixed the first and the fourth step and missed the two in between, and nothing noticed because the
number the wrapper's ceiling was built from was never one of the four.

**Measured on 2026-08-28 against a live broker**, background queue unserved, settings scaled down
to keep the ratio the shipped one (child ceiling 10 s, every step 1 s, `activity_queue_wait_seconds`
8 s — against the shipped 30 s and 3,600 s):

> the fixture job **completed**; `_record_run` ran and returned; the wrapper was killed by its own
> ceiling at **14.1 s** — exactly `10 + 4 * 1` — while `publish_result_best_effort` was still
> waiting for a worker. Final status `TIMED_OUT`. No `job_completed` push-back. A template step told
> a finished job had timed out, and the `job_records` row says `completed` beside it.

That is the failure `wrapper_execution_timeout` was written to prevent, reached through its own
arithmetic instead of through a careless caller.

Two live-lane checks were audited alongside it, because both are how this would have been caught.

**E2, "a job survives its connector worker being SIGKILLed mid-flight", has never proved what it
says.** Its precondition was "the wrapper reports RUNNING" — which is true from the instant the job
is launched, on *core's* queue, before the bundle's worker has been handed anything. A kill landing
there interrupts nothing and the check still records the precondition as met. The same poll also
broke on the first status of *any* kind, so a run that had already died reported `at kill: FAILED`,
correctly reporting that it proved nothing, for the second reason rather than the first.

**E1's bar asserts a design this system no longer has.** It required a disconnected session to
accept a new turn within five seconds, on the stated ground that the stream's `finally` releases
both guards "on client disconnect".
`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` retired that: a disconnect detaches the *view*,
the turn runs on a pump of its own to its real end, and both guards release there. The behaviour it
drives (`[[f-slow]]`) thinks for eight seconds. The 2026-08-28 campaign measured 0.2 s, 10.4 s and
25.3 s across three runs against a 60 s lease, and reported two of the three as regressions in a
system behaving exactly as designed.

## Decision

**A ceiling's headroom is the sum of the bounds of the steps it covers, and every step it covers
has one.**

1. `finish_tail_budget()` sums the four post-child steps' own bounds;
   `wrapper_execution_timeout()` is `connector_job_timeout_seconds` plus that sum. `_FINISH_STEPS`
   is gone: a count multiplied by an unrelated setting is not a budget.
2. Each of the four bounds is named where its step is written and read by the sum, so a step whose
   bound moves moves the ceiling with it: `connector_job.record_run_bound`,
   `publish.result_publish_bound`, `publish.note_publish_bound`, `notify.pushback_bound`.
3. `publish.best_effort_close_timeout` is the one rule those four express — a best-effort step gets
   `2 × start_to_close` as `schedule_to_close_timeout` — extracted once it had two hand-written
   copies and two omissions. `publish_note_best_effort` and `publish_result_best_effort` now pass
   it; `publish_note`'s **must-deliver** caller (`report_workflow`) passes none and keeps its full
   `note_write_max_attempts`, because there the note *is* the workflow's result.
4. E2 waits for `running_activity_worker()` — the identity of the worker the broker says is
   executing an activity of the job right now — and reports it, so the check knows it killed the
   process holding the work and says which one. Resolving the child's id goes through core's own
   `child_workflow_id_for`, split out of `child_workflow_id` rather than restated at the observer.
5. E1's verdict is `_freed_without_the_lease(codes, waited, lease)`: the session must answer, the
   wait must not reach the **lease**, and a 409 must have been seen at all — a probe that arrived
   after the detached turn was over measured nothing about the guard and must not be counted as
   evidence that it works.

## Consequences

- `wrapper_execution_timeout()` moves from 18,120 s to 18,600 s at the shipped defaults. It is a
  backstop on the template path only; the direct path (`connectors/jobs.py`) still gives the wrapper
  no execution timeout, because the child is already bounded.
- The note publish's whole-call bound is 240 s, so on the **best-effort** path
  `note_write_max_attempts` is reachable to the extent that its attempts fit that window rather than
  in full. That is the trade `_record_run`'s docstring already accepts and states, for the same
  reason: nothing downstream reads the step's output synchronously, and the alternative is holding a
  finished job open for an hour per step on an unserved queue.
- E1 and E2 will now *fail* on runs they used to pass vacuously, which is the point. E2 in
  particular cannot pass at all unless a worker was genuinely holding the job when it was killed.
- What is still not proven, and is filed rather than fixed: the SIGKILL recovery itself. It needs a
  bundle worker in its own process to kill, which is `make live-storm`'s lane and not this suite's.
  The suite now holds the *precondition* — that a running wrapper and a working worker are
  different facts, and that the broker can tell them apart.
