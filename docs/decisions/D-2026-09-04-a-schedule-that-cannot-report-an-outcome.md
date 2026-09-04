# D-2026-09-04-a-schedule-that-cannot-report-an-outcome — the ceiling moved the failure onto a silent surface

**Status:** accepted · **Date:** 2026-09-04 · Supersedes the schedule-health claims in
`D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait` and
`D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it`. Neither is edited;
both stand as the record of what was decided then, and four of their sentences are corrected below.

## Context

`schedule_run_timeout_seconds` ended a real failure: an overrunning run held its slot and every
subsequent fire was skipped. That fix was right, and it moved the failure onto a surface that says
nothing.

`ScheduleHealth` carried `paused`, `last_run`, `runs_total`, `skipped_overlap`, `running_now` and
`note`. Measured against a live broker — two schedules on 3 s intervals, `schedule_run_timeout_seconds`
at 1.0, one workflow returning immediately and one waiting forever:

```
probe-healthy  paused:false runs_total:4 skipped_overlap:0 running_now:0 note:""
probe-wedged   paused:false runs_total:4 skipped_overlap:0 running_now:0 note:""
```

Identical in every field but the id and the timestamp. A schedule whose *every* run is killed reads
exactly like one whose every run succeeds — while the wedge the ceiling replaced had a distinctive
signature on that same surface (`last_run` frozen, `running_now` stuck at 1, `skipped_overlap`
climbing). So an operator who learned to read the old signature now sees nothing at all.

## Why it was deferred, and why that reason was wrong

The backlog row recorded this as costing "one `describe` per schedule on the front door's own event
loop, which is why it was not taken here", and both ADRs above treat the schedule surface as the
only thing available offline. **The premise that it needed a live broker is false.**
`WorkflowEnvironment.start_local()` runs in this sandbox and supports Schedules, and
`tests/temporal_env.py` already had the helper. Everything below is driven against it. Time
skipping is the wrong instrument here and that helper's own docstring says why.

What *is* true is that the outcome is not on the schedule surface at all. Measured against the
installed `temporalio` 1.31.0:

```
ScheduleActionResult                  ['scheduled_at', 'started_at', 'action']
ScheduleActionExecutionStartWorkflow  ['workflow_id', 'first_execution_run_id']
ScheduleInfo                          no failure counter beside num_actions_skipped_overlap
```

`_describe` was already reading everything Temporal offers. The outcome has to come from a
*workflow* describe or from nowhere — which is the fact the deferral should have rested on.

## Decision

`ScheduleHealth` gains `last_outcome`: Temporal's own `WorkflowExecutionStatus` name for the newest
run of that schedule **that has finished**.

```
probe-healthy  … running_now:0 last_outcome:"COMPLETED"
probe-wedged   … running_now:1 last_outcome:"TIMED_OUT"
```

Deliberately not "the status of the run `last_run` names": a run in flight has no outcome, and
`running_now` already reports one. `""` means nothing has finished yet; `"unknown"` means the run
could not be described, with the reason in `note`.

**The cost is one extra describe per schedule, and no new setting.** `ScheduleInfo.running_actions`
already names the workflow ids Temporal considers in flight, so the newest `recent_actions` entry
*not* in that set is the newest finished run — no describe is spent discovering that a run is
unfinished, and there is no lookback window to tune. The bound is the existing
`connector_health_timeout_seconds`, the same probe budget the schedule lookup one line above uses:
a second knob for the same probe on the same event loop could only disagree with it. The sweep
still fans out with one `gather`, so its worst case is `2 ×` that timeout rather than a sum.

### The field the obvious implementation would have used is the wrong one

The run is described **by workflow id alone, never by `action.first_execution_run_id`**. Four
scheduled jobs drain by `continue_as_new`, and a `continue_as_new` chain shares one workflow id: the
bare id answers with the chain's *tail*, `first_execution_run_id` with its *head*. Measured on a
three-hop chain killed by its run timeout:

