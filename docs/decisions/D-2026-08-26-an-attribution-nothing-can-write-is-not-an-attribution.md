# D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution — the specialist carrier is deleted, the invariant it carried is not

## Status

Accepted. Sweeps up what `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` did not
reach. `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` stands unchanged and binds
whoever re-adds subagents.

## Context

`agent/audit.py` records, for every tool call, which person authorized the turn and which agent made
the call. The second half read a contextvar:

```python
event_agent = get_current_specialist()
```

`set_current_specialist` had **zero callers in `src/`**. Its only producer was
`agent/team.running_specialist`, deleted with the specialist team in D-2026-08-15.
`core/turn_signals.record_handoff` — which raised the `HandoffSignal` a surface would have drawn the
same span from — had no callers anywhere, tests included.

So `audit_events.agent` was `''` on every row that trail has ever written, while three docstrings
said otherwise in the present tense:

- `core/identity_context.py`: *"The **running specialist** rides here too … recorded beside the
  actor, never instead of it."*
- `agent/audit.py`'s module docstring: *"Each call records … which specialist ran it (beside the
  human, never instead — empty for the main agent)."*
- the `agent` field's own comment: *"A trail that names only the person cannot say which of five
  specialists ran a tool … it has to carry both, so it does."*

The last clause is the whole defect in four words. It did not.

Three tests kept the machinery alive by calling it directly: one set the contextvar and asserted the
audit row carried the value, one asserted the nesting reset restored an outer specialist, one
asserted the main-agent path recorded `""`. Every one passed. None of them could fail, because none
of them ran anything a deployment runs.

That is precisely the `map_to_hpc_identity` shape D-2026-08-15 names — *a claim that a control
exists* — and it is the shape that ADR deleted three other controls for. It simply did not sweep
these, because the specialist team's removal was scoped to the agent package.

## Decision

**Delete the plumbing; keep the column, the event and the rule.**

Deleted:

- `core/identity_context`: `_current_specialist`, `set_current_specialist`,
  `reset_current_specialist`, `get_current_specialist`.
- `core/turn_signals`: `HandoffSignal` and `record_handoff`, and the member from the `Signal` union.
- `api/graph_stream`: the `HandoffSignal → HandoffEvent` conversion, and the `agent` local that was
  threaded from a handoff pair nothing raised — it could only ever be `""`, so the events it fed now
  take the namespace-derived attribution directly.
- `agent/audit`: the read of the contextvar.

Kept:

- **`api/events.HandoffEvent`.** Dropping a member of the `Event` union is a coordinated change
  across `Chemclaw3_ui` and `Chemclaw3_mock`, and its own docstring already said it was declared and
  unproduced. What changed is that it is now *only* that: no signal can produce it here.
- **`audit_events.agent` and `AuditEvent.agent`.** `infra/sql/006` is merged and a merged migration
  is never edited. The field's comment now states that nothing writes it and why the shape is still
  the right one for when subagents return.
- **The invariant.** A subagent is an attenuation of its caller's authority, not a new actor
  (D-2026-08-10, invariant 3): attribution to *the agent* makes a trail worthless, attribution of an
  agent's act to a person is the D-040 failure. The place that rule lives is that ADR and this one,
  not a function with no callers. An invariant is not a function.

## What replaces the tests

Two, and the second is the one that matters:

- `test_the_audit_row_records_an_empty_agent_and_nothing_else_changes` — the row's full wire shape,
  `agent` written in rather than excluded, so the deletion is proven to perturb nothing already
  stored.
- `test_nothing_in_the_tree_writes_the_agent_column` — an **absence**, in the form
  `tests/test_upstream_surface.py` established: no module under `src/` constructs an `AuditEvent`
  with an `agent`. Whoever re-adds subagents fails this test, which is the point. The producer and
  the claim have to arrive together, and last time they did not.

## Consequences

~120 lines out, and the audit trail says exactly what it does.

The cost is real and small: when subagents return, the carrier has to be rebuilt. That is the right
trade in a repository where the alternative is a control that reads as present in three docstrings
and one database column, and is present in none of them. The constraint that governs the rebuild is
unchanged and is recorded twice over — deepagents builds a bare `SubAgent` dict with only
`spec["middleware"]`, so anything not compiled by `build_langgraph_agent` runs with no audit trail,
no authz and no plan gate, silently.

## What was measured rather than assumed

- `grep -rn "set_current_specialist" src/` → the definition and nothing else.
- `grep -rn "record_handoff" src/ tests/` → the definition and nothing else.
- `tests/test_audit.py`, `tests/test_langgraph_stream.py` and the full suite green with all four
  functions, the signal and the conversion removed.
