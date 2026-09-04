# D-2026-09-04-a-name-is-one-capability-across-every-namespace — a connector may not claim a name this process already binds

**Status:** accepted · **Date:** 2026-09-04 · Closes the ambient-name row `docs/planning/BACKLOG.md`
carried. Narrows nothing in `D-118`; the connector seam still adds capability without a core edit.

## Context

`_declared_tool_names` refused a connector-declared tool name that collided with **another
connector's**, and never one that collided with a first-party `@tool` or with a middleware-supplied
ambient verb. Two reviewers found it from opposite directions in the same pass — one looking for a
way to replace `propose_knowledge_note`, one reading `ToolNode` dispatch — which is how a single
cause came in as three findings.

Measured at HEAD, all three reproduced:

- A connector **job** named after a first-party tool was silently dropped, *and* re-classified that
  first-party tool as state-changing and expensive — so a read-only tool started being gated and
  billed as a mutation.
- A connector **endpoint** tool named after a first-party tool won `ToolNode` dispatch outright.
- `make connector-validate` passed either way, and passed for exactly the two bundles whose surface
  it cannot see: `chem` and `safety` are served from `Chemclaw3-mcp`, so the manifest is all there is.

The third is what makes the first two more than theoretical. The gate that would have caught a
hostile or careless manifest is the one that cannot look at the bundles most likely to carry one.

## Decision

**One name is one capability across every name space this process binds.** `_bound_by_this_process()`
seeds the collision check with the in-process `@tool` registry plus the eight ambient names
`FilesystemMiddleware` and `SubAgentMiddleware` supply, and the check raises `ConnectorError` at
agent-build time *and* inside `make connector-validate`.

Two details are load-bearing and were measured rather than reasoned:

**It imports `chemclaw.agent.chemclaw_agent` at function scope.** The registry holds **zero** tools
at `connector-validate` time, so seeding the check without forcing that import would have produced a
check that passes everything — a control that reads as one and is not. The import is the declared
`connectors → agent` edge, the same load-bearing one `cli/validate_templates.py` documents.

**Generated job launchers are excluded by their generating module**, not by name. Without that, a
second `build_langgraph_agent` in one process reads its own output back as a collision.

Cost: **0.014 ms** per build. No shipped bundle is affected — 60 declared names against 31
in-process, 8 ambient and 9 launchers, with zero overlap.

## Consequences

A third-party bundle that declares `read_file`, `task`, `write_todos`, one of the other ambient
verbs, or any first-party tool name now **fails to load, loudly**, at build and at validation. That
is the intended behaviour and it is a real restriction: a code-execution server would reasonably
want `read_file`, and it must now pick another name.

The rule binds whoever adds a future tool source. The check reads the surface the process binds
rather than a list, so a tool an upstream bump introduces is covered the day it is bound — the same
argument `_bound_tools` makes for the context floor, for the same reason.

**Still open, and narrower than it was**: `build_langgraph_agent(connectors=[...])` accepts a
hand-built `BaseTool` that shadows a first-party name. Production reaches the tool surface only
through manifests, so this is closed in practice; the belt-and-braces check would live in
`agent/langgraph_agent.py` and is a `BACKLOG.md` row rather than part of this decision.
