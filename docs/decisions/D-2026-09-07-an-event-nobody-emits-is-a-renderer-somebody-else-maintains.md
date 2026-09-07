# D-2026-09-07-an-event-nobody-emits-is-a-renderer-somebody-else-maintains — `HandoffEvent` is deleted

## Status

Accepted. Carries out on the wire contract what
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` did to the audit column, and
reverses the one thing that ADR deliberately kept.

## Context

`D-2026-08-15` deleted the specialist team. `D-2026-08-26` then deleted the plumbing that survived
it — the specialist contextvar, `record_handoff`, `HandoffSignal`, and `graph_stream`'s
`HandoffSignal → HandoffEvent` conversion — under the rule that *a claim that a control exists is
worse than no control*. It kept exactly one thing, and said why:

> **`api/events.HandoffEvent`.** Dropping a member of the `Event` union is a coordinated change
> across `Chemclaw3_ui` and `Chemclaw3_mock`, and its own docstring already said it was declared and
> unproduced.

Measured on this branch, before the change:

```
$ grep -rn "HandoffEvent" src/ tests/ --include=*.py
src/chemclaw/api/events.py:571     class HandoffEvent(BaseModel)
src/chemclaw/api/events.py:624     union member
tests/test_langgraph_stream.py:195 constructed directly by a test
```

No emitter anywhere in `src/`. The only thing that had ever constructed one outside `events.py` was
a test asserting its wire form.

**The coordination cost that argument avoided was paid anyway, in the other repository — and paid
there first.** `Chemclaw3_ui` mirrors this union by hand, so the member had propagated into
`shared/events.ts` (union member plus a `normalizeEvent` branch), `state/types.ts`,
`state/turnActivity.ts`, `state/chatStore.ts` and a `TracePanel.tsx` renderer printing "Handed to
X" — a complete consumer chain, maintained across every refactor of that tree, for an event that
has never been sent and cannot be. Keeping the member here did not defer the cross-repo change; it
bought a permanent one, and then that repo went ahead without us: measured on its checkout, the
member is gone from every one of those files and `tests/eventContract.test.ts` **pins the absence**,
its comment reading *"Measured against the Chemclaw3 checkout: `grep -rn HandoffEvent src/` finds
the class and its union membership"* and nothing that constructs one. So the state before this
commit was the exact inversion of the argument for keeping it: the producer-less declaration
survived only in the repository that had no consumer for it.

The second half of the argument does not survive contact either: `tests/test_event_contract.py`
established on 2026-08-27 that `Chemclaw3_mock` contains **zero** occurrences of any event name, so
"a coordinated change across two repositories" was a coordinated change across one.

## Decision

**Delete `HandoffEvent`, its union membership and the test that constructed it**, and update the
golden wire fixture in the same commit — which is what `tests/test_event_contract.py` asks of any
deliberate contract change.

**In its place, an absence test, in the form D-2026-08-26 established:**
`tests/test_event_producers.py::test_every_declared_turn_event_has_a_producer` resolves the union at
runtime and requires each member to be constructed somewhere under `src/` outside the declaring
module. It fails by the member's own class name, and it fails whoever adds a member without its
emitter — which is the thing that actually went wrong, twice, while every test stayed green.

A name in a list would have asserted the deletion and nothing more. The rule worth asserting is that
a member arrives with its producer.

## Consequences

- The union has 17 members. The wire form of every other member is byte-identical; a consumer that
  never received a `handoff` event sees no change at all, because there was never one to receive.
- **`Chemclaw3_ui` needs no change**, which is the finding this ADR was expected to hand it and
  did not: its deletion is already merged, with `tests/eventContract.test.ts` asserting that
  `normalizeEvent({type: "handoff", …})` returns `null` and that no layer names the type. The two
  repositories now agree, in the direction where neither drops a live event.
- `src/chemclaw/api/static/app.js`'s dev-page switch loses its `case "handoff":` in the same
  commit, because `tests/test_dev_page_events.py` asserts both directions and
  `test_dev_page_has_no_case_for_a_type_that_does_not_exist` goes red on a case for an event the
  union no longer has. That test is why the dev page could not be left behind — and it is the
  in-repo analogue of the guard `Chemclaw3_ui` had to write by hand.
- When subagents come back with a name to attribute — the contingency the old docstring was reserved
  for — the event is re-added *with its producer*, in one commit, and the new test is what requires
  that. `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` still binds that work.

## What was measured rather than assumed

- The grep above: two declarations and one direct construction in a test, zero producers.
- The producer sweep across the whole union: every other member resolves to at least one
  constructing module (`runner.py`, `graph_stream.py`, `routes/streams.py`, `routes/turns.py`,
  `runner_trace.py`, `runner_answer.py`); `HandoffEvent` resolves to none. That asymmetry is the
  test.
- `tests/test_event_contract.py` regenerated and green; `tests/test_langgraph_stream.py` green.
