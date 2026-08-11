# D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs — A handoff is observable where the specialist runs, not where it was dispatched

**Status:** accepted · **Date:** 2026-08-11 · Closes an M9 open row; supersedes nothing.

## The defect

`HandoffEvent` shipped in M8's event-contract change as a member of the `Event` union that **no
code path produced**. The declaration was complete in every other respect — the union carried it,
`api/static/app.js` already switched on `"handoff"`, `graph_stream` already attributed events by
subgraph namespace, `tests/test_langgraph_stream.py` already pinned its wire form — and nothing
raised it.

It is additive and defaulted, so no consumer broke. That is exactly what made it worth fixing
before the things it is entangled with: a surface programming against the contract waits forever
for an event that never comes, and a contract with an unreachable member teaches its readers to
distrust the rest of it. It is the only item on the migration's open list where the shipped code
makes a promise it does not keep.

Two facts about how it went unnoticed are worth recording, because they generalise. The event's own
docstring described when it is emitted, in the present tense, and was evidence only about what its
author intended (the repo's standing "prose is evidence about its author, never about the code"
rule). And `tests/test_m12_probes.py` had *already written the finding down* — a routing suite
scores specialists off the `agent` attribution rather than off handoffs, with a docstring saying
"`HandoffEvent` exists and nothing raises it yet" — so the gap was known, recorded in a test, and
still shipped.

## The question

Not "should a handoff be emitted" but **where is a handoff observable from**, which looked
entangled with the still-open routing choice: `SubAgentMiddleware`'s `task` tool (what is built) or
a routing node issuing `Command(goto=…, graph=Command.PARENT)` (what
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` prefers, precisely *for trace
legibility*). Picking between those is an M12 measurement against a live stack, so the handoff
looked blocked behind a credential.

## The decision

**Emit the handoff from `agent/team.running_specialist`** — the contextmanager that wraps a
specialist's invocation — as a pair: `to=<name>` on enter, `to=""` on the way back out, carried on
the turn's stream as a `HandoffSignal` beside every other out-of-band signal.

Three things follow from that placement, and each is why it is the placement:

1. **It un-entangles the fix from the routing choice.** The two candidate mechanisms differ in how
   a specialist is *reached*; they agree that the compiled specialist is then *invoked*, through
   `_AttributedSpecialist`. Observing the invocation rather than the dispatch means the routing
   measurement can settle either way without changing the event contract. Reading the `task` tool's
   `subagent_type` argument instead — the obvious alternative — would have bound the contract to
   the mechanism and made the routing decision a breaking change.

2. **The trace's span and the audit trail's span become the same `try`/`finally`.** Invariant 3 of
   the subagent ADR already made this block the interval attributed to a specialist in the audit
   trail; the handoff is that same fact told to the person watching. Emitting it anywhere else
   would create two brackets that can disagree about when a specialist was running, and the
   disagreement would be silent — the failure mode `_AttributedSpecialist.with_config` already
   exists to prevent one layer down.

3. **The hand back is free and correct.** `to=""` fires in the `finally`, so a specialist that
   raises still closes its span. A handoff that never closes would show a turn stuck inside a
   specialist it left, and would imply in the GxP record that the specialist authored everything
   that followed.

`reason` is the `task` tool's `description` — the supervisor's own stated reason — read off the
state the specialist is handed rather than off the tool call, for reason (1): the invocation
payload is what a specialist receives under any dispatch mechanism. It is best-effort by design; an
unrecognised state shape costs an empty `reason` on an otherwise correct handoff, never a failed
delegation, because nothing branches on it.

## What this does not decide

The routing choice itself (`task` tool vs. routing node) and the three M12 probes remain open and
still need a live stack. This narrows what those measurements have to settle: trace legibility was
one of the ADR's arguments for the routing node, and the handoff being observable under both
mechanisms removes it from the comparison. What is left to measure is routing *accuracy* and
per-specialist cost, which was always the argument that mattered.

## Verification

Falsified rather than asserted, twice:

- With the emitter stubbed to a no-op, all three new tests in `tests/test_agent_team.py` fail.
- With the enter announcement moved after the specialist's execution — both events still fire, in
  the wrong order — `test_a_delegated_turn_announces_the_handoff_and_the_hand_back` still passes
  and `test_the_specialists_own_output_falls_between_its_handoff_and_its_hand_back` fails. The
  ordering test earns its place.

The end-to-end test drives the production wiring (`build_langgraph_agent` with
`agent_teams_enabled`, the real `SubAgentMiddleware`, the real `_AttributedSpecialist`, the real
translator) with `build_chat_model` patched at the seam that exists for exactly that. The part that
was genuinely in doubt is whether a signal raised *inside a tool call* reaches the turn's stream at
all; a test of the mapping alone would have passed against the shipped, broken code.