```
probe-chain-scheduled-…T05:38:08Z   tail: TIMED_OUT   first_execution_run_id: CONTINUED_AS_NEW
probe-chain-scheduled-…T05:38:16Z   tail: TIMED_OUT   first_execution_run_id: CONTINUED_AS_NEW
```

So the obvious field — and the one the investigation behind this change first reached for — would
have reported a killed `corpus_sync`, `document_sync`, `label_sync` or `eln_sync` drain as
`CONTINUED_AS_NEW`, i.e. as normal. A single-hop test passes either way, which is why this needed a
chain to see.

### Failing safe

Any failure of the run describe — timeout, expired retention, gRPC `NOT_FOUND` — degrades to
`"unknown"` and never raises, so one dead lookup cannot end the sweep for the other schedules. A
non-`StartWorkflow` action shape drops out by `isinstance` rather than raising. Narrowing the
`except` to `ValueError` was tried: `RuntimeError: workflow execution already completed and was
deleted` then escapes and takes the whole sweep with it.

## Four claims in merged ADRs that the tree has falsified

Recorded here rather than edited there, per this repository's rule that a merged ADR is never
changed. Three were named by the backlog row; the fourth was not, and is the largest.

1. **"the ELN and corpus syncs store their high-water mark per chunk"** — half true, and the
   backlog row's own evidence for it was the wrong file. The *ELN* half still holds
   (`durable/eln_sync.py` keeps a per-source `sync_cursors` row). The *corpus* half is false in the
   mode the claim was made about: `corpus_sync.py` persists only `if binding.append_only`, and its
   module docstring says that for a **release** — "the default, and what this job was written for"
   — the cursor is intra-run only and rides the state. So a terminated release-mode corpus run
   restarts from its first page, not "at most the chunk in flight".
   `D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop` is what split the two modes. The row cited
   `document_sync.py`, which does keep no row — but a full re-walk there is idempotent, so
   document-sync does not falsify the clause. The corpus half does.

2. **"a run with no memo stamps nothing"** — false. `connectors/calc/workflows.py` reads
   `workflow.memo_value("requested_by", settings.service_actor_id)`, which defaults to
   `service-account`, so the durable path stamps it. True only of a *direct caller* of the
   activity, whose own defaults are `""`.

3. **the queue-bound AST rule "fails on any dispatched activity call with neither queue bound"** —
   was false when written, is true now. Three spellings walked past the original: an import alias,
   a bare `execute_activity` imported by name (an `ast.Name`, which never reached the receiver
   check), and a site in a `durable/` subpackage that `glob` does not descend into.

4. **Both ADRs say `_build_schedule` sets `execution_timeout`.** It sets **`run_timeout`**, changed
   under `D-2026-08-28-a-gate-that-cannot-fire-and-a-rate-with-no-denominator`. The difference is
   load-bearing: `execution_timeout` bounds the whole `continue_as_new` chain and a continued run
   cannot extend it, so it would kill a multi-day first corpus load. It is also the reason claim 4
   and the `first_execution_run_id` finding above are the same fact seen twice — a chain is one
   workflow to some of Temporal's API and many runs to the rest.

Worth recording that (2) and (3) had **already been corrected in the tree's own tests, in the
present tense**, while the ADRs kept the old sentence. That is `lessons.md` #15 exactly.

## Consequences

Five in-tree comments asserted in the present tense that `ScheduleHealth` carries no run outcome.
All five are corrected. **No park/declare stance resting on them is changed** — each now says the
trade is reopened, not settled.

`D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it` rested six "may park"
stances on the measured fact that the schedule surface carried no outcome, including its explicit
recommendation to "park, and give the schedule surface an outcome instead". That surface now
exists, so the visibility half of that trade is live again. One asymmetry to decide when it is
taken up: a *parked* run stays in `running_actions` and therefore reports no outcome until
`schedule_run_timeout_seconds` kills it, at which point it reads `TIMED_OUT` — the same value a
wedged run gives. Whether that is enough is a decision this ADR deliberately does not take.
