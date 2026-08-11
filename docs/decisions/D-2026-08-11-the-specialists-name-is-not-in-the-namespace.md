# D-2026-08-11-the-specialists-name-is-not-in-the-namespace — Attribute a specialist's events from the handoff, because the graph path never held its name

**Status:** accepted · **Date:** 2026-08-11 · Supersedes the attribution half of M9's event work.

## The defect

M9 gave three events an `agent` field so a team's trace would not read as though one actor did
everything, and derived it from the subgraph namespace:

```python
def _agent_of(namespace: tuple[str, ...]) -> str:
    if not namespace:
        return ""
    return namespace[-1].split(":", 1)[0]      # "<node>:<task-id>" → "<node>"
```

The assumption is that a specialist's updates arrive under `("<specialist>:<task-id>",)`. They do
not. Measured on this engine:

```
namespace=('tools:e619de6a-8779-6d32-25f0-1369526240c6',)  -> _agent_of = 'tools'
```

`SubAgentMiddleware` invokes the compiled specialist as an ordinary runnable **inside the `task`
tool**, not as a named node of the parent graph. The only frame on the namespace is therefore the
parent's tool node, and **the specialist's name is not on the path at all** — not in a different
position, not in a different format. There was nothing there to read. Every event every specialist
ever raised was attributed to an agent called `"tools"`.

## How it survived

Two failures compounded, and both are ones this repository has recorded before.

**The unit test asserted against an invented shape.**
`test_the_agent_attribution_is_read_from_the_subgraph_namespace` parametrized
`("evidence:7f3a",) → "evidence"` and `("supervisor:1", "safety:2") → "safety"`. Both are
hand-written fixtures; neither is a namespace the engine emits. The test proved the helper computed
what its author believed, which is the one thing a test of a *reader* must not be allowed to prove
on its own.

**Nothing had ever run a team.** `agent_teams_enabled` ships off pending M12, so the only code path
that could expose the defect was the one path no test and no live run exercised. A field that is
wrong only when a disabled feature is enabled looks exactly like a field that is right.

It was found by enabling the team on a live lane and reading the routing report:

```
## Mis-routes
- **team** rt-01: expected evidence → tools
```

The suite reported a supervisor mis-route. The supervisor had routed correctly; the harness was
reading a field that could not hold the right answer.

## The decision

**Attribute from the handoff pair, tracked as state across the stream**, and delete `_agent_of`.

`graph_events` keeps one variable: the handoff's `to` on entry, cleared on the hand back. Every
event from a completed node is stamped with it. This is correct where the namespace could not be,
for one reason: `agent/team.running_specialist` raises the handoff **with the name it was
constructed with**, rather than reconstructing a name from a graph path. It is the difference
between reporting an identity and inferring one.

Reading it as stream state is safe because the pair brackets the specialist's execution in stream
order — asserted, not assumed, by
`test_the_specialists_own_output_falls_between_its_handoff_and_its_hand_back`.

This is the second thing to fall out of
`D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs`, and it is worth naming: that ADR
placed the handoff at the specialist's *invocation* so the event would survive the open routing
choice. The same placement is what makes it the only component in the system that knows a
specialist's name at the moment the specialist runs — so the "unproduced event" and the "wrong
attribution" turn out to be one defect with one fix, which is not how either was filed.

## The rule this leaves

**A test whose subject is a reader of an external shape may not supply that shape by hand.** Drive
the producer and read what comes out. The deleted test's replacement does exactly that; so does
`test_a_specialists_events_are_attributed_to_the_specialist_not_to_the_tool_node`, which fails with
`assert 'tools' == 'safety'` — the live symptom, reproduced in the suite — when the namespace
reader is restored.

## Verification

- The real namespace was dumped from a delegated turn before anything was changed (above).
- The new end-to-end test passes with the fix and fails with the old reader, reproducing the exact
  live string.
- `tests/test_agent_team.py`, `tests/test_langgraph_stream.py`, `tests/test_m12_probes.py`: 72
  passed.
