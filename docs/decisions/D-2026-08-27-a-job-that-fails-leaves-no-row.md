# D-2026-08-27-a-job-that-fails-leaves-no-row — a durable run that fails leaves the same record a run that succeeds leaves

## Status

Accepted.

## Context

Measured against a live broker on 2026-08-27: one `ConnectorJobWorkflow` was run twice, once
succeeding and once failing on a `ValueError`.

- The **successful** job emitted **zero** log records.
- The **failed** job emitted zero first-party log records and moved **no metric**.
- The only output either run produced was two `temporalio` SDK warnings.
- `job_records` held **one row for two jobs**. The failed one had none.

Four independent absences produce that picture, and each was verified by reading the tree rather
than inferred from the symptom:

1. **No worker interceptor of any kind existed.** `grep -rn "activity.logger" src/` returned
   nothing; 39 of 43 activities logged nothing at all, and the four that did used a plain module
   logger with no `extra`, so no line carried a workflow id, an attempt or a task queue.
2. **`set_current_correlation_id` had exactly one caller in the repository** — the front door
   (`api/runner.py`). Every line every worker wrote therefore rendered
   `correlation_id="-" actor="-" session_id="-"`, while `deploy/README.md` told an operator to join
   on those fields. The ids were never missing: `ConnectorJobInput` carries `correlation_id` and
   `session_id`, and the wrapper puts `requested_by`/`correlation_id` on the child's memo. Nothing
   bound any of it to the ambient context `core/logging.py`'s `ContextFilter` reads.
3. **The durable record was only ever written on the success path.** `ConnectorJobWorkflow._finish`
   is the only caller of `record_job`, and a failing run raises before reaching it.
4. **`Client.connect` was never given a `runtime=`**, so the Temporal SDK's own metric surface did
   not exist: no poller count, no worker slot saturation, no sticky-cache size or miss, no
   `activity_schedule_to_start_latency`, no `activity_execution_failed`, no
   `workflow_task_execution_failed`.

The consequence that decides this: the flagship interaction had **no success rate, no failure rate
and no error budget**. "All my CREST jobs are failing" and "nobody is running jobs" were the same
picture on every dashboard, in every log search, and in the one table meant to outlive Temporal's
history.

Three smaller findings sit inside the same tier and are fixed here because they are the same
mistake at a different scale:

- A **template-launched** job built its `ConnectorJobInput` with no `session_id` and no
  `correlation_id`, though `StepIdentity` had carried both since it was written. `_notify_failure`
  short-circuits on `if not job.session_id: return`, so such a job's failure reached nobody.
  Combined with (3), it left no log, no metric, no row and no session event — it existed only in
  Temporal's expiring history.
- `durable/orchestrator.py` was the **one unguarded workflow-side metric increment in the tree**.
  Its two siblings (`durable/publish.py`, `durable/notify.py`) wrap theirs in
  `if not workflow.unsafe.is_replaying():` and one of them states why; `record_metric` adds no guard
  of its own, so a replayed history re-counted every child that workflow had ever dropped.
- Three long core activities carried **no `heartbeat_timeout`** — the whole-corpus note reindex, the
  retention sweep and the result-publication drain. `connectors/calc/workflows.py` states the rule
  correctly ("without a heartbeat timeout those heartbeats do nothing for failure detection") and it
  was simply never applied to core's own long work, so a dead worker was invisible for the whole
  start-to-close budget: ten minutes for two of them.

## Decision

**One interceptor, installed on every `Worker`, carries the obligations that must hold for every
activity** — the same rule `ConnectorJobWorkflow` already follows for the durable record, the
PR-gate and the push-back, and for the same reason: *"each activity remembers" is the discipline
that fails silently.* `durable/interceptor.py` binds the three ambient ids from the activity's own
argument (one level into a nested identity model, never into the model-authored `payload`), emits
`activity.started` / `activity.finished` through `log_event` with the Temporal coordinates and the
duration, and increments `chemclaw_activity_failures_total{activity}` **once per attempt**, so a
retry storm is a rate rather than something only the broker's history knows. `durable/serve.py`
builds the chain, because that is the one tail every worker's `main()` runs through.

