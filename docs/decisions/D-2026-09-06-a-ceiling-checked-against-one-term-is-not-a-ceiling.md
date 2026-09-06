# D-2026-09-06-a-ceiling-checked-against-one-term-is-not-a-ceiling — the fan-out and schedule-run ceilings each bounded a fraction of what they had to contain

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:**
D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait (a `start_to_close` says nothing
about the queue), `durable/publish.py::connector_queue_wait_timeout` (the composite a parent must
contain is `q + w`, and subtracting makes it fit by construction), D-093 (a fan-out child must be
able to fail for isolate-and-drop to mean anything) ·
**Corrects** `Settings._the_fan_out_ceiling_covers_the_section_it_bounds`, whose own docstring
called itself "the same rule as" the connector one while checking half of it, and the
`fan_out_child_timeout_seconds` comment that claimed `Settings` checked it "against the section
budget it has to contain".

## Context

A limits review re-derived the whole timeout ladder from live `Settings()` and drove the real
broker at 1000:1. Three links held exactly. Three did not, and all three are the same mistake: a
ceiling was checked against *one* of the terms it has to contain.

## What was measured

**The fan-out ceiling equalled the queue wait it had to contain.**
`fan_out_child_timeout_seconds` is 3,600 s; both fan-out children passed core's flat
`queue_wait_timeout()` — also 3,600 s — as their `schedule_to_start`, beside a 300 s section or a
120 s note write. So the composite a child may legally spend was 3,900 s under a 3,600 s ceiling,
and the child's own `SCHEDULE_TO_START` expiry could never be observed. On the real broker at
1000:1, ceiling == wait gave `ChildWorkflowError: Child Workflow execution timed out` for both
children — dropped by `fan_out` with no cause, because an execution timeout is not delivered to
workflow code. Above `q + w` the same run gave `retrieval_failed:TimeoutError`, which is the
degradation `ReportSectionWorkflow` was written for and which `activity_failure_reason` turns into
"nothing is serving that queue".

**The schedule run ceiling did not fund a bounded run.** Four Schedules bound their own run by an
iteration count and continue as new; `schedule_run_timeout_seconds` (86,400 s) is the `run_timeout`
on that run. Nothing checked the product. Work only, with zero queue wait: `corpus_sync`,
`document_sync` and `label_sync` each needed 90,900 s, and `document_sync`'s loop dispatches
**three** activities per iteration rather than one, so it needed 270,900 s. A run killed at the
ceiling is a `TIMED_OUT` no `except` sees; the setting's own paragraph names `corpus_sync` (release
mode) and `document_sync` as keeping no row between fires, so the next fire restarts from page one.
A corpus large enough to use its iteration budget therefore burnt a day of a worker slot per fire
for zero net progress — the wedge the ceiling exists to prevent, arrived at from the other side.

**`mirror_commitments_activity` had no heartbeat timeout**, the one gap in an otherwise clean
eleven-pair sweep. It reaches an external portfolio system and then writes every row it got back
under a 300 s `start_to_close`, so a worker dying mid-mirror was invisible for all of it.

## Decision

**The fan-out children get their own derived wait**, `durable/publish.py::fan_out_queue_wait_timeout`
— `C - w - a`, the same construction as the connector one and for the same argument: a *fraction*
makes `q` grow with the ceiling it must fit inside, and no fraction fixes that, while subtracting
makes the composite `C - a` whatever the three numbers are. At the shipped settings the wait is
3,270 s, still nine tenths of core's hour, so **no deployment loses a wait it was using**. The
validator now reserves one activity's overhead, which is what keeps that wait positive;
`longest_fan_out_activity` is the single definition of the max, for the reason its bundle twin
gives.

**A validator refuses an iteration count that cannot finish inside the run ceiling.**
`_BOUNDED_DRAINS` declares each drain's iteration setting, its per-activity budget, and how many
activities one iteration dispatches; a test derives that third column from each workflow's own AST,
so the declaration cannot go stale in silence. **The queue wait is deliberately not charged**: at
the shipped hour it would dominate every term and force `document_sync_max_iterations` to 6, and a
run that overruns because its activities are unclaimed is precisely the stuck run this ceiling is
the backstop for. A run that overruns doing its own budgeted work is the defect, and that is what
is refused.

**What the defaults cost, stated because they are shipped defaults that moved.**
`corpus_sync_max_iterations`, `label_sync_max_iterations` and `eln_sync_max_iterations` go 100 → 90;
`document_sync_max_iterations` goes 100 → 30, a third of its siblings' because its loop dispatches
three activities per iteration. All four land at ~81,900 s against 86,400 s. The cost is one extra
`continue_as_new` per 90 (per 30) iterations — the hop carries the drain's position in `state`, so
nothing is re-read and no cursor is lost — against a run that could not finish before.

`mirror_commitments_activity` gains `commitment_sync_heartbeat_timeout_seconds` (60 s, a fifth of
its budget, the ratio `eln_sync_heartbeat_timeout_seconds` uses) and goes through
`durable/heartbeat.py::beating`, with the pass extracted to `_mirror_one_source` so the activity is
the heartbeat wrapper and nothing else.

## What holds it

- `tests/test_activity_queue_bound.py::test_the_fan_out_ceiling_funds_one_worst_case_child_at_any_setting`
  — parametrized over three ceilings, so a wait re-derived as a fraction of the ceiling fails it.
- `…::test_every_fan_out_child_waits_on_the_fan_out_bound_not_on_cores_hour` — the composite fits
  by construction only for a call site that uses the derived wait, and the defect was that two did
  not.
- `tests/test_config.py::test_every_bounded_drain_can_finish_a_run_inside_the_ceiling_that_kills_it`
  and `…::test_the_dispatch_count_each_bounded_drain_declares_is_the_one_it_runs`.
- `tests/test_durable_heartbeat.py::test_every_beating_activity_in_durable_is_dispatched_with_a_heartbeat_timeout`
  — derived from the tree in both directions, so a beat nobody listens for and a timeout over an
  activity that never beats both fail. Every one of these was watched failing against the unfixed
  source.

## What is deliberately not done

`BoCampaignWorkflow` runs at least four sequential activities under `connector_job_timeout_seconds`,
so `connector_queue_wait_timeout`'s "fits by construction" — a bound on **one** `q + w` — is
4 × (10,170 + 300) = 41,880 s against a 25,200 s ceiling. The elegant fix is for that child to
continue as new per round, which is a change to `connectors/bo/workflows.py` and belongs with that
bundle. It is a `BACKLOG.md` row.

The residual on the run ceiling is real and is not argued away: under sustained backpressure a
bounded run can still be killed, and for the two jobs that keep no row between fires that still
costs the whole drain. The cursor's absence, not the arithmetic, is what makes that expensive, and
it is its own row.
