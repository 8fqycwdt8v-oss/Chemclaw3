# D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it — fourteen of the twenty background workflows declare their failure types, six deliberately do not

**Status:** accepted · **Date:** 2026-08-27 · Closes the scope `D-2026-08-16-a-job-that-cannot-fail-is-a-job-that-hangs`
left open, without widening its test.

## Context

`D-2026-08-16` gave every workflow on the **job path** `failure_exception_types=[Exception]`, because
the Temporal SDK parks a plain exception raised in workflow *code* in an internal
workflow-task-failure loop that ignores the retry policy and never gives up — measured live as a
parent `RUNNING` indefinitely while `get_durable_job_status` answered `running` for a job that would
never finish. It scoped that deliberately and said so: the periodic workflows "have nobody waiting on
a turn, so the same trade lands differently for them and is a separate decision."

This is that decision, taken one workflow at a time. Two facts decide each one, and both were read
off the code rather than assumed:

**(a) Who starts it, and is anything waiting?** Not "is it periodic" — a workflow can have more than
one starter, and the second one is what decides several of these. `synthesize_memory` and
`request_development_report` start five of them for a named chemist and hand back an id to poll;
`request_note_reindex` starts one from a merge webhook; `cli.live_data.backfill` starts one and then
awaits `handle.result()`. **None of those four call sites passes an `execution_timeout`**
(`agent/durable_tools.py`, three `start_workflow` calls; `cli/live_data.py`, one). A parked run there
is the D-2026-08-16 hang exactly, with nothing to end it.

**(b) What bounds the park, and what does the run hold?** A Schedule-started run is different, and the
difference is already in this tree: `_build_schedule` sets `execution_timeout=schedule_run_timeout_seconds`
(a day) and `ScheduleOverlapPolicy.SKIP`. So a parked scheduled run is bounded — and
`config/temporal.py` states, at the setting itself, why terminating one is safe: "each of these jobs
is cursored or idempotent, so the next fire picks up where this one was cut off."

Three measurements shaped the conclusions, and one of them cuts against declaring:

- **The stance behaves as documented.** Against the time-skipping server, one `ValueError` raised in
  workflow code: the declared workflow reached `FAILED` with an `ApplicationError` on its **first**
  attempt; the undeclared one was still `RUNNING` five seconds later with the worker re-failing and
  re-polling the same poisoned task, and nothing ending it. That is now
  `tests/test_workflow_registry.py::test_the_two_stances_behave_as_the_table_assumes` rather than a
  claim, because the whole table below rests on it.
- **Declaring `[Exception]` also converts a `NondeterminismError` into a workflow failure.** Read out
  of the installed SDK, not the documentation: `worker/_workflow.py::nondeterminism_as_workflow_fail_for_types`
  selects exactly the workflow types whose `failure_exception_types` cover it. So the declaration
  costs a run its ability to survive a rollback — which is the one place parking genuinely helps, and
  the reason `memory_jobs.resolve_notes_per_run`'s docstring is written the way it is.
- **A failed scheduled run is *less* visible than a parked one on the surface this deployment has.**
  `ScheduleHealth` reports `paused`, `last_run`, `runs_total`, `skipped_overlap` and `running_now` —
  and **no run outcome at all**. A run that fails every fire leaves that page looking healthy;
  a parked one at least pins `running_now` at 1. So "declaring makes it visible" is false here, and
  any argument resting on it had to be dropped.

The queue holds **twenty** workflows. After this decision fourteen declare and six park; ten of
the fourteen are declared *by this ADR*, and the other four were already the job path.

## Decision

**The rule.** Declare `failure_exception_types=[Exception]` where the park has **no ceiling, or a
ceiling somebody is waiting through**. Leave it parking where the only starter is a Temporal
Schedule, because there the run is already bounded at a day, nothing reads its result, and the work
is cursored or idempotent by that Schedule's own stated contract — so what a failure would buy is a
terminal state no surface reports, and what it costs is the run's chance to finish once a same-day
redeploy fixes the bug.

`tests/test_workflow_registry.py::test_every_background_workflow_holds_the_stance_argued_for_it`
holds both halves: a `_MUST_FAIL` workflow that stops declaring goes red, **and a `_MAY_PARK`
workflow that starts declaring goes red too**. A workflow in neither set goes red as undecided, so a
new one on the queue forces the question instead of inheriting whatever its neighbours did. Mutation-
checked in all three directions.

### The table

