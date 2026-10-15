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

1. **Keep `BAD_DATA_RETRY` and the four variants; only check that every dispatch names one.** No
   behaviour change. The policy still follows the call site, so the same activity can be dispatched
   under two policies, and the check can only see that a name was written, not that it is the
   right one.
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
| `REMOTE` | a service or system outside the worker and Postgres, repeatable: a data source or share, the model gateway, an embedding or labelling server, retrieval across sources, an outbound channel, a governed tool call | as `COMPUTE`, and jittered |
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

**What decides a class, checked on the code.** An activity that calls the model gateway, an
embedding server, a data source or an outside channel is `REMOTE`, because a retry is billed or
asked again and, unjittered, every worker asks a provider that is down at once. That reading moved
four activities after review: `read_check_result` (a model call), `retrieve_section` and
`gather_hypothesis_evidence` (retrieval across the registered sources, hybrid mode included) to
`REMOTE`, and `collect_digests` (a scan of the corpus and `git log`, no outside call) to `COMPUTE`.
Left in `IO` on the same reading: the activities that touch only Postgres, the settings or the
registry, and `settle_orphaned_waits`, which asks the Temporal frontend the worker already needs
in order to run at all.

`authorize_job_step`, `ground_check_template` and `ground_check_job` stay `IO`, with their reason.
Each runs `audited_launch`, which calls `prepare_job_launch` through the governed chain: validate
the arguments, check funding and availability, authorize an expensive trigger, run the job's
declared precondition. That is a pre-flight, not the launch: it starts no job and changes nothing in
the world. What a re-run repeats is one more append-only `audit_events` row (the sink inserts
without a key, and the chain derives one call id per tool and arguments), which records the attempt
and is the audit trail's intended content for a retried call. This differs from `run_tool_step` and
`run_computable_check`, whose re-run repeats the governed tool call itself. Every deterministic
refusal the pre-flight raises (`AuthorizationError`, `ConnectorError`, a validation error) is in the
bad-data list and costs one attempt; only a transient read fault is retried.

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
refused at start-up, since from a workflow it would fail the task without end). A sixth rule couples
the retry windows to the fan-out child's ceiling: `activity_max_attempts` report sections of
300 s plus the 150 s of backoff between them is 1650 s, which `fan_out_child_timeout_seconds`
(3600 s) must exceed with the child's overhead; at 1500 s the one-attempt rule passes and this one
refuses.

An activity a worker does not serve raises `NotFoundError`, which is not in the bad-data list (on
purpose: during a rolling deploy another pod serves it). It is now retried five times over about
150 s (`REMOTE`) or eight over about 91 s (`IO`) before the workflow gives up, instead of about
15 s, so a misconfigured worker delays a job's end by that much; four tests whose worker left an
activity out waited past their bounds and now serve it.

The same worst case, per item, is not bounded by a setting: a hard Postgres outage makes each
swallowed `IO` write in a per-subscriber or per-source loop cost the whole 91 s window (about 950
digest subscribers exceed `schedule_run_timeout_seconds`). That is a backlog row, not a validator,
because the item count is data.

Two retries multiply and are left as they were: the hypothesis calculation child restarts up to
`activity_max_attempts` times on top of the `CALC` attempts of its activity, and an LLM activity's
Temporal attempts multiply the SDK's own `llm_max_retries`. Both are backlog rows.

A template `tool` step (`run_tool_step`) is classed `REMOTE` and so keeps retrying, although a
re-run repeats a governed tool call and its audit row exactly as `run_computable_check` does, which
is single-attempt. Reclassifying it `GOVERNED` is a behaviour decision and is a backlog row.

## What keeps it true

`tests/test_retry.py`: every `@activity.defn` declares a class and the imported function carries
it; the members of `CALC`, `GOVERNED`, `QUEUED_TOOL` and `REMOTE` are pinned by name, so moving an
activity into or out of one fails a test that names it; every dispatch passes `retry_for` of the
activity it dispatches and every child start one of the two child policies (an AST walk that also
refuses an aliased import, a `getattr` and the dispatch name used as a value, each shown by a
snippet); no module but `retry.py` builds a `RetryPolicy`; every class is bounded except `QUEUED_TOOL`, whose dispatch passes
`schedule_to_close_timeout`; no class is empty; and against a real dev server, in every class, a
`ValueError` is attempted once and a transient failure is attempted exactly the configured number
of times and then fails with its own message.

Revisit when: a deployment tunes one class's attempts apart from the settings that share them, or a
second activity needs the unbounded shape of `QUEUED_TOOL`.
