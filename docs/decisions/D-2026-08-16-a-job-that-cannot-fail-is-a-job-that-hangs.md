# D-2026-08-16-a-job-that-cannot-fail-is-a-job-that-hangs — the job path declares its failure types, and stops retrying whole children

**Status:** accepted · **Date:** 2026-08-16 · Extends `D-093`'s fan-out finding to the wrapper every
connector job runs through, and narrows the child retry policy that finding left in place.

## Context

A review of the durable seam drove the real workflows against a live broker rather than reasoning
about them. Two states came out of it in which a durable job is **permanently lost while the chemist
is still told it is running**, and one in which the most expensive thing this system does is paid
for five times.

**The Temporal SDK does not fail a workflow that raises a plain exception in workflow code.** It
treats it as a suspected bug and parks the run in an internal workflow-task-failure loop that
ignores the retry policy and never gives up. That is a defensible default for a workflow nobody is
waiting on. `ConnectorJobWorkflow` raises plain exceptions of its own — chiefly the
`result_type=ConnectorJobResult` decode of whatever the bundle's workflow returned — and it is the
one wrapper *every* connector job runs inside.

Measured: a bundle workflow returning a non-envelope left the parent `RUNNING` indefinitely, its
history repeating `workflow_task_failed: "Failed decoding arguments"` every ~10 s, the worker
re-polling the poisoned task forever. No `job_failed` push-back. `get_durable_job_status` answering
`running` for a job that will never finish. The parent carries no `execution_timeout` of its own —
only the child does — so nothing ends it. The same shape was measured one level down: a bundle
workflow reading an absent optional key from its payload (`prepare_job_launch` dumps with
`exclude_none=True`, so an omitted optional param is simply not there) hung the child, and the parent
waited on it.

This repository already knew the trap. `TemplateWorkflow` declares `failure_exception_types=[Exception]`
for exactly this reason (REV-13), `durable/orchestrator.py` documents it at length (D-093), and
`tests/test_template_job_step.py` pins it. Twenty workflows are registered across the four queues;
**one** declared it.

Separately, the child call carried `retry_policy=BAD_DATA_RETRY`, which cannot classify anything at
that boundary: Temporal matches `non_retryable_error_types` against the *outermost* failure, and a
child that failed through its own activity surfaces as `ActivityFailure` — a name deliberately
absent from `_BAD_DATA_TYPES`, since that list names the errors themselves. Measured: the identical
`ValueError` costs **1** attempt at an activity boundary and **5 child executions** here, 15.4 s of
backoff, six executions under one parent id. On `qm` that is five DFT submissions for one
unparameterised basis set, and the D-011 cache cannot help, because a failed run stores nothing.

## Decision

**1. Every workflow on the job path declares `failure_exception_types=[Exception]`** — the wrapper
and each bundle's own workflow — and `tests/test_workflow_registry.py` asserts it over the
*registry*, so a bundle added later is covered without editing the test.

**2. `ConnectorJobWorkflow` notifies its session on any way the run can end badly**, not only on a
failing child. The clause was `except (ChildWorkflowError, ActivityError)` around the child call,
which is a correct account of the child failing and covers nothing else — the envelope decode,
`job_record_for` and `note_with_run_provenance` all raise outside it. Without this, decision 1 would
have converted a measured hang into a *silent* failure, which is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` through a door that clause does not
cover. `BaseException`, so a cancellation is announced too.

**3. The child is started with `maximum_attempts=1`.** A workflow-level retry at this boundary can
only duplicate compute: the child's own activities already carry `BAD_DATA_RETRY`, so genuine
transients are retried where they *can* be classified, and a worker that dies mid-child is
re-delivered by Temporal without any workflow retry.

## The trade, stated rather than buried

Declaring the failure types means **a genuine code bug in a redeploy now fails the in-flight jobs
instead of parking them until someone ships a fix.** That is a real loss and it is the right way
round here: a job that hangs forever while a chemist is told it is running is the worse failure, it
is invisible to every operator signal, and the same judgement was already made one module over for
templates. A parked run is only recoverable if somebody notices it, and nothing here would have.

## Scope, and what is deliberately left

The declaration is applied to the **job path** — the wrapper plus every workflow on a
`connector-<name>` queue — not to all twenty registered workflows. The sixteen periodic ones
(retention, the memory jobs, the report fan-out, the approval hold) have nobody waiting on a turn,
so the same trade lands differently for them and is a separate decision. It is a `BACKLOG.md` row
with this ADR named, not a silent extension.

## What this was verified against

- The hang: reproduced live, parent `RUNNING` past 45 s and past worker exit, history as quoted.
- The retry: 5 child executions vs 1 activity attempt for one `ValueError`, timed.
- The guard: mutation-checked — reverting the decorator on `CalcJobWorkflow` alone turns
  `test_every_workflow_on_the_job_path_can_actually_fail` red.
- `make lint type test` green with a live Postgres and Temporal.
