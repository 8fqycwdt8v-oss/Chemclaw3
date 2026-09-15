# D-2026-09-15-a-helper-shares-the-session-its-caller-already-opened — the bound was two claims, and driving them found both false for this shape

**Status:** accepted · **Date:** 2026-09-15 · Supersedes the behavioural half of
`D-2026-08-29-a-helper-reaches-no-connector-because-of-the-lifecycle-not-the-deadlock`, whose
lifecycle argument is kept and whose cost argument now points the other way.

## Context

A helper reached no connector tool, on a bound that had been restated twice and driven never.

The first statement was concurrency: *two concurrent readers of one MCP tool object deadlock*.
The second, `D-2026-08-29`, corrected the *reason* rather than the bound — the deadlock measurement
is about sharing a session, a helper with sessions of its own shares nothing, so what really binds
is the **lifecycle**: connectors are opened by the async caller into an `AsyncExitStack` before the
synchronous builder runs, the roster is frozen per compiled graph, and a helper would therefore
need a second full set opened **eagerly on every turn**, spawned or not.

That correction was right about the lifecycle and left the concurrency claim standing as the reason
the *cheap* shape — passing the caller's already-open tools down — was refused.
`tests/test_subagents.py::test_a_helper_holds_no_connector_tool` said so in as many words: *"what
this test protects against is passing the caller's already-open tools down, and that is the one
thing the deadlock measurement does cover"*.

Nobody had run it. What forced the question was a measurement of something else: with a helper
holding no connector, four of six typed specialist profiles would hold **nothing at all** —
`property-lookup` 0 tools, `safety` 0, `computation` 1, `design` 1 — because a specialist's whole
surface is connector tools.

## What was measured

Against two real `Chemclaw3-mcp` servers on loopback, over **one** `HeldConnectorSession`, driven
through the shipped `open_connector_specs`:

| arm | result |
| --- | --- |
| `props.solvent_properties`, 2 / 4 / 8 / 16 / 32 concurrent | 42 / 55 / 94 / 197 / 348 ms, **0 errors** |
| `pyexec.run_python`, 1.88 s per call, 4 concurrent | **1.99 s** wall — fully overlapped, 0 errors |
| a call that fails mid-flight beside a fast one | both resolve correctly; a third call afterwards succeeds |

The server's own log shows three ~1.9 s requests completing within 60 ms of each other on one
session, so the overlap is on the wire rather than an artefact of the client. **There is no
deadlock**, and there is no serialisation either.

The second half of the bound — *"the second's calls travel over the first's connection,
misattributing them in the connector's own log"* — does not reach a helper. `core/call_identity.py`
binds the headers from the ambient context at the moment the **session** opens, which is why a
session belongs to one turn; a helper runs inside its caller's turn and is the same actor, session
and correlation id by `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`. The headers are
correct, not merely tolerable.

And a third claim, repeated in three places as a reason the roster question could not be settled —
*"nothing counts how often `task` is called"* — is false and was false when it was written.
Driven on a compiled graph: `chemclaw_tool_calls_total{outcome="ok",tool="task"} 1`. `task` is an
ordinary tool in the caller's `ToolNode`, so it passes the same `@wrap_tool_call` chain as
everything else and `agent/audit.py::_count_outcome` counts it like everything else.

## Decision

**A helper holds its caller's already-open connector tools, minus every one that acts.**

- `helper_connectors` subtracts `side_effecting_tools()` — the same derived set `helper_profile`
  subtracts from the in-process half, so a bundle added next year is out of a helper's reach on the
  day it is enabled. Two halves, one switch: `helper=True` applies both.
- The objects are **shared, not reopened**. The caller's sessions are already open when
  `_subagents` runs, so this costs **zero** extra sockets, handshakes or server-side session state
  — better than either shape `D-2026-08-29` weighed, which is why it is the one that shipped.
- The lifecycle argument survives untouched and simply never applied here. It forbids a helper
  opening sessions of its *own*; it says nothing about being handed sessions that exist.

**No specialist roster.** Re-measured with connectors reachable, every profile now holds something
(`computation` 12, `evidence` 14, `design` 4, `safety` 4, `reporting` 3, `property-lookup` 1). It is
still not built, for the reason `agent/subagents.py` already gives and this measurement does not
change: a named partition is a routing hypothesis, the corpus built to test one measured a mediator
and settled nothing, and six descriptions in `task`'s schema are paid on every model call of every
turn. What *did* change is that the excuse is gone — the spawn rate is on `/metrics`, so the next
argument about this has a number under it.

## Consequences

- `tests/test_subagents.py::test_a_helper_holds_no_connector_tool` is replaced by
  `test_a_helper_holds_its_callers_reading_connectors_and_none_that_act`, which asserts a
  **narrowing** rather than an absence — so it cannot pass by the helper getting nothing, which is
  how the old test would have read if `connectors=` were dropped everywhere. Its acting name is
  derived from `side_effecting_tools()` rather than transcribed. Driven red on all three edits it
  claims to catch (the call site, the pass-down, the narrowing) before being believed.
- It reads the compiled roster out of upstream's `task` closure, because the edit worth catching is
  an *argument* and a helper this test built itself would agree with itself forever. The coupling is
  declared in `tests/test_upstream_surface.py`.
- **The cost is stated.** A helper's prefix grew: measured 26,626 tokens against its caller's
  66,316. It needs no new ceiling, because a helper's surface is a strict subset of its caller's and
  its prompt is its caller's plus `HELPER_BRIEF` minus the harness block — so the existing ceiling
  bounds it *as an inequality*, which
  `tests/test_context_floor.py::test_a_helpers_prefix_is_bounded_by_the_one_this_file_already_ratchets`
  asserts rather than a second number restates. The caller's ceiling did not move.
- `HELPER_BRIEF` and the `task` description both stop saying a helper cannot call a connector. A
  model that believes a false bound spends a turn learning otherwise.
- The delegation counter is pinned by
  `tests/test_subagents.py::test_a_delegation_is_counted_where_every_other_tool_call_is`, so the
  claim that it does not exist cannot come back.

## What keeps it true

- `tests/test_subagents.py::test_a_helper_holds_its_callers_reading_connectors_and_none_that_act`
- `tests/test_subagents.py::test_a_helper_holds_no_tool_its_caller_does_not`
- `tests/test_subagents.py::test_a_delegation_is_counted_where_every_other_tool_call_is`
- `tests/test_upstream_surface.py::test_the_task_tool_still_closes_over_its_roster_as_subagent_graphs`
- `tests/test_context_floor.py::test_a_helpers_prefix_is_bounded_by_the_one_this_file_already_ratchets`