**A run that fails leaves the same kind of row a run that succeeds leaves.** `job_records` gains
`state` (`completed` / `failed`) and `failure_reason`; the wrapper writes the failure record
*before* the session push-back, for the reason `_finish` writes its record before publishing the
note — the row is the durable copy and the notification is a message to a session that may no
longer be listening. `chemclaw_jobs_finished_total{connector,outcome}` and
`chemclaw_job_duration_seconds{connector}` are booked in the `record_job` **activity** beside the
existing runtime counter, which is where D-157's own argument already put them: an activity's side
effects happen once per successful execution, so neither a replay nor a retry can double-count.

**The SDK's metrics get their own port.** `temporal_metrics_port` (0 = off) builds one `Runtime`
per process. A second endpoint rather than a merge, because those names, labels and cardinality are
the SDK's and `core/metrics.py` is deliberately strict about all three.

**The trace crosses the durable boundary and the calc wire.** `TracingInterceptor` is installed on
the client and both workers behind `otel_enabled`, so a durable job is a child of the turn that
launched it. `core.mcp_session.open_session` gains a `request_hook` parameter and
`connectors/calc/remote.py` passes `turn_identity_hook(url)` — the *same* hook the connector
registry installs, never a second one, because the origin-strip guard it carries is a security
control and two copies is how one stops matching `STAMPED_HEADERS`. `core` may not import a
sibling, which is why the hook is a parameter rather than an import.

The three long activities get `heartbeat_timeout` from one new setting,
`background_activity_heartbeat_timeout_seconds`, with `durable/heartbeat.py::beating` deriving the
beat from that same number so the two cannot drift. The orchestrator's increment gets its replay
guard. `_record_run`'s swallow and the three `CalcServerError` paths go through
`metrics_bridge.degraded`, which had exactly **one** call site in the whole of `durable/`,
`connectors/` and `science/` before this. The D-011 cache reports
`chemclaw_calc_cache_total{outcome}` at its three branches — `hit`, `miss`, and `shared` for a
concurrent miss the single-flight joined, which the `was_cached` boolean could not distinguish from
a hit.

## Consequences

- `find_past_jobs` and `GET /jobs` now return failed runs. `JobRecordSummary` therefore carries
  `state`: a failed run appearing in a listing with an empty summary and nothing saying it failed
  would be a worse answer than the one that omitted it. `failure_reason` stays off the summary — the
  listing says *that* a run failed, opening the record says why.
- **`agent/durable_tools.py::_recorded_status` must read `record.state`.** Its docstring states "the
  record is only ever written for a run that *completed*", which this decision makes false, and it
  builds `DurableJobStatus(status="completed")` unconditionally. Until it is changed, a failed job
  whose Temporal history has aged out reports as completed with an empty summary.
  `connectors/jobs.py::failed_job_reason` is the public walker for the related gap on the live path
  (`durable_tools.py` returns a bare status word for any non-completed run, discarding the cause
  that the other two collectors both render).
- Binding an actor inside an activity changes what `require_actor`, `check_expensive_action`,
  `ambient_provenance` and the document share's entitlement read there. It is safe by construction
  in the narrowing direction: roles are taken only where an input declares them, and every gate
  fails closed on an empty role set. What it buys is that the audit trail and the PR-gate name the
  person a background run was launched for instead of booking `""`.
- The chart must scrape `temporal_metrics_port` as a second PodMonitor port on every process that
  opens a Temporal client, and set it; it is 0 by default so nothing binds a port unasked.
- `chemclaw_jobs_in_flight` reads the durable jobs **this process is carrying**, tracked as a set of
  workflow ids so a replay of a job already in the set adds nothing. Its declared HELP text says
  "launched from this process", which describes a subtraction that cannot be done — the launcher and
  the worker are different pods — and should be reworded by whoever owns `core/metrics.py`.
