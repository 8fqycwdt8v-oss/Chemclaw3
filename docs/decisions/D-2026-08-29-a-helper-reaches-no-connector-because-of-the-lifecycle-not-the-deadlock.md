# D-2026-08-29-a-helper-reaches-no-connector-because-of-the-lifecycle-not-the-deadlock — the bound stands, the reason given for it did not reach the case

**Status:** accepted · **Date:** 2026-08-29 · Corrects the reason recorded by
`D-2026-08-15-a-harness-is-adopted-whole-or-its-defaults-are-inherited-silently` and repeated by
`D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`. **Behaviour is unchanged**: a
helper still reaches no connector.

## Context

Three places said why a helper holds no connector tools — `agent/subagents.py`,
`langgraph_agent._subagents` and `tests/test_subagents.py` — and all three said the same thing:

> A helper is concurrent with its caller by construction, and two concurrent turns over one MCP tool
> object deadlock.

The measurement is real. D-110 recorded it, `tests/test_langgraph_connectors.py` pins the per-turn
shape it forced, and it is why a graph is compiled per turn at all. **It is about sharing one
session object**, and the question it was being quoted for is a different one: whether a helper may
have connector sessions *of its own*. A helper holding its own sessions shares nothing, and
`open_connector_specs` already opens a whole fleet **concurrently** by design — with each
`HeldConnectorSession` confining its `anyio` cancel scope to a task of its own precisely so that
entering them together is legal. So the stated reason forbade the shape nobody proposed and said
nothing about the shape somebody would.

This is the failure mode this repository names elsewhere and did not catch here: a control whose
recorded justification is broader than the evidence under it. The risk is not that the bound is
wrong — it is that the next reader either removes it, having noticed the gap, or leaves it in place
believing a measurement covers it.

## Decision

**Record the lifecycle as the binding constraint, and keep the deadlock measurement where it
actually applies — passing the caller's already-open tools down.**

Connectors are opened by the **caller** — the front-door runner, the CLI, a template activity —
into an `AsyncExitStack`, and they are opened **before** the graph is compiled, because a
connector's tools do not exist until its session is live (`load_mcp_tools` needs one).
`build_langgraph_agent` is **synchronous** and receives them already open. The roster is fixed per
compiled graph: `SubAgentMiddleware` sets `_subagents` once and freezes `subagent_names`, so the
surfaces a turn can spawn are decided at compile time.

Three consequences, and together they are the bound:

- A helper **cannot** open sessions at spawn time. There is no async context at the point where its
  graph is built, and no way to add a roster entry after the graph is compiled.
- Giving it its own set therefore means the caller opening a **second full set eagerly, on every
  turn**, whether or not a helper is ever spawned — twice the sockets, handshakes and server-side
  session state, on a path whose tail already cost six sequential connect timeouts the day a fleet
  went dark.
- The benefit that would buy is unmeasured: nothing counts how often `task` is called, and the
  corpus that was supposed to measure delegation was deleted with the specialist team.

**The deadlock measurement keeps one job**, and `tests/test_subagents.py::test_a_helper_holds_no_connector_tool`
is where it belongs: it is exactly why the caller's open tools must not be passed down, which is the
edit that test exists to catch. Two readers of one MCP tool object deadlock; that claim is true, is
measured, and is about sharing.

## Consequences

- No code changes. Three docstrings do, and the test's docstring now separates what it protects
  against (passing the tools down) from why the alternative shape is not built (the lifecycle).
- **The cheap form, if the measurement ever asks for it, is not a second eager session set.** It is
  a lazily compiled roster entry — which is a change to a shape upstream owns, and belongs in
  `tests/test_upstream_surface.py`'s count before anything relies on it.
- `docs/planning/BACKLOG.md` keeps the behavioural half of the row, gated on the delegation
  measurement rather than on this argument.
