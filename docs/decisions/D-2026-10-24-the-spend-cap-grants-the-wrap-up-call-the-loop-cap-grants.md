# D-2026-10-24-the-spend-cap-grants-the-wrap-up-call-the-loop-cap-grants — a turn over its billed-token budget answers once, tool-less, then ends

**Status:** accepted · **Date:** 2026-10-24 · **Builds on**
`D-2026-09-18-a-cap-below-an-ordinary-turn-is-a-guard-that-kills-another`, whose derivation of the
default (loop cap times context budget) it replaces as the defence; the number is unchanged.

## Context

`enforce_spend_cap` ended a turn at the first call that would start over budget (`jump_to: end`).
The turn then had no final answer, and the runner could only say "nothing had been written".
Measured live on `claude-sonnet-5-5`, 2026-10-24: 22 of 91 deep-research turns ended that way. On a
lane binding all 14 connectors (fixed prefix about 107k tokens per call), probe rx-01 hit the cap
at 3,330,590 tokens against a cap of 3,300,000 after fewer than 25 model calls, and the chemist got
nothing after 55 tool calls. The per-call size depends on how many connectors a deployment
enables, and the loop cap's own wrap-up call is not in the 3,000,000 derivation, so any number set
now is wrong for the next deployment.

## Options

1. **Raise the number again.** Cheap, and the same defect one deployment later: a larger prefix or
   one more call moves the turn back over it. A higher cap also loosens the backstop it exists to be.
2. **Derive the cap from the measured prefix.** Right for one lane, and a derivation the code
   cannot check for the deployment it runs in: connectors, profiles and the gateway model change
   the per-call size without touching the setting. The loop cap's wrap-up call still falls outside it.
3. **The spend cap grants the one tool-less wrap-up call the loop cap grants.** The first arrival
   over budget sets `spend_capped` and `loop_wrap_up`; `AnswerAtTheCap` makes that call
   `tool_choice="none"` with the existing step-cap note and drops any tool call in the reply, so the
   graph ends on its own. A second arrival ends the turn.

## Decision

Option 3. No number can make ending on the budget safe, but the shape of the stop can: the bound
becomes the budget plus one call, which is what the loop cap already costs. Where the two hooks meet
(`enforce_loop_cap`, then `enforce_spend_cap`, then `AnswerAtTheCap`): a `loop_wrap_up` the spend
hook finds without `spend_capped` is the loop cap's pending wrap-up and is left alone (the loop cap
ends the turn on its second arrival); with `spend_capped` it is the spend cap's own, spent, and the
turn ends. Helpers keep their own private `loop_wrap_up` and share `billed_tokens`, so each graph
over budget gets one call.

## Consequences

- An over-budget turn makes exactly one more call than before, billed to the turn; the overshoot
  bound is one call plus the wrap-up. The default 3,000,000 does not change.
- Both caps send the existing `WRAP_UP_NOTE` verbatim: a new model-facing sentence would need the
  live evaluation `D-2026-10-08` requires, disproportionate for a rare path. The model is told
  "step limit" while the chemist is told the true reason by `spend_cap_reached`; an accurate note is
  a BACKLOG row.
- `spend_cap_reached` still fires and the answer is marked partial; "nothing had been written"
  now means the wrap-up itself came back empty.
- `tests/test_spend_cap.py` and `tests/test_spend_wrap_up.py` hold the cases.
- Supersedes the unmerged proposal to raise the default to 3,300,000 (PR #674).

Revisit when: a turn marked `spend_capped` again ends with no answer text in `turn_costs`.
