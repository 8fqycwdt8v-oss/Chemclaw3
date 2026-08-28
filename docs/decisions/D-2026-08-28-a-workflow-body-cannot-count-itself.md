# D-2026-08-28-a-workflow-body-cannot-count-itself — the in-flight reading is the broker's, and a failure record never erases a result

## Status

Accepted. Supersedes the `chemclaw_jobs_in_flight` paragraph of
[D-2026-08-27-a-job-that-fails-leaves-no-row](D-2026-08-27-a-job-that-fails-leaves-no-row.md); the
rest of that ADR stands.

## Context

The observability work landed on 2026-08-27 gave the durable tier a failure record, a completion
counter, a duration histogram and an in-flight gauge. Driven against a **live broker and a live
Postgres** on 2026-08-28 — rather than against the direct function calls its own test used — three
of the four turned out to be measurably wrong, each in a way no in-process test could see.

### 1. The in-flight gauge was wrong in three directions and raised in a fourth

`durable/job_metrics.py` kept a process-local `set` of `ConnectorJobWorkflow` ids: added at the top
of the workflow body, discarded in its `finally`. Its docstring argued that neither call needed an
`is_replaying` guard "because this is a statement about the present". Measured:

| event | reading | truth |
| --- | --- | --- |
| `handle.terminate()` | `1.0`, for the life of the process | 0 — a termination never resumes workflow code, so the `finally` never runs |
| eviction (`max_cached_workflows=0`) | `0.0` | 1 — the workflow was still `RUNNING` |
| `worker.shutdown()` | id still present, **plus** `_NotInWorkflowEventLoopError: Not in workflow event loop` raised out of `job_ended(workflow.info().workflow_id)` | 1 |

The eviction row is the one that decides it: a parent wrapper spends its whole life idle between
tasks, so the shipped no-sticky-cache posture reads zero for exactly the long jobs the gauge exists
to count. This is not a bug in the bracketing. **A workflow execution is not "in" a process** —
between tasks it is in the broker, and which worker takes the next task is not this process's
business — so the quantity is not measurable from inside a workflow body at all.

`tests/test_durable_observability.py` called `job_running`/`job_ended` directly and never drove a
workflow, which is why a gauge that was wrong in three directions passed for as long as it existed.

### 2. A failure after `_finish` destroyed the scientific result

`_finish` writes the completed `job_records` row and *then* awaits three best-effort steps, which
swallow `ActivityError` and nothing else. A `CancelledError` (a cancelled workflow was confirmed
live to run its cleanup after one) or a `ValidationError` out of `note_with_run_provenance` reaches
`except BaseException`, which writes `failed_job_record(...)` under the **same job id** — and the
upsert refreshed every mutable column from `EXCLUDED` while `failed_job_record` supplies no
summary, result, note id or calc refs. Measured against Postgres:

```
BEFORE: {'summary': 'dG = -12.3 kJ/mol', 'result': {...}, 'note_id': 'note-1',
         'calc_refs': ['k1','k2'], 'state': 'completed'}
AFTER : {'summary': '', 'result': {}, 'note_id': '', 'calc_refs': [], 'state': 'failed'}
```

One run also booked **both** `outcome="completed"` and `outcome="failed"` on
`chemclaw_jobs_finished_total`, and two samples on `chemclaw_job_duration_seconds`.

### 3. The failure path's record write had no `schedule_to_close_timeout`

