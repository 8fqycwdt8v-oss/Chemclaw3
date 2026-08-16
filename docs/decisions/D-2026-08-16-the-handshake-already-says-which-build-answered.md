# D-2026-08-16-the-handshake-already-says-which-build-answered — the trail names the server's build, not only the orchestrator's

**Status:** accepted · **Date:** 2026-08-16

## Context

`audit_events.revision` was added by D-140 (AG-14) to answer a single question: which build of
Chemclaw produced this result. It reproduced a number because it named the whole system — the
prompt, the routing, the skills and the chemistry were one image, released together.

`D-2026-08-16-the-physics-leaves-the-cache-stays` ended that. `predict_pka` no longer computes
anything in this process; it dials a `Chemclaw3-mcp` server that ships from another repository on
another cadence. The column kept filling in, kept being correct, and quietly stopped answering the
question it was built for: it now names the commit of the orchestrator that *asked*, and says
nothing about the solver that answered.

Nothing failed. That is the characteristic of this defect class and the reason it is worth an ADR
rather than a commit — the same shape as the revision field's own first eight months, where the
function, the column and the test were all present and correct and no build ever set the variable
(REV-17). A provenance field that has stopped covering what it names looks exactly like one that
has not.

## Decision

**The revision rides the MCP `initialize()` handshake, and is recorded in its own column beside the
orchestrator's.**

Three parts, one in each repository and one at the seam:

1. **`Chemclaw3-mcp`** — `connector_app` stamps `MCP_SERVER_REVISION` onto the lowlevel server's
   `version`, which `initialize()` returns as `serverInfo.version`, and onto `/healthz` for a probe
   that has no session. Each `Containerfile` threads it from an `ARG CHEMCLAW_REVISION` the release
   build fills with the commit.
2. **This repository** — `connectors/transport.py::_stamped` reads that version off the handshake it
   already performs and writes it into each loaded tool's `BaseTool.metadata` under one shared key.
3. **The trail** — `agent/audit.py::_served_by` turns that into `"<connector>@<revision>"`, and
   `audit_events.tool_revision` (migration 045) stores it beside `revision`.

### Why the handshake, and not anything else

Every session already calls `initialize()` and already receives `serverInfo{name, version}`. So the
fact costs **no new endpoint, no extra round trip, and no field on any tool result** — which is what
makes it worth doing at all. The three alternatives each cost more and answer less: a `/version`
route is a second call per turn, a field on every tool's return value changes fifteen signatures and
puts provenance in front of the model, and a header is invisible to the stdio transport.

Left alone, that version reports the **MCP SDK's** own release — `1.29.0` — a true fact about the
wrong thing that reads as provenance to anyone who does not check. Measured, not assumed: removing
the stamp turns the fleet test red with `assert '1.29.0' == 'abc1234'`.

### Why a second column rather than overwriting `revision`

Both facts are wanted and neither substitutes for the other. A prompt change and a solver change
produce different numbers for the same question, and a trail carrying one SHA cannot say which
moved. That is migration 044's argument in a second place — two questions, two columns — and it is
also why `purpose`, the one column deliberately empty because nothing can fill it honestly, is not
spent on this.

### Why the tool carries it, rather than a map threaded through the turn

The alternative was a `{tool_name: revision}` mapping returned by `open_connector_specs` and passed
through four callers and a builder parameter to `make_audit_middleware`. It duplicates the structure
of the tool list it would travel beside, and a tool present in one and absent from the other is a
silent misattribution — the worst failure available to a provenance field. `BaseTool.metadata` is
the field LangChain provides for exactly this, and it keeps the fact attached to the thing it is
about.

The cost is stated rather than hidden: `_served_by` is **the only place in either governance chain
that reads `request.tool`**, which `tool_authz` explicitly refuses to do because `ToolNode` passes
`tool=None` for a name the graph does not hold. That refusal is right for a *refusal* — one that
depended on this field would fail open on exactly the calls it exists to stop. It is safe here
because this one is observational and its degenerate case is already the correct answer: no tool
object means no connector means no server revision, which is the empty string an in-process tool
yields anyway.

### The three states, and why `""` is not `"unknown"`

| value | meaning |
|---|---|
| `""` | No out-of-process server was involved. `revision` covers the build. |
| `"<connector>@<sha>"` | That server, at that build, answered. |
| `"<connector>@unknown"` | A remote server answered and could not name its build. |

The third is a fixable deployment mistake — an image built without `--build-arg CHEMCLAW_REVISION` —
and it must read as one. Collapsing it into `""` hides it; filling `"unknown"` in for every
in-process `write_todos` call buries the real cases under noise. No backfill is needed for the same
reason `""` is the default: every row written before the physics left was in fact computed in this
process.

## Consequences

- **The gap this closes is one-way.** A tool call now names both builds. A *cached* calculation
  still reports the `calc_version` the server transported with it
  (`D-2026-08-16-a-key-the-caller-cannot-see-is-a-key-the-caller-can-poison`), which is the
  finer-grained answer for that path and is deliberately not duplicated here.
- **One shared constant, `transport.SERVED_BY`**, because a key spelled in two files is a
  provenance field that stops being filled the day one of them is renamed — and a blank column reads
  exactly like an in-process call.
- **A cross-repository coupling now exists and is asserted from both ends.** `Chemclaw3-mcp` reaches
  through `FastMCP._mcp_server` because `FastMCP.__init__` takes no `version`; this repository reads
  the result back through a real served session in
  `tests/test_connector_transport.py::test_a_tool_carries_the_build_of_the_server_that_answers_it`.
  An upstream rename turns tests red in both repositories rather than silently reverting every
  server to reporting the SDK's version.
- **The audit row grew a field, and the guard against that noticed.**
  `test_the_main_agent_records_an_empty_specialist_and_nothing_else_changes` compares the whole
  event dict, so the widening had to be written into its expectation rather than added to an exclude
  set — an exclude set that grows with each new field is a guard that checks less every time it is
  updated.

## Verification

- `make lint type test` green; migration 045 applied against a real Postgres, and
  `tests/test_audit_store.py` round-trips both provenance columns through it.
- Mutation-checked in both repositories rather than reasoned about: removing the stamp, removing the
  `_served_by` call site, dropping a Containerfile's `ARG`, and transposing the two revision columns
  in the `_INSERT` value tuple each turn a specific test red.
- The end-to-end assertion runs against a **real** uvicorn-served `FastMCP` opened through
  `open_connector_specs`, not a stubbed session, because every claim is about what survives the
  allow-list filter, the holder task and the adapter.
