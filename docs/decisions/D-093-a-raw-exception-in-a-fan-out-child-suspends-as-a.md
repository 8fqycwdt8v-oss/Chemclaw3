# D-093 — A raw exception in a fan-out child suspends as a task failure, not a workflow failure

CI's own `ci.yml` comment already named the symptom: `tests/test_orchestrator.py::test_fan_out_runs_children_in_order_and_isolates_failures`
"skips wherever the Temporal test-server binary cannot be fetched and hangs where it can" —
investigated after a PR's CI run was cancelled at the job's 30-minute `timeout-minutes` bound
(added earlier the same day specifically because, before it, every recent `ci` run on `main` had
instead been cancelled at GitHub's 6-hour absolute ceiling: runs #207, #218–#221 all ran the full
six hours before being killed). **This was not new, not caused by that PR, and not fixed by the
timeout bound alone** — the bound only converts a silent 6-hour hang into a bounded, visibly-failing
one. Two distinct issues stacked, and only fixing both cleared the hang.

**Issue 1 — the real root cause.** The Temporal Python SDK's safety default: a raw exception raised
directly in workflow code (not already one of the SDK's own `FailureError` subclasses, e.g. plain
`raise ValueError(...)`) is *not* treated as a workflow failure by default. It "suspends the
workflow via task failure" instead (`temporalio.workflow.defn`'s own docstring) — an internal retry
loop the *worker*, not the server's `RetryPolicy`, drives, with no bound and no
`non_retryable_error_types` check, on the theory that an unclassified exception might be a code bug
that a redeploy will fix, not a legitimate business failure. `_DoublerWorkflow` in the fan-out test
deliberately raises a plain `ValueError` on its poison input (13) — exactly the shape this default
swallows into an unbounded suspend-and-retry loop, invisible to any `retry_policy` passed to
`execute_child_workflow` at all. Against the time-skipping test server this is not a slow hang; it
is a genuine infinite loop (an offline sandbox never gets far enough to hit it — it skips first
when the test-server binary can't be fetched), matching the reported symptom exactly. Fixed by
declaring `_DoublerWorkflow` with `@workflow.defn(failure_exception_types=[Exception])`, which is
what the poison-input test was always implicitly assuming.

**Issue 2 — `fan_out`'s own retry default, found while investigating Issue 1.**
`workflows/orchestrator.py::fan_out` starts each child via
`workflow.execute_child_workflow(..., retry_policy=retry_policy)`, where `retry_policy` defaults to
`None` — and neither real caller (`report_workflow.py`, `memory_jobs.py`) ever passes one either.
`None` does not mean "no retry"; it means Temporal's own default `RetryPolicy()`
(`maximum_attempts=0`, unlimited, no `non_retryable_error_types`). Once Issue 1's fix makes the
poison child's `ValueError` a genuine `WorkflowExecutionFailed`, *this* default is what would make
the fan-out retry it forever anyway rather than isolating and dropping it as documented. Neither
production caller was actually at risk from this specific default (`ReportSectionWorkflow` catches
its activity's error and never raises; `PublishNoteWorkflow`'s uncaught error is an `ActivityError`,
already a `FailureError`, so Issue 1 doesn't apply to it) — but the default was still wrong relative
to the fan-out's own stated contract, so it is fixed regardless: an unset `retry_policy` now
defaults to `BAD_DATA_RETRY` (bounded `maximum_attempts`, immediate failure for the
already-catalogued bad-data exception types) instead of passing `None` straight through — the same
policy already used for the sibling `resolve_fan_out_limit` local activity in the same function, so
no new retry idiom is introduced.

**Verification.** Offline: `tests/test_orchestrator.py`'s non-server tests pass; mypy/ruff clean
across both changed files. The server-backed fan-out test itself cannot run in this sandbox (the
Temporal test-server binary host is egress-blocked here) — confirmed instead against the real
time-skipping server via this repo's own CI, which is reachable there. `fan_out`'s docstring now
calls out the `failure_exception_types` gotcha directly, since any future child workflow that
raises a raw exception (rather than an already-wrapped `FailureError`) would reintroduce Issue 1.
