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
- [ ] **D — an agent heartbeat: the agent wakes on a timer to report and ask, never to decide**

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

(filled in at the end)
