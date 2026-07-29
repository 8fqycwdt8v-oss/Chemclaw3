# D-111 — Stage C: the domain connectors, and two defects the migration surfaced

`safety`, `chem` and `calc` moved out of the agent's process to their own bundles (D-110's seam).
Five connectors ship now; `rdkit`, `tblite` and the calculation store's driver are no longer the chat
service's dependencies, which was the operational point of the exercise.

**Verification came first.** Two of the four safety-rubric invariants are MAF function middleware
over tools we did not write, and MAF assembles MCP tools into a run's tool list separately from the
configured ones — so whether audit and authz reach a connector's tools is a property of the framework
that no amount of reading our wiring establishes. `tests/test_connector_safety_rubric.py` drives a
real agent, MAF's own tool-calling loop and a real connector server, and asserts on the audit sink
and on what the server observed. Both hold: a connector call is audited with the turn's actor, and a
`tool_role_gates` denial is recorded as an error while the tool body never runs. Had either failed,
the migration would have been unsafe and Stage C would not have proceeded.

**Two defects, both found by the existing suite, both fixed at the root:**

1. **Swallowing `CancelledError` in `connectors.transport` broke the front door's turn bound.** The
   degrade-on-connect-failure mixin caught it — following MAF, which swallows it in its own MCP paths
   because an internal `anyio` cancel scope is indistinguishable from a real cancellation. At *this*
   layer it is distinguishable: `Task.cancelling()` is non-zero only when cancellation was requested
   on this task, which an inner scope never does. Without that check a hung turn ran to completion
   holding its admission permit — precisely the collapse `service_turn_timeout_seconds` exists to
   prevent, and a much worse failure than the one the swallow was protecting against.

2. **`AgentProfile.tool_names` could no longer reach a migrated tool.** Profiles had two dials —
   `tool_names` for in-process tools, `mcp_server_names` for whole connectors — which was coherent
   while capability lived in-process and became incoherent the moment it did not: a profile could
   name a whole `calc` connector but not "just the two predictors". `tool_names` now spans both
   halves, narrowing the in-process tools *and* each connector's agent-facing allow-list, dropping a
   connector left with no named tool. Mutating `allowed_tools` per instance is safe only because
   connectors are per-turn objects (D-110) — on a shared connector it would have been a cross-turn
   surface change. The unknown-name check moved to the union, since only a view of the whole surface
   can tell a typo from a name that lives on the other side of the boundary.

**A boundary clarification worth stating.** `calc` exposes `report_measurement`, which writes. The
read/compute-only rule for a connector's agent-facing tools is about the *knowledge graph and the
fingerprint index* — the paths the PR-gate governs — not about all state: a capability's own store is
its own business, and the calibration ledger is `calc`'s. What remains structurally impossible from a
connector is unchanged: it cannot write a graph note (its only route is a job result core publishes)
and cannot launch durable work (a `jobs:` entry is a core-generated tool).

Remaining in Stage C: `kg` (needs a decision on whether it also owns re-indexing) and `bo` (whose
workflow moves to its own worker, taking `start_optimization_campaign` onto the generic job path with
it). See `tasks/todo.md`.
