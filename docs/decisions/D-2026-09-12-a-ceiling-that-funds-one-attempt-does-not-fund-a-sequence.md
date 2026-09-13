# D-2026-09-12-a-ceiling-that-funds-one-attempt-does-not-fund-a-sequence — A ceiling that funds one attempt does not fund a sequence

**Status:** accepted · **Date:** 2026-09-12 ·
**Extends:** `D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait`, and the bundle-scale
derivation `durable/publish.py::connector_queue_wait_timeout` carries

## Context

`connector_queue_wait_timeout()` is derived as `C - w - a` — the parent's execution ceiling, less
one worst-case attempt, less one activity's overhead — so that a queued activity's composite
`q + w` fits the ceiling **by construction**. Its docstring says so, and for a child that dispatches
once it is exactly right: `calc` and `results` each run one activity.

`BoCampaignWorkflow` runs six for a single-round campaign: propose the seed, evaluate it, propose
the round, evaluate it, record the round, record the campaign. It passed that same number to every
one of them.

Measured at the shipped settings:

```
ceiling 25200.0  queue bound 10170.0  bo activity 300.0  composite 10470.0  6x 62820.0
1 round, flat bound (before): waits [10170]*6  total 62820s  OVERRUNS by 37620
```

So the ceiling funds **two** of the six (20,940 ≤ 25,200) and breaks at the third. The overrun is a
`WorkflowExecutionTimedOut`, which reaches no workflow code: the chemist is told nothing, the
campaign's return value is lost, and the evaluations the run had already paid for go with it.

Three things make this worse than it looks:

- **The count is not six.** It is `3 + 3 × n_rounds`, and `n_rounds` is model-authored input bounded
  only by `bo_max_rounds` (500). Any fixed divisor would be wrong for most campaigns.
- **`continue_as_new` does not reset it.** `durable/connector_job.py` applies the ceiling as
  `execution_timeout`, which spans the whole continue-as-new chain; only a *run* timeout resets.
  `_carry_on_if_history_is_filling_up` continues the campaign several times over a long run and
  every continuation inherits the same spent budget.
- **The run cannot see how much it has spent.** `workflow.info().workflow_start_time` is the *run's*
  start, reset on every continuation, and nothing on `workflow.info()` carries the chain's.

## Decision

**A run spends its execution budget down, and each dispatch takes a share of what is left.**

`durable/publish.remaining_queue_wait_timeout(remaining, activity_seconds)` is the same subtraction
against what is left rather than against the whole ceiling, sharing one private `_queue_wait_seconds`
with `connector_queue_wait_timeout` so the two cannot drift. It returns `None` — not a zero or
negative `timedelta` — when what is left cannot fund another wait plus attempt, because Temporal
takes a non-positive `schedule_to_start_timeout` at face value and both readings say "the queue is
unserved" about a queue that is fine.

`BoCampaignWorkflow._queue_wait` takes the **minimum** of that and the queue-wide bound, since both
have to hold, and divides the remainder by `dispatches_left(rounds_remaining, seeding)` — the
activities still to come. `CampaignCarryOver.spent_seconds` carries the chain's elapsed time across
each `continue_as_new`.

### Why the division, measured

Bounding each dispatch by the whole remaining budget already makes the sum fit. It is not enough:

| | dispatches funded | waits |
| --- | --- | --- |
| flat bound (before) | 2 of 6, then overrun | 10,170 × 6 = 62,820 s |
| remaining budget, undivided | 3 of 6, then stop | 10,170 / 10,170 / 3,930 |
| remaining budget, shared | **6 of 6** | 3,870 / 3,876 / 3,884 / 3,894 / 3,908 / 3,938 = 25,170 s |

A three-round campaign shares the same 25,170 s across twelve dispatches at ~1,800 s each. The total
lands on `C - a` in both cases, which is the property `connector_queue_wait_timeout` claims for one
attempt, now holding for a sequence of any length.

