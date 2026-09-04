# D-2026-09-04-a-budget-that-excludes-the-prefix-is-not-a-budget — the prefix is charged whether or not a window is declared

**Status:** accepted · **Date:** 2026-09-04 · Revisits
`D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget`, whose `if window:` guard was correct
arithmetic against a setting no deployment sets. That ADR is not edited and its unit decision —
budgets are *billed*-token budgets — stands; what changes is which tokens are counted.

## Context

`context_budget.effective_trigger` subtracted the request's measured prefix from the configured
budget only `if window:`. `llm_context_window_tokens` defaults to 0, `.env.example` ships 0, and no
deployment declares one, so in every shipped configuration the prefix — the system message, the
skills listing and every bound tool schema — was budgeted against nothing.

Measured end to end on a compiled graph over the real 61-tool surface, one thread driven twice:

```
undeclared (shipped)   thread cut to 90,030   request 137,301   does NOT fit a 128k model   counter 0
window = 128,000       thread cut to 75,025   request 122,296   fits                        counter 0
```

The prefix measures **43,175** estimated tokens. A budget that excludes 43% of what it is budgeting
is not a budget.

`D-2026-09-04`'s earlier pass proposed the row's third candidate fix — a window-aware arm on
`_record_overrun` — and measuring it is what killed it: where a window *is* declared the budget has
already been cut to fit, so that arm is a control that cannot fire. The indicator is downstream of
the arithmetic, and could not substitute for it.

## Decision

**`effective_trigger` subtracts `prefix_tokens()` unconditionally.** `agent_context_token_budget`
and `agent_tool_result_clear_trigger` stop being bounds on *thread* spend and become bounds on
**request** spend.

### What it costs, stated rather than discovered

Every deployment's thread allowance falls by the prefix:

| configured | before → after (thread allowance, estimated tokens) |
| --- | --- |
| `agent_context_token_budget` 100,000, ratio 1.0 | 100,000 → **56,825** (−43.2%) |
| same, ratio 2.2 (calibrated) | 45,454 → 25,829 |
| same, ratio 4.0 (clamp) | 25,000 → 14,206 |

A session that never compacted may now compact. That is the change working, not a regression — the
tokens were always being spent.

### The finding that outranks the decision

`agent_tool_result_clear_trigger` shipped at **30,000**, which is *below* the 43,175-token prefix.
Charged unconditionally it floored at 1 — "clear every reclaimable tool result on **every** model
call, keeping only the newest batch", so the agent loses sight of evidence more than one step back.

That was never a tuning anybody chose; it is what a thread number reads as once the unit changes
underneath it. **The default is raised to 73,500**, which is the old 30,000 re-expressed in the new
unit rather than a retuning: `tests/test_context_floor.py`'s ratchet **ceiling** (43,500) plus the
30,000 of thread the setting has always intended. The ceiling rather than today's measurement,
deliberately, so the number does not move every time a tool schema does.

`tests/test_compaction.py::test_the_shipped_clear_trigger_clears_the_prefix_it_is_charged` asserts
the clearance against that ceiling. The day a tool surface is allowed to grow past what this
setting can absorb, that test fails and names the trade rather than the behaviour changing quietly.
It replaces a test that asserted the *opposite* state, written in the same session to make the
floor undeniable while it stood.

### The floor stays at 1, and it is loud

A deployment can still misconfigure below its own prefix. `_note_floored_trigger` reports it once
per process per distinct `(configured, prefix, window)` at WARNING (`context.trigger_floored`),
naming both numbers and the remedy. Raising instead was rejected: this runs inside a middleware and
`compaction.py`'s own `GuardedEdit` argument is that a raising edit costs the turn, which is the
worst trade available. A counter was declined because the condition is *static* — fixed by two
settings and the bound tool surface — so a rate carries nothing the one line does not, and this
repository makes every declared series earn a panel or an alert.

## Consequences

**The overrun indicator is repaired, measured.** `chemclaw_context_unreducible_total` moved **0 → 1**
on a newest conversation group the window edit cannot cut past, at shipped settings with no window
declared. Both the "it cannot fire" prose from the earlier pass and its correction are superseded: a
tick now means "the policy could not bring the whole request inside `agent_context_token_budget`" in
every configuration. Whether that budget is also the provider's real limit is still what
`llm_context_window_tokens` decides — declared, it is the leading indicator of a context-length
failure; undeclared at 100,000 against a 128k model it fires *before* the provider would, which is a
conservative early warning rather than a false negative.

**The soundness invariant got stronger.** `tests/test_context_budget.py` sweeps 1,728 combinations
of `(window, prefix, reservation, budget, ratio)` × 5 thread sizes and now pins, with no window
needed, that `sent ≤ effective_trigger(budget)` implies `prefix + sent × ratio ≤ budget` — and,
where a window is declared, D-2026-08-28's property as well. The degenerate corner is asserted to be
*reached* with no window declared (108 of 288 undeclared points), because a plain misconfiguration
now opens it where previously only a window narrower than its own prefix could.

**No bad interaction with the calibration warm-up.** The prefix comes off before the ratio division,
so the largest and most stable part of the request is charged exactly from model call 1, where it
was previously charged at 0 forever. Whether a trigger floors is `configured − prefix < ratio`, so a
pod restart can flip it only inside a band as wide as the ratio — swept at prefix 43,175, only
43,176–43,179 flip. The warm-up's over-spend also shrinks: the uncalibrated-to-calibrated swing was
100,000 → 45,454 and is now 56,825 → 25,829.

**Three existing tests failed and were right to.** Each set a budget measured off its own small
fixture, which became a negative budget; fixed by a `_request_budget(n) = prefix + n` helper, so the
meaning change is written *into* the tests rather than around them. A fourth
(`test_a_fan_out_never_loses_its_own_results`) read its trigger off the shipped setting and went
**vacuous** rather than red when that setting rose above its fixture — caught only by its own
"this test proves nothing" guard. Its trigger is now derived from the fixture, because the property
is the edit's and must hold at every trigger.

**Closes** `BACKLOG.md`'s "No deployment declares a context window, and the overrun indicator cannot
see the prefix" by taking its second option. Its first — declaring `llm_context_window_tokens` in
`deploy/` — is now an independent and still-worthwhile improvement rather than the whole control,
and the row read the other way.
