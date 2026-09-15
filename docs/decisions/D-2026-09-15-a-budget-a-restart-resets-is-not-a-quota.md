# D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota — the per-user spend window becomes durable, and warns before it refuses

**Status:** accepted · **Date:** 2026-09-15 · Closes the `DEFERRED.md` row "Durable / rolling-window
budget quota", which named this design and is deleted in the same commit. Does not supersede
`D-066`; it finishes the half that ADR left in-process. The per-turn ceiling
(`agent/spend_cap.py`, `D-2026-08-29-an-iteration-cap-is-not-a-cost-cap`) is a different unit and
is untouched — it still ships at 0.

## Context

The evaluation that prompted this compared this tree against Paperclip
(`tasks/paperclip-ideas-2026-09-15.md`). Of that product's budget model — a monthly per-agent cap
that warns at 80% and pauses at 100% — two properties turned out to be things this repository
believed it had and did not.

**1. The per-user cap was per-pod, between restarts.** `api/budget.py`'s counters live in a
`BoundedLru` for the process's lifetime. Its own docstring said so — "the counters reset on restart"
— and the consequence was left as a property of the *implementation* rather than stated as a
property of the *cap*: `budget_max_tokens_per_user = 20_000_000` reads as "this person may spend
20M tokens" and in fact meant "20M per pod, between restarts". Three replicas multiply it by three;
a nightly roll multiplies it again; an LRU eviction resets one user's counter with no restart at
all. The setting's name and the guard's behaviour disagreed by an unbounded factor in the one
direction nobody wants.

**Measured before anything was changed**, against a migrated database
(`/tmp/.../scratchpad/vacuity.py`, reproduced as
`tests/test_budget_window.py::test_a_turn_booked_by_one_tracker_binds_a_second_one`): one tracker
books 900 tokens, a second tracker — the stand-in for a restart, an eviction or a second pod —
is asked to admit a turn against a 500-token cap, and **admits it**. That is the whole defect in
one line, and it is why the test is not vacuous.

**2. A budget's first observable signal was the refusal.** A 429 tells an operator about a cap on
the turn that was lost to it. Nothing said "80% spent" while there was still room.

## Decision

### The per-user window is durable; the per-session one is not

`infra/sql/100_budget_usage.sql` and `api/budget_store.py`. Engaged wherever
`session_store == "postgres"` — the same switch `agent/turn_cost.default_turn_cost_sink` and the
audit sink read, rather than a `budget_durable` flag beside it, which could only restate that or
contradict it (the argument `durable/schedules.py` makes three times over for asking the manifests
instead of adding an enable switch).

**Only the user scope, deliberately.** A session is bounded by `service_max_live_sessions` and dies
with the process that holds it, so a durable session counter would outlive the thing it meters. The
deferral asked for per-*user* fairness across restarts and pods; that is a property of a principal.

**One row per principal, reset in place.** The obvious shape — a row per `(actor, window)` — grows
one row per user per window for ever and needs a sweep, and a sweep that ships off (as retention
does) means it grows for ever in every shipped deployment. Resetting in place bounds the table by
the number of distinct principals ever served, which is the bound `budget_max_tracked_users`
already names for the map this backs. The reset is lazy and atomic, inside the upsert's
`ON CONFLICT` arm, so no second process can observe the row half-reset.

**The window is rolling and anchored at first use.** A calendar boundary would hand every user a
fresh allowance at the same instant. What it costs is stated in `api/budget_store.py`: a user who
spends their whole allowance in a minute waits the full window.

### `check` is async and `record` is not

Forced rather than chosen. `record`'s one production caller is `api/runner._book_turn_spend`, which
runs from a `finally`-driven teardown where an `await` re-raises a pending cancellation and skips
what follows it (D-130) — the constraint that already makes `agent/turn_cost.record_turn_cost`
synchronous by contract. So `record` books the in-process counters *synchronously* and schedules
the durable write exactly as that function does, with the same `_PENDING` strong-reference set.
`check` has no such caller — both sites are in `api/routes/turns.py`, one in an `async def` and one
in an async generator — so it can read the row before admitting a turn.

**The two halves combine with `max()`, not replacement.** The in-process counter has this pod's
just-ended turn immediately; the durable row has every pod's turns a moment later; neither is a
superset of the other. `agent/spend_cap.py` reads its own two sources the same way.
`test_the_in_process_counter_still_binds_before_the_durable_write_lands` is that gap asserted.

**An unreachable meter admits the turn.** Refusing a chemist's question because the meter is down
would make the guard an outage amplifier; the in-process half still bounds this pod. Degradation is
counted (`degraded(..., "budget_window", ...)`) because it is silent otherwise — the cap quietly
goes back to being per-process, which is precisely what this ADR removed.

### A warning fraction, booked from `record` and only from `record`

`budget_warn_fraction`, default 0.8. **From `record`, never from `check`**, because the front door
checks twice for every one turn — a fast path before taking an admission permit and the binding one
after it — so a warning emitted from `check` would count every turn twice and log it twice for a
fact that changed once. `test_checking_a_budget_never_warns` pins that.

Both ends of the band are excluded: under the fraction there is nothing to say, and at or past the
cap the *refusal* says it, to the caller rather than only to a log.

**It reaches a metric and a log, not the chemist**, and the setting's comment says so rather than
leaving it to be discovered. Putting it on the wire means a new member of the SSE `Event` union in
`api/events.py`, a coordinated change across `Chemclaw3_ui` and `Chemclaw3_mock` — the same reason
`AnswerEvent.challenged` is still declared at its default. "The user is warned at 80%" is what the
setting's name suggests and is not what it does.

## What the registers made explicit

Three declared sets went red on this change, and each asked a question worth answering:

- **`tests/test_degraded.py`** — the `subsystem` label space. `budget_window` is declared with its
  reason.
- **`tests/test_leaver.py`** — the erasure table set. A `budget_usage` row is a rate-limiting
  counter keyed by a principal, not an attributable record of who did what to the science, so it is
  **erasable** (`_ERASE`) where `turn_costs` is retained. Deleting it hands a departed person's
  successor a fresh window, which is correct: there is nobody left to meter.
- **`agent/session_store._ACTOR_SCOPED_ONLY`** — the guard refusing to let a table join the
  erasure without saying whether a *single session* delete should touch it. **No**, and the reason
  is a security property rather than a tidiness one: a chemist may delete their own session at
  will, so a `session_id` predicate would turn "delete the conversation" into "reset my quota" —
  available to exactly the runaway the budget exists to stop.

`durable/retention.py` refuses the table on the clock, with the argument above: one row per
principal, reset in place, so a sweep would reclaim one row per departed user and nothing else. The
grant is `INSERT, UPDATE, DELETE`, where the first two are the upsert's arms and the third is
offboarding's — the only thing that ever removes a row.

## What this does not do

- **It does not bound one turn.** `agent_max_turn_billed_tokens` still ships at 0, so the
  readiness record's accepted risk ("a turn's spend is unbounded at the shipped default") stands
  unchanged. This is a bound on a *sequence* of turns, which is what `api/budget.py` has always
  been.
- **It does not make the overshoot exact.** `check` and `record` are still separate calls, so a
  bounded number of in-flight turns pass `check` before any of them `record`. That bound is a
  property of where `check` is called, and is unchanged.
- **It does not reach the chemist.** See the warning section.
