# D-2026-10-02-a-prefix-beyond-the-derivation-basis-is-paid-in-spend-not-thread — what a deployment that binds more bundles than the chart pays for them

**Status:** accepted · **Date:** 2026-10-02 · **Amends**
`D-2026-09-04-a-budget-that-excludes-the-prefix-is-not-a-budget` (the prefix is still charged
unconditionally, now up to a stated basis) · Closes issue #504 (live re-verification 2026-10-02,
defect D8).

## Context

Both context triggers are a prefix bound plus a thread allowance:
`agent_context_token_budget` and `agent_tool_result_clear_trigger` are derived from
`tests/test_context_floor.PREFIX_BOUND`, which is the chart's connector surface.
`context_budget.effective_trigger` then subtracted the request's *whole* measured prefix. A
deployment may bind more bundles than the chart
(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`); doing so moved
the prefix and not the budgets, so every token of extra schema came out of the thread.

Measured 2026-10-02 through production's own connector open and graph build, inside the four-repo
lane that binds every bundle the fleet publishes (`infra/live/e2e-full-stack/up.sh` has done so
since #470):

| connector set | prefix (estimated) | window's thread | lossless edit's thread |
|---|---|---|---|
| default-enabled bundles | 85,790 | 32,910 | 25,810 |
| all 15 bundles (the lane) | 109,743 | **8,957** | **1,857** |

The extra 23,953 is the five process-development bundles and `pyexec` (22,322 of schema), their
prompt sections (848) and one in-process tool they enable. The live log matched to 15 tokens
(`budget of 8942`). The estimator ratio played no part: the measured value is the ratio-1.0 value.

What it cost, live with DeepSeek V4 Pro: tool results were cleared on almost every model call, so
results were dropped before the model had read them. Each clearing forgave the repeat guard
(`compaction._record_reduction` → `repeat_guard.forget_calls`), so the model re-fetched them
without limit, and research turns ended at `loop_cap_reached`. Nothing warned, because neither
trigger reached the floor of 1 that `context.trigger_floored` reports. A capped turn's wrap-up then
answered "No question has been asked yet", which is a second defect fixed beside this one (below).

## Options

1. **Keep charging the whole prefix to the budget** (the old behaviour). The request bound holds,
   and the thread is whatever is left. Measured above, that is not a working agent. It also does
   not save spend: a turn looping to the 25-call cap re-sends a 110k prefix 25 times.
2. **Raise the defaults until the lane fits.** That moves every deployment's request bound for one
   lane's sake, and on a 128k model it re-opens `D-2026-09-04`'s context-length failure.
3. **Charge the budget the prefix up to the basis it was derived for, and pay the excess in
   spend.** The thread keeps exactly what the derivation gives it at the bound, whatever a
   deployment binds. A request may then bill past `agent_context_token_budget` by the excess.

## Decision

Option 3.

- `agent_context_prefix_basis` is `PREFIX_BOUND` by derivation, and
  `tests/test_compaction.py::test_the_prefix_basis_is_the_bound_both_defaults_are_derived_from`
  asserts the equality. `effective_trigger` subtracts `min(prefix, basis)` from the converted
  budget, so a deployment at or under the bound behaves exactly as before.
- **A declared `llm_context_window_tokens` is still charged the whole prefix.** A provider's limit
  is not a budget, and no basis buys room a model does not have. A deployment that binds more than
  its window can hold is starved there, and that is the honest outcome.
- The excess is said once per surface at WARNING (`context.prefix_over_basis`), naming the prefix,
  the basis and the three remedies: bind fewer bundles, declare the window, or raise the basis and
  both budgets together. Before this, the same condition was silent.
- `agent_max_turn_billed_tokens` still bounds the turn's spend. That cap was always the spend
  bound, while the per-request budget is what the thread allowance is derived from.

**Beside it, and not a separate decision: a request-only note is not a conversation group.**
`loop_cap.AnswerAtTheCap` appends its wrap-up instruction as a `HumanMessage` from outside the
compaction group, so the window read it as the newest group, protected it, and cut the chemist's
question. It is now built with `compaction.request_note`, and the window's group starts skip it.
That means the latest message the chemist sent is always kept.
`tests/test_compaction.py::test_the_wrap_up_at_the_cap_still_carries_the_chemists_question`
drives it through the compiled graph and fails on the old code with exactly the live symptom: the
wrap-up call carried the note alone.

## Consequences

- On the lane, the window's thread goes from 8,957 to 34,850 and the lossless edit's from 1,857 to
  27,750 (measured with this branch's code against the same 109,743-token prefix). Each request
  may bill about 26k more than the 118,700 budget, on a 1M-token model.
- A deployment on a 128k model that binds extra bundles and declares no window may now meet a
  provider context-length error where it used to meet a starved thread. Declaring the window is
  the remedy, and the warning names it. The chart declares one.
- `tests/test_context_floor.SERVED_ELSEWHERE_ALLOWANCE` is breached today (`chem` grew to 13
  tools: 11,723 against 11,000). The check skips wherever the sibling has no `.venv`, CI
  included, so a chart deployment is about 700 over the basis and logs the warning once. That is
  queued in `docs/planning/BACKLOG.md` rather than absorbed here.

**Revisit when:** a deployment's measured prefix approaches its declared window, so the window arm
rather than the basis decides its thread (`context.trigger_floored` naming a declared window is the
signal). Or when deferred tool schemas (`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`)
are built, which would make the bound surface, and so the basis, a per-turn quantity.
