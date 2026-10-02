# D-2026-10-02-the-allowance-follows-the-fleet-and-the-result-cap-pays-for-it — the breach decision taken by the owner

**Status:** accepted · **Date:** 2026-10-02 · Supersedes the breach half of
`D-2026-10-02-ci-runs-the-fleets-servers-and-a-breached-allowance-is-held-not-raised` (its CI half
stands: the `check` job still builds the fleet's environment and a sibling skip still fails).

## Context

`SERVED_ELSEWHERE_ALLOWANCE` bounds the three bundles this repository declares and
`Chemclaw3-mcp` serves. CI measured them at **12,020** against 11,000 (`chem` 7,604 / 13 is all
of the growth). The superseded ADR held the breach in a tolerance constant,
`SERVED_ELSEWHERE_KNOWN_BREACH`, filed Chemclaw3-mcp#152 to narrow `chem`, and named the decision
left if the fleet would not narrow it: *"the choice is between the budget's warm floor
(`_unreclaimable_batch_tokens`, i.e. `agent_max_tool_result_chars`) and the window margin, and that
needs a decision of its own."* This is that decision. The owner took it.

The arithmetic both ADRs rest on: `agent_context_token_budget` is pinned by
`tests/test_compaction.SMALLEST_TARGET_WINDOW`, so each token of allowance is a token of thread.
The warm arm of `test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims` needs the
calibrated thread above one maximal tool batch, which is `agent_max_tool_result_chars // 4`.

## Options

1. **Narrow `chem` in the fleet** (the superseded choice). Declined by the owner: those
   descriptions are the units and the "what this tool is not" sentences the fleet's own rules
   require of every tool, and they are what the model reads before calling it.
2. **Raise the budget** with the allowance. Declined, as in the superseded ADR and at
   `test_a_maximal_request_at_the_shipped_budget_fits_the_smallest_window_it_targets`: the margin
   under a 128k window is headroom the provider decides.
3. **Raise the allowance and lower the result cap so the warm floor still holds.** Chosen.

## Decision

- `SERVED_ELSEWHERE_ALLOWANCE` 11,000 → **13,200** (9.8% over 12,020). `PREFIX_BOUND` and
  `agent_context_prefix_basis` move with it, 83,850 → 86,050. A chart deployment's prefix is back
  inside the basis, so it no longer pays the excess in spend or logs `context.prefix_over_basis`.
- Both thread allowances fall by 2,200 (`CLEAR_TRIGGER_THREAD_ALLOWANCE` 25,550,
  `BUDGET_THREAD_ALLOWANCE` 32,650). Both defaults stay.
- The warm thread falls to 13,275 estimated tokens, under the 15,000 a 60,000-character batch
  occupies. `agent_max_tool_result_chars` 60,000 → **52,000** (13,000 tokens), and
  `gather_evidence_max_chars` with it: the two are one number on purpose, because a sweep over the
  tool-result cap is cut head-and-tail through the middle of a cross-source ranking.
- `SERVED_ELSEWHERE_KNOWN_BREACH` and its second assertion are deleted; the allowance is the bound
  again.

## Consequences

- A single tool result, or a parallel batch, larger than 52,000 characters is cut about 13%
  sooner. The full text is still kept for the chemist
  (`D-2026-09-27-a-cut-result-is-kept-for-the-chemist-not-the-model`), and the ten-hypothesis
  report, the largest first-party payload measured against this cap, renders at about 44,800.
- The warm floor now clears by about 275 tokens. The next prefix growth on either side, a
  ratchet raise here or a schema grown in the fleet, reds that test and reopens this same choice.
- Chemclaw3-mcp#152 is no longer required by this repository. Narrowing `chem` there would still
  buy thread back here.

Revisit when: `test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims` goes red on
the warm floor again, or Chemclaw3-mcp#152 lands a narrower `chem`. In the second case, lower the
allowance and give the thread back.

## What keeps it true

- `tests/test_context_floor.py::test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound`,
  run in CI against the fleet's `main`.
- `tests/test_compaction.py::test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims`
  and `test_the_prefix_basis_is_the_bound_both_defaults_are_derived_from`.
