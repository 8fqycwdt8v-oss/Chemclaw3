# D-2026-08-27-the-cap-is-a-property-of-the-loop-not-of-the-mode — the runaway cap attaches on every profile

## Status

Accepted. One-line change plus its regression test; the argument is why the line was safe to leave
wrong for as long as it was, and is not any more.

## Context

`enforce_loop_cap` was attached beside `TodoListMiddleware`, both gated on `harness_enabled_for` —
"matching MAF: the classic agent has no todo list and no loop cap, so attaching either
unconditionally would make this engine behave differently from the other while both are live." M13
deleted the other engine, and the reason expired with it: there was no second engine left to
diverge from, only a gate that had outlived its subject — the exact shape
`tests/test_upstream_surface.py` exists to catch for upstream workarounds, reproduced first-party.

What the expired gate cost was the shipped default. `harness_enabled=False`, so a default-profile
deployment ran with **no graceful stop at all**: the only bound was `agent_recursion_limit`, whose
expiry raises `GraphRecursionError` and discards everything the turn produced, after up to the
full turn deadline. That is precisely the failure `agent/loop_cap.py` argues a chemist must not
eat — end the run, let the partial answer out, mark it — available only in the mode nobody ships.

## Decision

`enforce_loop_cap` attaches unconditionally, on every profile and inside every helper. It is a
pure `before_model` counter over an untracked channel with no todo-list dependency; a runaway
model/tool cycle is a property of the loop, not of the plan/execute mode — the same argument that
already attaches compaction unconditionally ("an unbounded thread is a property of a session, not
of the plan/execute mode"). `TodoListMiddleware` stays harness-only: a classic turn has no plan
for the gate to read, and advertising `write_todos` there would be capability the mode does not
use.

The backstop relationship is unchanged and now universal: the cap is the graceful stop, the
recursion limit is the ceiling under it, sized so the cap always fires first.

## Consequences

- A classic-profile turn that loops now ends at `harness_max_loop_iterations` with its partial
  answer streamed and `loop_cap_reached` marked, instead of ten minutes of silence and a discarded
  turn. `tests/test_langgraph_agent.py::test_the_loop_cap_holds_with_the_harness_off` pins it,
  mutation-checked against the detached form.
- The prose in `agent/state.py` and `core/config/agent.py` that said the classic path had no cap
  is corrected in the same pass — prose describing a gate is how this one survived its reason.