**Getting the divisor wrong is a fairness bug, never a safety one.** `_queue_wait` divides what is
*left*, so whatever the count, each dispatch is bounded by the remaining budget and the sum cannot
exceed it; an over-count only makes early waits shorter than they needed to be. That is what lets
the count be re-synced from `rounds_remaining` at the top of each round instead of threaded through
every call — which matters, because a *measured* campaign's `_evaluate` opens a child workflow
rather than dispatching an activity and the running count would otherwise drift.

### Why one check per round is enough

The loop asks `_cannot_afford_another_dispatch()` once per round and covers that round's three
dispatches, and that is an argument rather than an optimism. `_queue_wait` hands out `R/n - w - a`,
so the next dispatch sees `R - R/n + a` over `n - 1`, which is `R/n + a/(n-1)` — strictly more than
`R/n`, which the check has just found to exceed `w + a`. Affordability is preserved as the share
decrements, and a real dispatch spends *less* than its allowance, which only widens the margin. So
`CampaignBudgetSpent` escaping mid-round is a state the arithmetic cannot reach; it stays a named
exception rather than a swallowed one, because a guard whose own reasoning is wrong should say so.

## Consequences

- A campaign that cannot finish inside its ceiling now **ends with what it has** — history, best
  point and a summary naming the ceiling and the rounds left unrun — instead of being killed
  mid-activity by a timeout nobody receives.
- The terminal `record_campaign_run` is **skipped** rather than attempted when the budget is spent.
  What makes that acceptable is the per-round write: an interrupted campaign is already resumable
  from the rows it left, which is the guarantee that write exists for.
- Every `bo` dispatch's queue allowance is now shorter than it was — 3,870 s rather than 10,170 s
  for a one-round campaign at the shipped ceiling. That is a real behavioural change and it is the
  point: three of six dispatches were previously drawing on a budget that did not exist.
- `calc` and `results` are untouched. `connector_queue_wait_timeout()` is unchanged for a child that
  dispatches once, and `test_the_job_ceiling_funds_exactly_one_worst_case_attempt_at_any_setting`
  still holds it.
- A *measured* campaign has no execution ceiling at all (`child_execution_timeout` returns None for
  `awaits_answer`), so there is no budget to share and the queue-wide bound is the whole answer.
- `connector_queue_wait_timeout`'s docstring said the composite fits "by construction" and it was a
  claim about one activity stated about every bundle child. It now says which.

## Related, recorded rather than fixed

`max_concurrent_workflow_tasks` is set nowhere: `durable/background_worker.py` sets
`max_concurrent_activities` only, so the workflow-task ceiling is the SDK default, and a child
workflow is not an activity — the worker's activity ceiling does not bound these children at all.
That is a different resource and a different decision; `docs/planning/BACKLOG.md` carries it.

## What keeps it true

- `tests/test_activity_queue_bound.py::test_a_campaigns_sequence_of_dispatches_fits_the_ceiling_they_share`
  — six worst-case dispatches at three ceilings, driven on the real `_queue_wait`, with the flat
  bound's overrun asserted beside it so the test cannot go vacuous.
- `tests/test_activity_queue_bound.py::test_a_campaign_never_waits_longer_than_the_queue_wide_bound`
  — the case where the `min` is what decides.
- `tests/test_activity_queue_bound.py::test_a_campaign_that_has_spent_its_ceiling_refuses_to_dispatch_again`
- `tests/test_activity_queue_bound.py::test_a_campaign_with_no_execution_ceiling_keeps_the_queue_wide_bound`
- `tests/test_activity_queue_bound.py::test_a_remaining_budget_that_cannot_fund_an_attempt_answers_none`
- `tests/test_activity_queue_bound.py::test_every_dispatched_activity_call_bounds_the_queue_wait`
  — unchanged, and still the rule that every new dispatch site passes *some* queue bound.
