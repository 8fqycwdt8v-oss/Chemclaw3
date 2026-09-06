# D-2026-09-06-a-decode-the-workflow-does-not-do-is-a-failure-nobody-hears — the wrapper decodes its child's result itself, and the result envelope ignores what a newer bundle added

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:**
D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed (the rule this restores through a door
it did not cover), D-118 (the envelope is the whole cross-process contract), D-157 (the durable
job record), D-079-workflow-versioning-is-a-deploy-checklist-not-a-ci (the rolling upgrade this is
about) · **Corrects** the docstring of
`Settings._the_job_ceiling_covers_the_activity_it_bounds`, which claimed a property its own
arithmetic cannot deliver.

## Context

`ConnectorJobWorkflow` wraps every connector-owned durable job, and its `except BaseException`
clause carries two obligations no other code has: write the `job_records` failure row, and push
`job_failed` back to a session that was told "this is running" a turn ago. The clause's own comment
says it covers **"every way this run can end badly, not only a failing child … the envelope decode,
`job_record_for`, `note_with_run_provenance` and the two best-effort steps all raise outside"** a
narrower one.

Driven against a live broker with a private queue and schema, one of those five is not covered, and
it is the envelope decode the sentence names first.

## What was measured

`_run_child` passed `result_type=ConnectorJobResult` to `execute_child_workflow`. The SDK converts a
child's returned payload while **applying the activation** — `_apply_resolve_child_workflow_execution`
→ `_convert_payloads` — which is outside the workflow coroutine, and
`@workflow.defn(failure_exception_types=[Exception])` makes it fail the run from there. A child
returning `{"not": "an envelope"}`:

```
A control (good envelope): OK      status COMPLETED
   job_records:    [('w4-x-61d9f3', 'completed', '')]
   session_events: [('job_completed',)]
B non-envelope child:      FAILED  status FAILED
   job_records:    []
   session_events: []
   activities scheduled on the failing run: 0
```

