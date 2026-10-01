# D-2026-10-01-a-stop-by-the-clock-is-named-not-retried — what a calculation the server's time budget stopped is

**Status:** accepted · **Date:** 2026-10-01 · Closes the `BACKLOG.md` row *"The calc server's
inline time budget refuses as bad data, so a load-dependent stop reads as a property of the
molecule"*, opened by the review of
`D-2026-09-27-a-screen-answers-per-item-a-distribution-refuses-by-name`.

## Context

`Chemclaw3-mcp`'s `servers/calc` bounds every in-process calculation with a wall clock
(`engine/budget.Deadline`, `CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS`). A stop raised a plain
`ValueError`, which reached this repository as `CalcToolError`, the class for a refusal of the
input. Wall clock depends on what else the pod is running, so the same relaxation finishes on an
idle pod and is stopped on a busy one.

Before the per-item ADR this misclassification failed the whole job. Since that ADR, a screen
answers per item, so the stop became one entry in `failed`, listed beside a structure that would
not embed. A species ranking also told the chemist to "remove or correct" a form that a clock had
stopped. The only signal a refused tool call carries is its text, which the capacity marker
(`[calc-at-capacity]`, `core/mcp_session.SERVER_AT_CAPACITY`) already uses.

## Decision

**The stop is named on the wire and here, and it stays a non-retryable refusal.**

- The server opens the message with `[calc-time-budget]` (`engine/budget.TIME_BUDGET_MARKER`,
  raised as `TimeBudgetError(ValueError)`). It is matched at the head of the message by
  `server_marked`, as the capacity marker is, so a caller's quoted argument cannot forge it.
- `core/mcp_session` raises `McpTimeBudget(McpRequestRefused)`, and `connectors/calc/remote`
  maps it to `CalcTimeBudgetError(CalcToolError)`. That class is registered by name in
  `_BAD_DATA_TYPES`.
- A screen records the failed item with `cause="time_budget"` on `FailedMedium` / `FailedBond`;
  every other refusal is `"refused"`.
- `species_ranking` raises `CalcTimeBudgetError` only when **every** form it could not compute was
  stopped by the clock. One refused input makes the whole refusal ordinary, because no amount of
  waiting completes that set.
- The published flag says "was stopped by the calculation service's time budget" and carries the
  cause in its `detail`.

## Options not taken

- **Retry it like `CalcBusyError`.** A full pod ran nothing, so waiting and asking again is free.
  A time-budget stop ran the calculation for the whole budget, up to 900 s, and a retry runs the
  same work against the same clock. On a molecule that is simply too large for the budget, that
  burns `activity_max_attempts` budgets for the same refusal. A retry would succeed only if the
  contention that stopped the run had ended, and nothing here can tell that before paying for it.
- **Fail the whole job again, as before the per-item ADR.** That throws away every item the screen
  did compute in order to report one item the clock stopped.
- **Match the server's prose (`"inline budget"`).** A reword on the server would turn the stop back
  into an ordinary refusal silently. The marker sits in the position nothing else writes, and each
  repository pins its own literal.

Revisit when: a deployment's `chemclaw_mcp_calc_inline_budget_exceeded_total` shows stops that
later succeed on resubmission, which is the evidence that a bounded retry would pay. Or when the
server gains a cheap way to say whether a stop was contention or size (for example, the CPU time
it was given), which would let only the first kind be retried.

## What keeps it true

- **The literal, pinned on both sides.** `servers/calc/tests/test_cost_bounds.py` pins the server's
  literal and class. `tests/test_calc_remote.py::test_a_time_budget_stop_is_named_and_stays_a_refusal`
  pins this side's literal, class and registration;
  `test_a_time_budget_marker_quoted_back_is_not_a_stop` holds the anti-forgery direction.
- **The wire.** `servers/calc/tests/test_server.py::test_a_time_budget_stop_carries_a_marker_the_caller_can_classify`
  drives a real stop over the transport.
- **The cause on each screen.** `tests/test_calc_screen_outcomes.py` holds it for a medium, a bond,
  an all-stopped ranking inside a species screen, and a mixed ranking that stays an ordinary
  refusal.
- **The published flag.** `tests/test_publish_projection.py::test_a_medium_the_clock_stopped_is_published_as_a_stop_not_a_failure_of_the_item`.
