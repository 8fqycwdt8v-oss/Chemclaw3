# D-2026-08-28-the-budget-is-the-control-not-the-trigger — the conversation window cuts to the token budget, and the group floor ships off

**Status:** accepted · **Date:** 2026-08-28 · Refines
`D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has`, which restored this policy; nothing
in that ADR is reversed.

## Context

`agent/compaction.py` composes two context edits inside `wrap_model_call`: upstream's
`ClearToolUsesEdit` (lossless, `agent_tool_result_clear_trigger` = 30,000, keeping the newest
`agent_keep_last_tool_groups` = 2 results verbatim), then the first-party
`KeepLastConversationGroupsEdit` (destructive, `agent_context_token_budget` = 100,000, keeping the
newest `agent_keep_last_conversation_groups` = 12 groups).

The window computes `cut = min(max(by_tokens, by_groups), starts[-1])`. `max` takes the **larger
cut**, so what survives is the **smaller** of "what fits the budget" and "the newest `keep` groups".
That `max` is deliberate and load-bearing: the count-only version it replaced left a 300,300-token
thread at 180,180 against a 100,000 budget, and the arm is what makes the edit *bound* rather than
merely reduce.

What nobody had measured is what the composition does when both arms are live.

## What was measured

The crossover is `budget / keep` — **8,333 tokens per group** at the shipped defaults, roughly 33 kB
of prose in a single turn. Below it the group arm wins outright. Over 2,000 prose groups at the
shipped defaults:

```
in 329,900 tokens  ->  1,944 tokens, 12 groups   (budget 100,000)
```

Sweeping the budget from 10,000 to 300,000 changed that number **not at all**. A 4× budget bought
zero additional context. The lossless edit ordered before this one makes the common case *more*
extreme rather than less, because a cleared tool result costs about twenty tokens — so in the
composed pipeline the group arm is the binding constraint essentially always.

Three sentences in the tree stated the opposite, and one was wrong by 50×:

- `KeepLastConversationGroupsEdit`: *"Cut the oldest conversation back to the token budget"*, and
  *"The budget is now `trim_messages` … so what survives is what fits."*
- `trigger`: *"the budget it cuts back to"*.
- `core/config/agent.py`: *"raising N no longer raises what a request can cost, it only drops
  more."* Measured at a fixed 100,000 budget: N=12 retains 1,944 tokens, N=600 retains 97,800.

The mechanism was stated correctly one paragraph below the headline that contradicted it.

## Re-verified after `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget`

That ADR landed on `main` while this one was being written, and it changes the *unit* the trigger is
in: the configured budget is now a **billed**-token budget, converted by a measured ratio and
clamped by a declared context window (`effective_trigger`, `agent/context_budget.py`). It does not
touch this arm — `cut = min(max(by_tokens, by_groups), starts[-1])` and `default=12` are unchanged
by it — and re-measuring through the merged code confirms the finding survives, on the same shape:

```
281,900 tokens, 2,000 groups, budget 100,000
  keep=12 (the old default) ->  1,692 tokens, 12 groups   (0.6% of the budget)
  keep=0  (shipped here)    -> 99,969 tokens, 709 groups
```

The two changes compose in the same direction rather than overlapping: that one makes the budget
*mean* what it says, and this one makes it *decide*. A budget converted carefully into the right
unit and then overridden by a count of turns is the same inert knob with better arithmetic behind
it.

## Decision

**`agent_keep_last_conversation_groups` becomes `ge=0` and ships at `0`.** The edit already reads 0
as "no floor"; nothing in `apply` changes. The token budget is then the whole rule, which is what
its name has always claimed.

The knob is kept, not deleted, and it keeps a real meaning: **an extra cut a deployment may ask
for** — "let the model see fewer *turns* than the budget would allow". The instrument for wanting
it to see fewer *tokens* is the budget. Deleting the field instead was measured and rejected:
`Settings` is `extra="forbid"`, the README quickstart is `cp .env.example .env`, and a stale
`CHEMCLAW_*` key in a `.env` file is a hard startup refusal — so every deployment carrying the row
would fail to boot.

The four false sentences are corrected rather than softened. That is not cosmetic: *"raising N no
longer raises what a request can cost"* is the sentence that would stop the next reader from
applying this fix.

## What was considered and rejected

**Raise the default to a large number (1,000).** Behaviourally identical below 1,000 groups, and
verified so. Rejected because the number is chosen by arithmetic rather than by principle, and it
leaves the coupling in place: a deployment raising the budget past ~10× the group cost still gets
nothing until it also raises N.

**Make `keep` a floor on what is *kept* rather than on the cut.** Not a distinct design. The two
constraints are simultaneously satisfiable only when the token cut already satisfies both, so the
least-aggressive feasible cut is `by_tokens` **always** — verified over 300 randomized threads, 214
firing cases, 0 disagreements with "delete the arm", and in 163 of the 214 the group floor was
infeasible against the budget at all. Its naive spelling, `min(by_tokens, by_groups)`, reintroduces
the exact defect the `max` exists to prevent: 180,180 tokens against a 100,000 budget,
byte-identical to the number the original finding recorded.

## Consequences

- A long conversation now sends the model up to the budget instead of twelve turns, so a turn on a
  long thread costs more. That is the stated bound being spent rather than a new cost: deployments
  have been paying 2% of a budget they configured. A deployment that wants less lowers the budget.
- The regression the `max` prevents stays prevented. Measured with the floor off: 20 groups of
  60 kB cut to **90,366** tokens against a 100,000 budget, not 180,180.
- `tests/test_compaction.py` gains two assertions — that a larger budget keeps strictly more
  context, and that an explicitly-set floor still binds. The first is the property the shipped
  defaults did not have; the second is what makes "we turned it off" different from "we removed
  it".
