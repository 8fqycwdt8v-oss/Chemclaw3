# D-2026-09-14-a-lowering-that-loses-a-merge-is-a-raising — the dropped 300, and the control that was not one

**Status**: accepted. Restores the ceiling `D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`
decided, and supersedes the second bullet of that ADR's `## What keeps it true`.

## Context

Two independent changes to `tests/test_context_floor.py`'s ratchet landed in one wave:

- `D-2026-09-13-the-default-is-the-posture-every-deployment-already-runs` **raised** the ceiling
  65,500 → 67,500, measured at +1,862 tokens on every profile but `computation`, because
  `harness_enabled` became the default and `write_todos` entered every prefix.
- `D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not` **lowered** it by 300, having moved
  309 tokens of developer rationale out of three tool descriptions.

They are independent and both belong, so the shipped ceiling is their sum: **67,200**. The merge
that resolved the conflict between them took the first and dropped the second. `git diff` over the
merge commit's `tests/test_context_floor.py` is empty, the ceiling reads 67,500, and three
documents — the lowering ADR, its ledger row, and `BACKLOG.md` — assert `65,500 → 65,200` shipped
in that commit. **A lowering that loses a merge is a raising**, and the three documents make it
invisible.

## Decision

The ceiling is 67,200, and everything derived from it moves with it:

| constant | where | was | now |
| --- | --- | ---: | ---: |
| `CEILINGS["__default__"]` | `tests/test_context_floor.py` | 67,500 | **67,200** |
| `PREFIX_BOUND` | same file, `= ceiling + SERVED_ELSEWHERE_ALLOWANCE` | 78,500 | **78,200** (derived) |
| `agent_tool_result_clear_trigger` | `core/config/agent.py`, `= PREFIX_BOUND + 30,000` | 108,500 | **108,200** |
| `agent_context_token_budget` | `core/config/agent.py` | 119,000 | **118,700** |

`.env.example` carries the two settings and moves with them. The budget's second bound is checked
rather than assumed: `128,000 − llm_max_tokens = 123,904` of input, and the margin under it widens
4,904 → **5,204**, which `tests/test_compaction.py` asserts as an equality so that a *narrowing*
has to be stated rather than discovered. `tests/test_compaction.py`'s two allowance assertions are
`>=` and stay green either way, which is exactly why the derivation had to be re-applied
deliberately rather than left to the gate.

## The finding that outranks the restoration

`D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`'s `## What keeps it true` says: *"the
309 tokens cannot come back without failing the lowered ceiling"*. **They can, and they always
could, at every value this ceiling has ever held.**

A ratchet ceiling is deliberately a *bound* rather than today's measurement — it is set above the
prefix so that an unrelated tool-schema merge does not redden the gate, which
`D-2026-09-04-a-budget-that-excludes-the-prefix-is-not-a-budget` states as the reason it uses the
ceiling and not the measurement. The headroom has been 593, 731, 602 and is now 740. **Every one of
those exceeds 309.** So no 309-token restoration has ever been catchable by it, and the sentence was
false in the commit that wrote it.

Driven here, both arms:

- The rationale put back into `get_durable_job_status`'s model-facing docstring measures **+212
  tokens** (66,460 → 66,672) and `test_the_static_prefix_stays_under_its_ceiling` **passes** — at
  67,500 *and* at 67,200. The lowering does not change that verdict, and claiming it would is the
  same shape of error one level down.
- The same edit turns
  `tests/test_prose_contract.py::test_no_tool_description_tells_the_model_about_a_tier_that_is_gone`
  **red**, on its own.

So the first bullet of that ADR's `## What keeps it true` is the control and the second was a
description of an intention. **What the ceiling holds is aggregate drift, not a paragraph**; what
holds a paragraph is a test that reads the paragraph. Both remain worth having and they are not
substitutes — the ceiling caught nothing here, and it is what makes 113 tools' worth of slow growth
somebody's decision rather than a merge's side effect.

## Consequences

- A per-paragraph prose regression is caught by `tests/test_prose_contract.py` or by nothing. An
  ADR citing the ratchet for one is citing the wrong control.
- The thread allowance is unchanged: the trigger is derived upwards from the bound and moves with
  it, and the budget's fall of 300 is a fall in what a *request* may be, against a window that did
  not move.

## What keeps it true

- `tests/test_context_floor.py::test_the_static_prefix_stays_under_its_ceiling` — the ceiling, at
  its restored value, over the prefix observed off the wire with connectors bound.
- `tests/test_compaction.py::test_the_shipped_clear_trigger_clears_the_prefix_it_is_charged`,
  `::test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims` and
  `::test_a_maximal_request_at_the_shipped_budget_fits_the_smallest_window_it_targets` — the two
  derivations, from below and from above, the last as an equality on the margin.
- `tests/test_prose_contract.py::test_no_tool_description_tells_the_model_about_a_tier_that_is_gone`
  — the control the superseded bullet should have named, driven red by the restored paragraph.
