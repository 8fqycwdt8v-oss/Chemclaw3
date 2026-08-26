# D-2026-08-26-a-guard-that-runs-at-collection-guards-nothing — a control that runs at the wrong moment inverts

**Status**: accepted · **Date**: 2026-08-26

## Context

A review of `#215` — the field-benchmark implementation, already merged — produced eight findings
against work this repository had just shipped green. Six were real defects in that change, and the
two most severe shared a shape worth naming: **a control that runs at the wrong moment does not
weaken, it inverts.**

### The collection abort

`tests/test_prompt_caching.py` guarded its two live tests with
`@pytest.mark.skipif(_live_credential() is None, ...)`. `_live_credential()` performs an
(unbilled) `count_tokens` round trip, and it catches only `anthropic.AuthenticationError`, on the
deliberate reasoning that a 429 or a 529 is the provider being busy rather than the operator being
unconfigured — "they propagate, and the test fails as it should".

`skipif` evaluates at **collection**. An exception there is not a failing test; it is
`Interrupted: N errors during collection`, which abandons the session. Measured with the provider
pointed at a closed port:

| | tests collected | tests run |
|---|---|---|
| `pytest test_prompt_caching.py test_context_floor.py` (before) | 0 | **0** |
| the same, after | 21 | **19 passed, 2 errors** |

So the sentence in the docstring was exactly backwards. The intent was "this one test fails"; the
behaviour was "no test in the session runs, including the static-prefix ratchet in the other file".
A credential probe written to make the suite honest could silently take the whole suite off the
air.

The fix is a fixture, because a fixture runs at call time. What is asserted now is the *absence* —
`test_no_module_level_call_dials_the_provider_at_collection` parses this module's own AST and fails
if `_live_credential()` appears outside a function body — because there are several ways back in (a
`skipif`, a module constant, a decorator argument) and only one property that matters.

### The default that became a floor

`agent_tool_result_clear_trigger` was added with a default of 30,000 and a cross-field validator
refusing a value above `agent_context_token_budget`. Both are right on their own. Together they
made 30,000 a hard **minimum** for the budget: a small-context deployment setting only
`CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET=20000` could not construct `Settings()` at all, and the error
named a variable it had never heard of.

A default is this repository's opinion; a budget is the deployment's. So the default is now clamped
to the budget and the refusal is kept for the case it was written for — an operator who *explicitly*
sets the trigger above the budget, where the setting silently means "unchanged". `model_fields_set`
is what tells the two apart.

## Decision

Fix the six defects in the change under review, and record the two that are decisions rather than
edits as backlog rows rather than fixing them badly.

Beyond the two above:

1. **`forget_calls()` becomes precise.** The repeat guard is reset when compaction clears a tool
   result, because a cleared answer makes the next identical call a re-read. The reset was global,
   so it also forgave repeats of the newest `agent_keep_last_tool_groups` results — which clearing
   *preserves* — once per reduction. That made the guard's strength a function of a token
   threshold. `agent/compaction.py::_cleared_calls` now reads upstream's own
   `response_metadata["context_editing"]["cleared"]` stamp and names the calls that actually lost
   their answers. Neither module had a test for this coupling; both do now.

2. **The grandfathered set is a closed baseline.** `len(GRANDFATHERED) <= 18` does not say "only
   shrinks" — drain one entry and a merge may add a fresh unprobed tool with the gate green. The
   second attempt, a frozen copy `frozenset(GRANDFATHERED)`, was worse: derived from the same
   literal, it can never differ, and a test built on it **passed against a deliberately planted
   addition**. Only a baseline the working set cannot influence expresses the invariant, so the
   literal is now dated and never edited and the live debt is *computed* as it minus whatever has
   since been probed.

3. **Two upstream couplings are registered.** `tests/test_context_floor.py` calls deepagents'
   private `_format_skills_list` and invokes `before_agent` with three arguments — the latter being
   the exact arity dependency `D-2026-08-14` removed from production code. Both now live in
   `tests/test_upstream_surface.py`, which is where this repository keeps every shape upstream never
   promised.

4. **`turn_cost_ratio`'s claim is corrected.** Its case carries literal turn records, so the metric
   returns a constant of committed data — the 32% static-prefix growth its sibling ratchet caught
   would leave the `baseline.json` row untouched. The metric has the property its docstring claims;
   this case cannot exercise it. Both now say so, and wiring real `TurnCost` rows is a backlog row
   blocked on a deployment that has turns in it.

5. **`infra/live/processes.sh` persists its environment after the fleet starts.** It was written 20
   lines before `start_fleet_bundles` minted `CHEMCLAW_CHEM_TOKEN`/`CHEMCLAW_SAFETY_TOKEN` and
   rewrote `CHEMCLAW_CONNECTOR_URLS`, so `processes.sh env` handed a second shell no credential for
   the two bundles that change introduced — the precise failure the comment directly above the
   write warns about, in its own words: "401s from a connector that is plainly up".

Deferred to `BACKLOG.md`: the `turn_cost_ratio` case rewiring (blocked), and the run-directory
collision between `processes.sh` and `e2e-full-stack/up.sh` (a decision about which lane owns the
fleet, not an edit).

## Consequences

The general rule, which is the reason this is an ADR and not six commits: **a check's moment is part
of its contract.** `skipif` runs at collection, a pydantic default is resolved before any validator
sees the deployment's intent, a count is evaluated against one commit while "only shrinks" is a
claim about two. In each case the code was locally correct and the guarantee was not the one the
prose beside it described — and in each case a test that ran the thing, rather than reading it,
is what showed the difference.

That is `CLAUDE.md`'s "measure it, don't argue it" pointed at this repository's own controls. All
six defects were in a change that shipped with a green `make lint type test`, CI green on three
jobs, and a full local suite of 4,533 passing tests. Green is evidence that the assertions hold; it
is not evidence that the assertions mean what they say.

## Alternatives considered

**Catch `APIConnectionError` in `_live_credential` too.** It fixes the reproduction and not the
class: any other exception from the SDK — a timeout, a proxy 407, a future error type — still
aborts collection. Moving the call out of collection removes the whole category.

**Assert the grandfathered membership exactly.** It works, and it means every drained entry needs a
second edit in the test. Computing the live set from a closed baseline gives the same guarantee with
no upkeep, and makes the "only shrinks" property structural rather than asserted.

**Leave `forget_calls` global and document the imprecision.** It was already documented on both
sides and tested on neither, which is how it survived. `D-2026-08-11`'s finding applies directly:
prose about a mechanism is evidence about its author's belief.
