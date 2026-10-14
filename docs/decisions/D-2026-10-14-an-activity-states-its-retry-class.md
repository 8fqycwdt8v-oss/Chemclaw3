# D-2026-10-14-an-activity-states-its-retry-class — an activity declares what it does to the outside world, and `durable/retry.py` is the only place a retry policy is written

**Status:** accepted · **Date:** 2026-10-14 · **Builds on**
`D-030-deep-review-hardening-bounded-retries-git-ref-safe` (retries are bounded) and
`D-011` (a persisted result is never recomputed).

## Context

Programme item W7.10 asked for an explicit `RetryPolicy` per activity class "instead of SDK defaults
at 68 of 76 activities". Measured on `main`: that premise does not hold. 72 of the 72 workflow
dispatches (73 `@activity.defn` functions; the interceptor's `self.next.execute_activity` is not a
dispatch) already passed a `retry_policy`; none leaned on the SDK's unlimited default.

The defect is that the policy was chosen **per call site, blind to what the activity does**.
Sixty-four sites passed the one `BAD_DATA_RETRY` (five attempts, the SDK's 1 s doubling to 100 s),
whether the activity was a one-row Postgres write, a corpus sweep, a call to the model gateway, a
mounted share or an outbound channel; four named variants served the other eight
(`note_publish_retry` 3, `calculation_retry` 2, `agent_step_retry` 1, `queued_tool_retry` 1) and one
site spelled `RetryPolicy(maximum_attempts=1)` inline, and a new dispatch took whichever its author
remembered. A down dependency was asked again after 1, 2, 4 and
8 seconds by every worker at once; a Postgres failover outlasted all five attempts.

## Options

1. **Keep `BAD_DATA_RETRY` and the four variants; add a lint.** No behaviour change. The policy
   still follows the call site, so the same activity can be dispatched under two policies, and the
   lint can only check that a name was written, not that it is the right one.
2. **A table in one module, activity name to class.** One place to read every assignment. A new
   activity needs an edit in a distant file, the key is a string that can drift from the function,
   and the Temporal name can differ from the function name (`activity.defn(name=...)`).
3. **The class is declared where the activity is defined, in the decorator that already says which
   queue serves it, and a dispatch asks for `retry_for(activity)`.** The policy follows the
   function, whoever dispatches it; a new activity cannot be written without choosing a class (the
   decorator takes it as a required argument), and a dispatch cannot pick a policy of its own.
4. **The class is passed at the call site: `retry_for(ActivityClass.CALC)`.** Reads well, but is
   option 1 again with an enum: two dispatches of one activity may disagree, and a class that is
   wrong is wrong silently.

## Decision

Option 3. `durable/registry.py` holds `ActivityClass` and stores the declared class on the
function (`@durable_activity(queue, ActivityClass.X)`, or `@activity_in_class(...)` for the one
activity with no fixed queue, `call_queued_tool`). `durable/retry.py` turns a class into a policy
from settings read when the activity is scheduled, owns the bad-data type list that used to live in
`publish.py`, and also holds the two child-workflow policies. Nothing else in `src/` constructs a
`RetryPolicy`.

Seven classes, each distinct in at least one parameter:

| Class | What it is | Attempts and spacing |
| --- | --- | --- |
| `IO` | short Postgres or in-process work | `io_retry_max_attempts` (8); 1 s doubling to 30 s |
| `NOTE_WRITE` | one knowledge-note write | `note_write_max_attempts` (3); as `IO` |
| `COMPUTE` | a sweep or computation in the worker | `activity_max_attempts` (5); 10 s doubling to 300 s |
| `REMOTE` | a service or system outside the worker and Postgres, repeatable | as `COMPUTE`, and jittered |
| `CALC` | the calculation backend | unchanged: sized so the last retry waits one calculation |
| `GOVERNED` | a re-run repeats audited side effects | `agent_step_max_attempts` (1); as `IO` |
| `QUEUED_TOOL` | a heavy tool call waiting for a slot | unchanged: unbounded, ended by the call's `schedule_to_close` |

Every class but `QUEUED_TOOL` refuses the bad-data list by exact name, so a `ValueError`, a contract
mismatch or a refusal costs one attempt. A separate `PUBLISH` class was not made: an idempotent
write to an outside system differs from a repeatable read of one in no parameter, and a class with
no parameter of its own is a name for the same policy. It is one enum member and one table row the
day an outbound channel needs a different bound.

Jitter spreads the first interval of the classes that lean on a shared dependency (`REMOTE`,
`CALC`); `COMPUTE` owns its work and has nothing to spread against. It is drawn from
`workflow.random()`, deterministic on replay.

**Replay.** A retry policy is a field of the schedule command, not part of the sequence Temporal
compares on replay (activity type, id and ordering are), so changing a class's numbers or moving
an activity between classes alters no recorded history. `tests/test_workflow_replay.py` replays
every archived history green against this code. Settings are read per dispatch, so a changed
setting applies to activities scheduled after it, never to one already running.

## Consequences

Behaviour that changes at the shipped defaults: `IO` gets eight attempts over about 90 s instead of
five over 15 s; `COMPUTE` and `REMOTE` (corpus and share syncs, the model gateway calls of a
hypothesis tournament, outbound delivery, a template's tool step) wait 10 s before the first retry
and up to 300 s between later ones instead of 1 s and 100 s; a hypothesis check
(`run_computable_check`) takes the `GOVERNED` bound, 1 by default as before, now tunable with the
agent step's. `CALC` and `QUEUED_TOOL` spacing and attempts are unchanged.

Five settings are added and validated against each other (a first interval above the longest is
refused at start-up, since from a workflow it would fail the task without end).

A template `tool` step (`run_tool_step`) is classed `REMOTE` and so keeps retrying, although a
re-run repeats a governed tool call and its audit row exactly as `run_computable_check` does, which
is single-attempt. Reclassifying it `GOVERNED` is a behaviour decision and is left to the backlog.

## What keeps it true

`tests/test_retry.py`: every `@activity.defn` declares a class and the imported function carries
it; every dispatch passes `retry_for` of the activity it dispatches; no module but `retry.py`
builds a `RetryPolicy`; every class is bounded except `QUEUED_TOOL`, whose dispatch passes
`schedule_to_close_timeout`; no class is empty; and against a real dev server, in every class, a
`ValueError` is attempted once and a transient failure is attempted exactly the configured number
of times and then fails with its own message.

Revisit when: a deployment tunes one class's attempts apart from the settings that share them, or a
second activity needs the unbounded shape of `QUEUED_TOOL`.