`durable/notify.py` documents and *measures* this exact defect one step later ("a workflow calling
`notify_session_best_effort` against an unserved queue was still RUNNING after 75 s with this
timeout at 30"), which is why `notify_session` carries the doubled bound. `_record_run` did not, and
2026-08-27 moved it **ahead** of `_notify_failure`. Measured with the background queue unserved: a
failed job was still RUNNING after 150 s, parked on `record_job`, having never told the chemist
anything.

### 4. Every activity and workflow ran through two `TracingInterceptor`s

`temporalio.worker._worker` prepends the interceptors the *client* already carries, and
`connect_options()` puts a `TracingInterceptor` on every client when `otel_enabled`. Measured chain:
`['TracingInterceptor', 'ChemclawWorkerInterceptor', 'TracingInterceptor']`. The existing test
asserted only what `worker_interceptors()` returns, which is the half that was never wrong — and the
function's docstring claimed our interceptor was outermost, which the SDK's reverse-order wrapping
makes false whatever that list contains.

## Decision

**The in-flight reading is asked of the broker.** `refresh_open_jobs(client)` runs
`count_workflows("WorkflowType = 'ConnectorJobWorkflow' AND ExecutionStatus = 'Running'")` into a
cached number every `jobs_in_flight_refresh_seconds`, driven by `serve_worker` — the same
"cached reading, refreshed by whoever knows, never a query per scrape" shape
`publish/outbox.py::bind_backlog_gauges` already uses, and for the same two reasons: a gauge source
is synchronous and a Prometheus scrape must not make a network call. `job_running` and `job_ended`
are gone, and an absence test fails whoever restores them.

That changes what the number *means*: it is a property of the deployment rather than of a pod, so
every worker publishes the same value and the dashboard takes `max()` where it took `sum()`. The
per-pod number the old set claimed to give never existed. A timer rather than a refresh at each
job's end, because a gauge that only moves when a job finishes stands still for exactly the
deployment with eight long jobs running and nothing completing.

The drain log line drops its jobs figure and keeps `activities_in_flight()`, which is the number a
drain can act on: a cancelled activity is redelivered and paid for twice, while an evicted parent is
picked up by another worker with no work repeated.

**A failure record refreshes what it carries and never clears what it does not.** `job_record_store`
gains a second upsert, chosen by `record.state`, that omits the five columns describing what a run
*produced* (`summary`, `result`, `note_id`, `calc_refs`, `payload_kind`). The asymmetry is the
point: a completed record is the whole account of a run and replaces the row entire — which is what
lets a re-run of a failed job supersede it — while a failed record is an account of how a run ended
and has nothing to say about a result.

Beside it, `ConnectorJobWorkflow` records a failure **only when no completed record is standing for
this run** (`_record_run` now returns whether it wrote), so one run books one
`chemclaw_jobs_finished_total` and one duration sample. Both halves are needed and neither is
redundant: the flag cannot see the case `record_job`'s own docstring names, where the upsert commits
and the activity then overruns its timeout, leaving a row behind while the workflow believes there
is none.

**`_record_run` carries `schedule_to_close_timeout`**, `notify.py`'s doubling rather than a new
knob. That is what makes the documented ordering — record first, then notify — safe rather than
merely intended.

**`worker_interceptors()` returns ours alone**, and its docstring now states what the SDK actually
does: the client's tracing interceptor is outermost and ours runs inside it, which is the right way
round, because a span that does not enclose the log line and the failure counter it explains ends
before the thing it is measuring does.

## Consequences

- A dashboard reading `chemclaw_jobs_in_flight` must use `max()`, not `sum()`. The shipped
  `chemclaw-durable.json` panel is changed in the same commit.
- Every worker issues one visibility count per `jobs_in_flight_refresh_seconds` (default 30 s). A
  failing count is `degraded("jobs_in_flight", …)` and leaves the previous reading standing, so a
  broker hiccup cannot take the refresh loop — and with it the drain and the probe surface — down.
- A `job_records` row can now read `state="failed"` beside a full result. That is the honest
  description of a run whose science completed and whose bookkeeping did not, and
  `find_past_jobs`'s docstring tells the model to read `state` before `summary`.
- Tests that assert wall-clock worker behaviour use `WorkflowEnvironment.start_local()` rather than
  the time-skipping server, which fast-forwards a parent parked on an unserved child queue to its
  own execution timeout instead of leaving it `RUNNING`.
