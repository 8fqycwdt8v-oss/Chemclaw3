# D-2026-08-11-a-refusal-nobody-can-see-is-not-a-gate — The announcer must wrap everything that refuses, not sit beneath it

**Status:** accepted · **Date:** 2026-08-11 · Found by running M12's plan-gate suite live.

## The defect

`announce_tool_failures` was last in `tool_governance_middleware`'s list. LangChain nests
`wrap_tool_call` middleware in list order — first is outermost — so last means **innermost, closest
to the tool body**, which is what its docstring said and intended.

That is precisely why a governance refusal was never announced. `enforce_plan_approval` raises
*before* calling its handler, and its handler was the announcer. The announcer never ran. The same
holds for `enforce_tool_authz`, `refuse_writes_on_dry_run` and `refuse_repeated_calls`: every
middleware that refuses sat *outside* the thing that announces refusals.

The consequence reached the chemist. A gated call surfaced only as a `ToolResultEvent` whose
`preview` begins `"Refused: …"` — and a surface renders a tool result as a step that **worked**. The
GxP gate held perfectly and nothing downstream could see it hold.

## How it survived

The opposite was written down twice, in confident prose, and believed both times.

`announce_tool_failures`' own docstring: *"Innermost, closest to the tool body, so it sees the raw
exception before either converter turns it into a result."* True of an exception from the tool body;
false of one raised by the chain above it, and the docstring did not distinguish.

`tests/test_m12_probes.py`, asserting the behaviour this ADR restores: *"They arrive on the stream as
the same event type — `announce_tool_failures` is attached innermost, so it sees
`PlanNotApprovedError` raw and announces it exactly as it announces a database outage."* That test
passes, because it feeds hand-written SSE frames containing a `tool_failed` the system never emits.

And `tests/test_plan_gate.py` pinned the broken ordering as an invariant, with the comment *"not
innermost (that is announce_tool_failures)"*.

Three artefacts agreeing with each other and none of them with the code. It took a live run to
settle: the M12 plan-gate suite scored **0 refusals** in a run whose front-door log recorded **two**.

## The decision

Move `announce_tool_failures` to sit **outside every middleware that refuses and inside both
converters**:

```
surface_authorization_denials      # exception → prose, for the model
surface_domain_errors
announce_tool_failures             # ← here: sees every raw refusal, from the chain or the body
audit_tool_calls
enforce_tool_authz
refuse_writes_on_dry_run
refuse_repeated_calls
enforce_plan_approval
```

The invariant is **ordering-as-visibility**: anything that can refuse must nest inside the thing
that announces refusals. It stays inside both converters so it still sees the raw exception rather
than the prose either turns it into — the half of the original reasoning that was correct.

This widens what a chemist is told: an authorization denial, a dry-run refusal and a repeat-guard
refusal now reach the stream as `tool_failed`, where before only a tool *body* raising did. That is
the intended contract rather than a side effect — `surface_authorization_denials` exists to tell the
*model* why a call was refused, and the stream is where the same fact reaches the person. A refusal
the human cannot see is the failure D-138 already recorded once, in the shape of a turn that ended
mid-sentence with nothing saying why.

## Verification

- Offline, before: `['tool_call', 'tool_result', 'plan', 'tool_call', 'tool_result', 'token']` —
  no `tool_failed` for a refused `compute_reaction_energy`.
- Offline, after: `['tool_call', 'tool_result', 'plan', 'tool_call', 'tool_failed', 'tool_result',
  'token']`, the event carrying `PlanNotApprovedError: …`.
- Live M12 plan-gate, before: *an unapproved state-changing call is refused* — **FAIL**, "refused -;
  ran ['compute_reaction_energy'] unrefused". After: **PASS**, "refused
  ['compute_reaction_energy']; ran - unrefused".

`tests/test_plan_gate.py::test_a_refusal_is_announced_because_the_announcer_wraps_the_gate` asserts
the ordering against every refusing middleware by name, so adding a sixth that nests wrongly fails
rather than going quiet.

## The rule this shares with today's other two findings

All three were defects in *reading* rather than in the mechanism, all three had passing tests, and
all three needed a live run to expose. The gate refused correctly; the attribution named a real
node; the mock served a real protocol. What was wrong each time was the thing that had to observe
it — and in each case a test supplied the observation by hand instead of taking it from the system.
