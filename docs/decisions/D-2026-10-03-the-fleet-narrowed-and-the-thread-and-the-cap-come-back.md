# D-2026-10-03-the-fleet-narrowed-and-the-thread-and-the-cap-come-back — `chem` narrowed, so the allowance and the result cap return

**Status:** accepted · **Date:** 2026-10-03 · Supersedes the decision half of
`D-2026-10-02-the-allowance-follows-the-fleet-and-the-result-cap-pays-for-it`, whose own
`Revisit when:` named this case ("Chemclaw3-mcp#152 lands a narrower `chem` … lower the allowance
and give the thread back").

## Context

`SERVED_ELSEWHERE_ALLOWANCE` went 11,000 → 13,200 on 2026-10-02 to follow `chem`'s growth (the
three fleet bundles at 12,020), and `agent_max_tool_result_chars` and `gather_evidence_max_chars`
went 60,000 → 52,000 so the window-pinned budget's warm floor still held. The owner then chose to
narrow `chem` as well. Chemclaw3-mcp#155 (closing #152) cut each `chem` docstring to its rule —
units, what the tool is not, which index to pass, the bound by name, every refusal — and moved
the anecdotes to the tests that hold them. Measured through this repository's own conversion
path: `chem` 7,600 → 4,776 tokens, the three bundles **9,192**. That PR also ratchets `chem`'s
published surface on its own side (`Chemclaw3-mcp:servers/chem/tests/test_prompt_cost.py`), so growth there is
red before it reaches this repository's CI.

## Options

1. **Keep 13,200 and bank the slack.** Leaves ~4,000 tokens of allowance unused while every
   request's thread pays for them. Declined: the allowance is a bound, and a bound 44% over what it
   bounds says nothing.
2. **Lower the allowance and keep the 52,000 cap**, spending the returned thread on margin under
   the warm floor.
3. **Lower the allowance and restore the 60,000 cap.** Chosen. The cap was lowered only to pay for
   the raise; restoring it returns large results and evidence sweeps to their previous size, and
   the warm floor still clears.

## Decision

- `SERVED_ELSEWHERE_ALLOWANCE` 13,200 → **10,250** (11.5% over 9,192, the headroom it was first
  set with). `PREFIX_BOUND` and `agent_context_prefix_basis` fall by 2,950.
- Both thread allowances rise by 2,950 (`CLEAR_TRIGGER_THREAD_ALLOWANCE` 27,900,
  `BUDGET_THREAD_ALLOWANCE` 35,600). Both defaults stay.
- `agent_max_tool_result_chars` and `gather_evidence_max_chars` return to **60,000**. The warm
  thread is then ~16,225 estimated tokens against the 15,000 one maximal batch occupies.
- `D-2026-10-02-the-artefact-prefix-is-paid-from-the-window-margin` is untouched: the 600 of window
  margin it spent stays spent, and whether to reclaim it is that ADR's question.

Revisit when: `test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims` goes red on
the warm floor, or the fleet raises `PUBLISHED_SURFACE_MAX_CHARS` in
`Chemclaw3-mcp:servers/chem/tests/test_prompt_cost.py` — either means the prefix grew into the thread again.

## What keeps it true

- `tests/test_context_floor.py::test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound`,
  run in CI against the fleet's `main`.
- `tests/test_compaction.py::test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims`
  and `test_the_prefix_basis_is_the_bound_both_defaults_are_derived_from`.
