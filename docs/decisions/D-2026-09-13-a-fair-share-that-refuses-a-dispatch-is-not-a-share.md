# D-2026-09-13-a-fair-share-that-refuses-a-dispatch-is-not-a-share — affordability is a property of what is left, never of the divisor

`D-2026-09-12-a-ceiling-that-funds-one-attempt-does-not-fund-a-sequence` made
`BoCampaignWorkflow._queue_wait` divide what is left of the execution ceiling between the dispatches
still to come, so a one-round campaign's first step could not eat the whole of it. That division is
right and it is *fairness*. It was then also used to decide **affordability**, and that made every
long campaign fail before doing any work.

## What was measured

`dispatches_left(n, seeding=True)` is `3n + 3`. At the shipped settings — a 25,200 s ceiling, a 300 s
`bo` activity and 30 s of overhead — the share falls under one attempt plus its overhead at 25
rounds, and `remaining_queue_wait_timeout` then answers `None`, which `_queue_wait` raises on.
Driven against the real method with a stubbed clock:

    n_rounds=   1 dispatches=    6 first_wait=3870.0s
    n_rounds=  10 dispatches=   33 first_wait= 433.6s   <- the default spec
    n_rounds=  24 dispatches=   75 first_wait=   6.0s
    n_rounds=  25 dispatches=   78 -> CampaignBudgetSpent
    n_rounds= 500 dispatches= 1503 -> CampaignBudgetSpent

`bo_max_rounds` is 500, so every campaign from 25 rounds up to the ceiling the settings themselves
permit died on its **first** activity — `propose_initial`, with the whole 25,200 s untouched in
front of it. That dispatch is neither guarded nor wrapped, and
`@workflow.defn(failure_exception_types=[Exception])` turns an escaping exception into a workflow
FAILURE, so what reached the chemist was a failure whose message is the empty string.

`dispatches_left`'s own docstring already contained the refutation: *"being wrong here is a fairness
bug, never a safety one"*, because each dispatch is measured against what is **left** and so the sum
fits whatever the divisor is. A share may therefore narrow a wait; it must never refuse one.

The second harm is the opposite sign and it reaches a healthy campaign. At the default ten-round
spec every dispatch went from the 10,170 s queue-wide bound to 433.6 s, so a `bo` worker rolling,
scaled to zero or merely slow to pull expires `schedule_to_start` — the misdiagnosis
`connector_queue_wait_timeout`'s own docstring warns about. The sharing made the common case worse
in order to improve a case (every dispatch waiting its full allowance) in which the campaign is
already lost.

## The decision

**Affordability comes from the remaining budget, undivided; the share only narrows; and a share
below a configured floor is not a wait.**

- `_queue_wait` asks `remaining_queue_wait_timeout(remaining, …)` first and raises
  `CampaignBudgetSpent` only on that. The share is computed after, and the answer is
  `min(queue_bound, affordable, max(fair, floor))` — both real bounds still hold above the floor,
  so the floor can lengthen a share and can never lengthen a dispatch past what the run or the
  queue can fund.
- `bo_queue_wait_floor_seconds` (default 900 s) is the floor. It is a setting rather than an
  arithmetic because it answers a deployment question — how long a `bo` worker may be absent
  before a campaign gives up on it — and fifteen minutes is comfortably above a rolling restart
  and far below the queue-wide ceiling.
- `_cannot_afford_another_dispatch` asks the same question on the same basis. Reading it off the
  share made the loop's stop condition and the dispatcher's refusal disagree in the direction that
  stops a campaign the dispatcher would have funded.
- The terminal `record_campaign_run` sets `_dispatches_left = 1` first: it is the last dispatch, and
  `_dispatches_left` was last re-synced at the top of a round that has since finished. Before this
  commit that write was unaffordable **by construction** on every budget-stopped campaign —
  `_cannot_afford_another_dispatch` deliberately does not consume a share, so the divisor at the
  terminal write is still `3R + 1` — and the campaign threw away the `campaign_id` that
  `resume_campaign` and the report both key on.
- A campaign that ran every round and could only not fund its terminal record no longer says "It
  stopped with 0 round(s) unrun … Re-run it to continue from here".

`CampaignBudgetSpent`'s docstring said "caught by `run`" and `run` catches it round the terminal
write only. It now names the three sites and states why the seed's two dispatches need neither: a
fresh run's first dispatch is affordable by construction (`Settings` refuses a ceiling that cannot
fund one attempt) and a resumed run does not seed. That was true only once affordability stopped
being read off the share.

## What keeps it true

`tests/test_activity_queue_bound.py`:

- `test_a_long_campaign_dispatches_its_seed_instead_of_refusing_it` — parametrised over 25, 100 and
  `bo_max_rounds`, with its own vacuity guard: it asserts the round count still produces a share
  below one attempt, so a later change to the divisor cannot make it pass by not testing anything.
- `test_a_shared_queue_wait_never_falls_below_the_configured_floor` — and that the floor is a floor,
  not an override: a campaign down to its last minute is still bounded by what it can afford.
- `test_the_floored_share_still_fits_the_ceiling_every_dispatch_shares` — the worst case on the
  longest campaign this deployment accepts, every dispatch waiting its whole allowance, still
  inside the ceiling less one activity's overhead.
- `test_a_campaign_that_stops_for_budget_can_still_write_its_terminal_record`.

Two mutations, each restored from a `.bak`:

| mutation | result |
| --- | --- |
| affordability read off the share again (the merged state) | red — 5 failed, 18 passed |
| the floor removed, `fair` used bare | red — `test_a_shared_queue_wait_never_falls_below_the_configured_floor` |

The summary wording is the one change here with no test of its own: it is a sentence, guarded by
the existing full-campaign tests only.