| Workflow | Stance | Why |
| --- | --- | --- |
| `CampaignSynthesisWorkflow` | **fail** | `synthesize_memory` starts it for a named chemist, no `execution_timeout`, polled by `get_durable_job_status`. The job path in everything but its queue. |
| `PlaybookDistillationWorkflow` | **fail** | as above |
| `OptimizationCampaignWorkflow` | **fail** | as above |
| `ObservationPromotionWorkflow` | **fail** | as above — and note its *sibling in the same file* parks, which is the point of deciding per workflow rather than per module |
| `DevelopmentReportWorkflow` | **fail** | `request_development_report`, same shape; it returns the `ConnectorJobResult` envelope precisely so `get_durable_job_status` can answer for it. Also runs real logic outside an activity (`_reconcile`), so the plain exception is not hypothetical |
| `PublishNoteWorkflow` | **fail** | fan-out child of the three synthesis jobs. `fan_out` drops a child that *fails*; a child that *parks* is dropped only when `fan_out_child_timeout_seconds` expires — **3600 s by default, per batch** — and that hour is charged to a parent the chemist is polling. Same outcome (logged, counted, siblings unaffected), an hour sooner |
| `ReportSectionWorkflow` | **fail** | fan-out child of the report; identical argument, and `_reconcile` already turns a dropped section into a visible `retrieval_failed` marker, which is what makes failing safe here |
| `NoteReindexWorkflow` | **fail** | the *webhook* starter decides it: `request_note_reindex` starts it per calendar minute with no `execution_timeout` and `ALLOW_DUPLICATE_FAILED_ONLY`, so a parked run is immortal, its id can never be reused, and every merge adds another run re-polling a poisoned task for the life of the deployment — while hybrid retrieval serves the stale index that module calls worse than no index |
| `ElnSyncWorkflow` | **fail** | `cli.live_data.backfill` starts it with an explicit `since`, no `execution_timeout`, and then **awaits its result**. Failing costs at most the chunk in flight, because `store_sync_cursor` persists the cursor after every chunk |
| `EvalDriftWorkflow` | **fail** | the one Schedule-only job that declares, and the reason is its output rather than its starter: the alerts are computed *before* the park and replayed from history, so a resumed run delivers a day-old regression verdict onto the operator channel as current — including one since fixed. It also defeats the must-deliver stance `run()` already states in writing |
| `ArtifactEvictionWorkflow` | **park** | Schedule-only; nothing reads it; one pass is an unconditional policy sweep, so a skipped fire costs a day of blobs the next pass reclaims with the rest. No party is misled — D-2026-08-16's harm was a chemist told `running`, and there is no chemist here |
| `PublishResultsWorkflow` | **park** | Schedule-only, and this module has *already argued* that a workflow failure is the wrong signal for it: rows stay `pending`, and `result_publications.attempts` plus the `queued_total − published_total` backlog say what is wrong more precisely. Those signals are undisturbed by a park, a failure or a timeout alike, so the run's end state carries nothing an operator reads |
| `ObservationSynthesisWorkflow` | **park** | Schedule-only; a full re-mine with no cursor, so a skipped fire costs a `last_seen` refresh the next fire redoes, and the retirement windows are weeks — a day-late pass is the same pass |
| `DocumentShareSyncWorkflow` | **park** | Schedule-only. Shaped exactly like `ElnSyncWorkflow`, and parked where that one fails, because the difference is the starter and not the shape — this one has no uncapped caller. It also keeps no cursor between runs *by design*: the next fire re-walks from the top, which its own docstring prices at a `scandir` |

### Grandfathered, and said out loud

`ReactionCorpusWorkflow` and `ReactionLabelWorkflow` already declare, from their own merged in-line
argument. **The rule above would not have required either** — both are Schedule-only cursored drains,
the same category as `DocumentShareSyncWorkflow`. Their stated reason is also inaccurate: they say a
"genuine bad-data failure looks like a run that is still going, forever", but bad data surfaces as an
`ActivityError`, which is already a `FailureError` and already fails the run. Reversing a merged
declaration on a tie is not worth the churn, and neither file was this session's to edit, so both are
recorded in `_MUST_FAIL` — which means removing one is a decision someone has to take rather than a
tidy-up. Their owners may reverse it; this ADR is the argument they would need.

### Recommended, not applied

Three workflows live in files another session held open. The analysis is here so it can be applied
separately.

- **`RetentionWorkflow` (`durable/retention.py`) — park.** The backlog row named this one as worth
  arguing about, "since a parked run there is invisible in exactly the way the fan-out drop was". The
  invisibility is real; the remedy is not this decorator, because **it is symmetric**. `ScheduleHealth`
  reports no run outcome, so a retention run that fails every fire is invisible in precisely the same
  way — and `runs_total` climbing with `last_run` advancing makes the page read *healthier* than the
  parked case, where `running_now` sticks at 1 and `skipped_overlap` climbs. On the substance the rule
  applies unchanged: Schedule-only starter, single idempotent age-cutoff activity, nothing reads the
  result, and a skipped day prunes with the next pass. The real gap is that `ScheduleHealth` carries
  no outcome for its recent actions; fixing that would make *both* stances visible and is the change
  worth making, rather than converting one unreported terminal state into another.
- **`DigestWorkflow` (`durable/digest.py`) — park.** Schedule-only; the watermark advances only after
  delivery, so a run that never finishes re-reports rather than skipping — the property that file was
  built around. A day-late digest is the digest.
- **`ConnectorJobWorkflow`, `TemplateWorkflow`** — already declared, correctly, by D-2026-08-16 and
  REV-13. Named in `_MUST_FAIL` so the table is a complete account of the queue rather than a partial
  one that reads as complete.

## What this was verified against

- The two stances, live on the time-skipping server: declared → `FAILED` first attempt; undeclared →
  `RUNNING` after 5 s with the task re-failing. Kept as a test, not a paragraph.
- The guard, mutation-checked three ways: removing the declaration from `EvalDriftWorkflow` →
  red; adding one to `ArtifactEvictionWorkflow` → red; dropping a name from the table → red as
  undecided.
- `nondeterminism_as_workflow_fail_for_types` read out of the installed SDK, because the cost of
  declaring turns on it.
- The four uncapped start sites read directly: no `execution_timeout` on any `start_workflow` in
  `agent/durable_tools.py` or `cli/live_data.py`.
- `uv run mypy --strict` and the targeted suites green.