Not one row, not one event, and a chemist still holding the running message: the exact shape
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` names, through the one door the wide
clause was written to close.

**The same mechanism is reachable with no bug at all.** `ConnectorJobResult` was
`extra="forbid"`. The wrapper runs in core's image and the child in the bundle's, so during a
rolling upgrade the bundle is routinely the newer of the two. A child returning the full valid
envelope plus `"provenance_v2": {...}` died identically — `job_records=[] session_events=[]` — which
would be *every in-flight job of that bundle* for the length of the upgrade. Five fields on this
wire carry the comment "additive and defaulted because it crosses the Temporal wire and histories
are in flight"; that rule was true of core→bundle and false of bundle→core, and nothing said so.

## Decision

**The wrapper decodes its child's result itself, inside the `try`.** `_run_child` takes the payload
untyped and returns `envelope_from_result(job_id, raw)` — already the single decoder both
client-side waiters share, already raising a `ValueError` written to be read rather than pydantic's
field dump. The failure lands in workflow code, so the clause runs: measured after, the same
non-envelope child produces one `failed` row and one `job_failed` event naming the envelope.
`failure_exception_types` is still required and its comment now names the raiser it actually has.

**`ConnectorJobResult` is `extra="ignore"`; `ConnectorJobInput` stays `extra="forbid"`.** The
asymmetry is the decision, not an oversight: an unknown field on a *result* comes from a separately
deployed image and is not this core's business, while an unknown field on the *input* is core
writing to itself — every launch site is in this repository, so an extra there is a bug that must
fail loudly. Either half alone closes the skew (the first turns it into a recorded, announced
failure; the second stops it being a failure); both ship, because "additive and defaulted" is now
true in both directions and that is worth being true rather than merely survivable.

**An eviction is not a failure.** The clause also ran during instance teardown, where the parked
coroutine is closed from *outside* the workflow event loop: `workflow.now()` raised
`_NotInWorkflowEventLoopError` and the interpreter printed a bare "Exception ignored in: <coroutine
object …>" on every eviction of a parked connector job. Nothing was lost — the run was not cancelled
server-side and another worker replays it — but a clause that half-runs while claiming to record and
announce is noise that hides the teardown problem worth seeing. It now re-raises on
`not workflow.in_workflow()`, guarding the condition rather than the clock call that noticed it
first, because every `await` below fails on the same fact one line later.

**A worker says at boot whether it keeps job records at all.** `default_job_record_sink()` resolves
on `session_store == "postgres"` and `record_session_event_activity` reads no such switch, so at the
shipped default (`memory`) the same completed run wrote its `job_completed` push-back to Postgres
and dropped its durable record — `record_job` reporting success in 0.000 s and
`chemclaw_jobs_finished_total` incremented anyway, with the drop at DEBUG on the null sink.
`log_record_durability` is a WARNING beside `bind_job_gauges` in `serve_worker`, the one tail every
worker's `main()` runs through. The gate itself is unchanged — it is the same switch
`default_audit_sink` reads, and that is a deliberate design this does not reopen — only its silence.

**The single-flight ledger is per event loop.** `cached_compute` kept one flat dict, so a caller on
a second loop in the same process — the shape `core/temporal_client.py`'s own docstring names —
found the first loop's future and raised `RuntimeError: Task … attached to a different loop`. That
case was neither shared nor deferred; it simply failed, for a cache whose purpose is to stay out of
the way. `_IN_FLIGHT` is now a `WeakKeyDictionary` keyed on the loop, so a ledger dies with its loop
rather than being keyed by an `id()` a later loop can be handed again. The cross-*process* half stays
deferred with its own trigger, unchanged.

## What is corrected rather than changed

`Settings._the_job_ceiling_covers_the_activity_it_bounds` said the check keeps `BAD_DATA_RETRY`
alive, "so `activity_max_attempts` is a number that can never be reached" is what it prevents. It
cannot. A bundle activity's attempt costs its queue wait plus its work, and
`connector_queue_wait_timeout` is *derived* as `C - longest - activity_timeout_seconds` precisely so
that composite fits by construction — so a worst-case attempt costs `C - activity_timeout_seconds`
for **every** ceiling `C`, and exactly one fits. Measured at the shipped defaults: longest 15,000 s,
ceiling 25,200 s, queue wait 10,170 s, one attempt 25,170 s, **30 s left over** against
`activity_max_attempts=5`. Raising the ceiling raises the wait in lockstep.

`durable/connector_job.finish_headroom` had it right from the other side — "one attempt each,
deliberately" — so two docstrings disagreed about one number and the operator-facing one was wrong.
The arithmetic is not touched: it is the reservation this repository intends, argued at length where
it is derived. The docstring now states what it guarantees, and says that `activity_max_attempts` is
spendable by attempts that end well short of their own budget — an unreachable calculation server, a
rejected payload — but never by a second full-length one. Funding that would need `C > 2*(q + w)`,
which this derivation cannot express, and is a new decision rather than a larger number.
`test_the_job_ceiling_funds_exactly_one_worst_case_attempt_at_any_setting` pins the ratio over three
ceilings a decade apart, so the property is measured rather than restated; verified to go red when
the queue wait is re-derived as a fraction of the ceiling.

## Consequences

- A bundle may add a field to the envelope it returns without a coordinated deploy. A bundle that
  returns something that is not the envelope at all still fails the job — loudly, with the row and
  the push-back.
- `tests/fixtures/foreign_result_workflow.py` exists because a child that bypasses this
  repository's own model is the only way to produce either case, and a workflow definition inside a
  test module drags that module's import graph through the SDK's sandbox.
- A memory-store deployment now gets one WARNING per worker start. That is the intent: it is nearly
  always a misconfiguration, and `find_past_jobs` measuring an empty table it cannot distinguish
  from a quiet one is the failure it prevents.
