# D-2026-10-02-the-artefact-prefix-is-paid-from-the-window-margin — where the artefact tools' prefix comes from

**Status:** accepted · **Date:** 2026-10-02 · Phase 1 of
`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`. Re-takes, for a prefix this
repository authors, the choice
`D-2026-10-02-the-allowance-follows-the-fleet-and-the-result-cap-pays-for-it` took for the fleet's
and named as reopening "when the warm floor goes red again".

## Context

The artefact ADR's decision 4 says phase 1 raises `CEILINGS["__default__"]` by the delta it
measures and re-derives the budgets `PREFIX_BOUND` feeds — the thread pays, as it has for every
raise in `tests/test_compaction.py`'s history, because `agent_context_token_budget` is pinned from
above by the smallest window this stack targets.

The warm arm of `test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims` requires the
thread a pod calibrated on evidence traffic keeps to exceed one maximal tool batch
(`agent_max_tool_result_chars // 4`), because below it every evidence turn is over budget and
neither edit can reclaim anything. `D-2026-10-02-the-allowance-follows-the-fleet-and-the-result-cap-pays-for-it`
lowered that cap to 52,000 (13,000 tokens) to absorb the fleet's allowance and left the arm
**271** tokens above it: any prefix growth over 271 crosses it, one token per token.

Measured with `tests/test_context_floor.py`'s own counter on that main:

| artefact surface | prefix delta | warm arm (floor 13,000) |
|---|---:|---:|
| docstrings as first written (from the ADR's drafts) | 903 | ~12,368 |
| trimmed to what a spec is; `edits` validated behind the boundary; shorter skill entry | **566** | 12,705 |
| setting off (`agent_exhibits_enabled=false`) | 0 | 13,271 |

The shipped surface is `create_exhibit` 195, `revise_exhibit` 228, `read_exhibit` 102 and the
`exhibits` skill's listing entry 41. Below it a tool no longer says what its argument is, which
costs a failed first call on every artefact rather than tokens.

## Options

1. **Lower `agent_max_tool_result_chars` again**, to ~50,000, with `gather_evidence_max_chars`.
   This is the instrument the owner chose hours ago for the fleet's breach, and it would work
   (floor 12,500, arm 12,705). It is declined here for what a second use makes of it: the cap
   becomes the ratchet every prefix raise pays from, so evidence shrinks on every deployment —
   artefacts on or off — for each feature that adds a schema, and the owner's own trigger for the
   next raise would already have fired on this one.
2. **Raise `agent_context_token_budget` by exactly the ceiling's raise**, so the thread allowance
   is held where the fleet decision left it and the cost lands in the margin under the 128k window
   (`test_a_maximal_request_at_the_shipped_budget_fits_the_smallest_window_it_targets`, asserted
   exactly). Declined twice today for the *fleet's* growth, on the argument that the margin is
   headroom the provider decides.
3. **Ship artefacts off by default.** Reverses the artefact ADR's decision 4, and a feature that
   ships off is one nobody has (`D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has`).

## Decision

**Option 2, by 600**: `agent_context_token_budget` 118,700 → **119,300**, the margin under the
window 5,204 → **4,604**, and the warm arm reads **13,202** (202 above the floor). The ceiling rises
by the measured 566 plus rounding, 72,850 → **73,450**; `PREFIX_BOUND` and
`agent_context_prefix_basis` follow to **86,650**. The clear trigger is held at 111,600, so its
thread allowance falls 600 to 24,950; the budget's thread allowance is held at 32,650.

Why the margin and not the cap, when the owner chose the cap for the fleet: the cap is a cost every
evidence turn pays whether or not the feature that grew the prefix is on, and a second cut in one
day to pay for 566 tokens would make that the default instrument. The margin is spent once, by an
amount equal to the prefix it pays for, and 4,604 on a 123,904-token input ceiling is still more
headroom than `D-2026-09-04` left. If the owner prefers the cap here too, the swap is mechanical:
`agent_max_tool_result_chars` and `gather_evidence_max_chars` to 50,000 and the budget back to
118,700.

## Consequences

- The budget's thread is held and the clear trigger's thread pays 600; the margin under the window
  is 600 smaller. `tests/test_compaction.py` states both where they are asserted.
- The warm arm is 202 tokens above its floor. The next prefix raise of that size reopens this
  choice with less margin to spend; the instruments that buy room back are unchanged — a narrower
  prefix through deferred schemas (`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`) or
  profile routing, or a narrower `chem` in the fleet (Chemclaw3-mcp#152).
- A deployment that switches artefacts off pays none of the 566.

**Revisit when:** `tests/test_compaction.py`'s warm arm reads under 13,100 again (the next prefix
raise), Chemclaw3-mcp#152 lands a narrower `chem` (give the margin back before the thread), or a
deployment declares `CHEMCLAW_LLM_CONTEXT_WINDOW_TOKENS` below 128,000.

## What keeps it true

- `tests/test_compaction.py::test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims`
  and `test_a_maximal_request_at_the_shipped_budget_fits_the_smallest_window_it_targets`.
- `tests/test_context_floor.py::test_the_static_prefix_stays_under_its_ceiling`.
