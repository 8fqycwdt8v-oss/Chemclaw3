# Four ideas from Paperclip, planned

Source: an evaluation of <https://paperclip.ing> against this tree (2026-09-15). Of its five
pillars, three dismissals in the first pass were wrong — checked against the ADRs rather than
against a summary of them — and the four items below are the ones that survived with high
confidence.

**Why this file and not `tasks/todo.md`.** That file holds the waves 21–30 plan, which is live work
belonging to another effort. Overwriting it to satisfy a convention would destroy it.

## What the re-reading established (the premises this plan rests on)

1. **Timers are not forbidden; timers that *decide* are.** `durable/schedules.py` holds 15 owned
   Schedule ids and conditionally plans ~12. `planned_schedules()`'s own docstring: *"What is left
   on a timer is ingestion, indexing, eviction and retention: jobs that make the corpus queryable
   and none that decide what it means."* And `durable/awaiting.py` already wakes **humans** on
   timers (`reminder_hours`, escalation, deadline). Nothing wakes the **agent**.
2. **The revision loop is an open gap, conceded in an ADR.**
   `D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer`: *"The gap is real and is
   not in dispute … Nothing routes a flagged answer back for another pass."* What was declined is
   `RubricMiddleware`'s implementation (it builds its own grader with no seam for `score_answer`,
   and every non-satisfied termination ships the answer unmodified), not the capability.
3. **Nothing re-checks a week-old answer's premise.** `AwaitRequest` holds a question open for up
   to 90 days; `Answer.payload` is opaque and the answer path re-validates nothing.
4. **The shipped configuration bounds no single turn's spend.** `agent_max_turn_billed_tokens`
   ships at 0; `api/budget.py` is in-process and reset by restart or LRU eviction.

## Items

- [x] **A — durable per-actor budget with a warning threshold** — `D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota`.
      Measured defect first: a second `BudgetTracker` (a restart, an LRU eviction or a second
      pod) **admitted** a turn against a 500-token cap after 900 were spent. Now one Postgres
      row per principal on a rolling window, reset in place; `check` async and `record` still
      synchronous because it runs in a `finally` (D-130); `budget_warn_fraction` warns from
      `record` only, since the front door checks twice per turn. Three declared registers went
      red, the sharpest being `_ACTOR_SCOPED_ONLY` — a `session_id` predicate there would have
      made "delete the conversation" a free quota reset.
- [x] **B — the answer's premise is re-checked before it is applied** —
      `D-2026-09-15-an-answer-days-later-is-answered-against-a-corpus-that-moved`. The
      fingerprint design was built on paper and abandoned: this tree has no arrival signal for
      a note, and the two readings would come from two pods whose knowledge checkouts drift.
      What ships asks the narrower question at *both* ends — the ask refuses an already-broken
      premise, which is what makes an answer-time break mean *since*, by construction. The
      premise is `cited_ids(subject + rationale)`, derived and never an argument. Found and
      fixed a latent bug in `_alert_expressions()` on the way past.
- [x] **C — a flagged answer is routed back for another pass, counting only agent-initiated
      rounds** — `D-2026-09-15-a-flagged-answer-that-goes-out-flagged-is-a-verdict-nobody-acted-on`.
      One premise of the original plan was wrong and is corrected: `score_answer` **already**
      fails closed (a crash sets `review_required`), so C did not need to fix that. What it
      needed was the loop itself, in the runner rather than a middleware, because the verdict
      is produced after the graph returns. Exhaustion ships the answer still marked — the arm
      D-2026-08-16 found `RubricMiddleware` lacking.
- [x] **D — a scheduled check-in over a requester's own blocked work** —
      `D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late`. **The plan promised an
      agent heartbeat and this deliberately is not one**, for a reason found while building
      it: `run_agent_step` takes a `StepIdentity`, because a worker has no request context and
      an agent step is run *as* somebody — and a Schedule has nobody to be. Synthesizing an
      identity from a `requested_by` string is not a question the read-only narrowing answers,
      since a narrowing bounds what an actor may do and does not supply the actor. The gap the
      item was really about is still closed: `awaiting.py` re-notifies `asked_of` and tells the
      requester only on expiry, so a 90-day wait meant three months of silence.

Ordered by ascending blast radius, so each lands green before the next starts.

## Verification plan (planned up front, not after)

- Every item ships with tests that drive the real path (a compiled graph, a live workflow
  environment, a migrated database) rather than asserting on a mock — the standing rule here, and
  the one `tests/test_state_channels.py` exists to enforce for channels specifically.
- `make lint type test` green, **with `dockerd` up**, and the skip count reported. A run without
  Docker skips 216 tests and is not evidence about the durable layer.
- Each item states what it would look like if it were wrong, and a test asserts that.
- An ADR per item, plus its `docs/decisions/README.md` ledger row.

## Review

All four shipped, each with an ADR, its ledger row, a topic-table entry and tests that drive the
real path. `make lint type test` green with Docker up; the full run is reported in the PR body with
its skip count, because a run without `dockerd` skips ~216 tests and is not evidence about the
durable layer.

**What the work changed about the plan.** Four of the premises this file opened with survived; three
things did not, and each was found by building rather than by reading:

1. **C did not need to fix a fail-open verifier.** The plan said `score_answer` had to be stopped
   from shipping an answer clean on a grader crash. It already fails *closed* — a crash sets
   `review_required = True`. The real gap was only the missing loop.
2. **B's fingerprint design was wrong and could not be made right.** There is no arrival signal for
   a note anywhere in this tree, and the two readings would have come from two pods whose knowledge
   checkouts drift by minutes. Asking the narrower question at *both* ends establishes "since" by
   construction instead.
3. **D is not the agent heartbeat the plan promised**, and the reason is worth more than the feature
   would have been: an agent step is run *as* somebody, and a Schedule has nobody to be.

**Three bugs found in existing code on the way past**, none of them in the new features:

- `_alert_expressions()` terminated on `for:`, which is optional in a Prometheus rule. All 51
  existing rules happened to carry one, so the guard against false coverage was correct by
  coincidence; the first rule without one read a metric named in prose as alerted.
- `budget_usage` reaching `session_store._ACTOR_SCOPED_ONLY` surfaced that a `session_id` predicate
  there would have made "delete the conversation" a free quota reset.
- My own absence test for D scanned a sixth of the file it was guarding (16.6%; it shipped here
  saying 18%, measured against the file mid-development). Rewritten over the AST and
  proven by planting a violation where the first version could not see.

**Two mistakes of mine worth recording.** A `git checkout` meant to revert a probe reverted an
hour of unrelated work in the same file — probes now go through a scratchpad copy. And the first
`open_days` arithmetic was a tautology returning 0 for every row, caught by writing the test that
read it.
