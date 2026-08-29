# The remaining actionable parts of C, D and E

Follow-up to `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`, which investigated
C (per-helper connector sessions), D (an advisor) and E (a second roster name) and built none of
them. What each investigation left actionable *now*, as distinct from what it left to a measurement:

## C — the stated reason is not the binding one

- [x] `agent/subagents.py` and `langgraph_agent._subagents` both give "two concurrent turns over one
      MCP tool object deadlock" as why a helper reaches no connector. That measurement is real and
      it is about **sharing one session object**; a helper holding sessions of its own shares
      nothing, and `open_connector_specs` already opens a fleet concurrently by design.
- [x] Replace it with the constraint that actually binds — the lifecycle — which is also the
      stronger argument: connectors are opened by the *async caller* into an `AsyncExitStack`
      before the *synchronous* builder runs, and the roster is frozen per compiled graph, so a
      per-helper set means a second full set opened eagerly on every turn.
- [x] `tests/test_subagents.py::test_a_helper_holds_no_connector_tool` cites the same wrong reason.
- [x] Behaviour unchanged. The BACKLOG row keeps the behavioural half, gated on the measurement.

## D — the guard the advisor investigation exposed, fixed without building the advisor

- [x] `tests/test_spend_cap.py::test_no_in_tool_model_call_passes_its_own_callbacks` guards the
      chain that puts a tool body's model call on the turn's ledger — and it guards it in
      **`agent/condense.py`**, by name. A second in-tool model call walks past it in silence.
- [x] The realistic mistake is not hypothetical: `verifier.py` passes `config=off_stream_metering()`
      deliberately, and `off_stream_metering`'s own docstring says attaching it to an in-graph call
      would take that call off the stream. Copying that line into a tool body is one edit.
- [x] Derive the module set instead: every module that defines a registered tool **and** makes a
      model call. Module granularity, not per-function — in `condense.py` the `.ainvoke` is in
      `_read_prose`, and the tool is `condense_protocols`, so a per-function scan misses it.
- [x] The advisor itself is **not** built: it cannot be enabled without a second model tier this
      deployment does not have, which is the shape `D-2026-08-15` deleted 3,300 lines for.

## E — nothing to implement, and saying so is the deliverable

- [x] The recommendation was to leave it closed; the BACKLOG row already carries the trigger. No
      code follows from "not yet", and adding a second roster name to be ready for one is the
      capability-that-ships-off shape again.

## Verification

- [x] The derived scan finds exactly the module the hardcoded one named, and would fail on a
      planted second offender.
- [x] `make lint type test`, reporting what it skipped.
- [x] Two ADRs, one decision each, and their ledger rows.

## Review

**Two of the three had an implementable part; E did not, and that is the finding rather than an
omission.**

**C — done, and the correction made the bound stronger rather than weaker.** The three places that
said "two concurrent turns over one MCP tool object deadlock" now say what actually binds: a
connector's tools do not exist until its session is live, so the async caller opens them into an
exit stack *before* the synchronous builder runs, and `SubAgentMiddleware` freezes the roster at
compile time — a helper cannot open sessions at spawn time even in principle, and its own set would
mean a second full set opened eagerly on every turn, spawned or not. The deadlock measurement keeps
the one job it fits, in `test_a_helper_holds_no_connector_tool`: it is why the caller's *already
open* tools must not be passed down, which is the edit that test exists to catch. No behaviour
changed.

**D — the advisor is still not built, and the trap it exposed is closed.** Building it now would
create a capability that cannot be enabled without a second model tier this deployment does not
have, which is the exact shape `D-2026-08-15` deleted 1,442 lines for. What *was* implementable is
the guard: `test_no_in_tool_model_call_passes_its_own_callbacks` scanned `agent/condense.py` **by
name**, so it guarded one file rather than the invariant, and a second in-tool model call would have
walked past it in silence — silence being the defect's own signature, since what fails is that spend
stops being counted. It now derives its module set from the tool registry: every module that defines
a registered tool *and* builds a model.

*Two things that decided the derivation's shape, both found by looking rather than reasoning.*
`agent/verifier.py` passes `config=off_stream_metering()` and is **right** to — a judge runs outside
the graph where nothing else meters it — and it holds **zero** registered tools, so requiring both
halves excludes it precisely; a naive "every module that builds a model" scan would have failed on
correct code. And in `condense.py` the `.ainvoke` is in `_read_prose` while the tool is
`condense_protocols`, so a per-function scan would have found **nothing** — module granularity is
not looseness here, it is the only granularity that sees the one call there is to see.

**The new scan was verified to fail rather than assumed to.** A planted second module holding a
registered tool and an `ainvoke(..., config=off_stream_metering())` failed the test naming the file
and the line; the by-name scan passed on the same tree. Then removed.

**E — nothing to implement, recorded in the row so nobody looks again.** Everything a second roster
name needs already exists — `AgentProfile.model_route` for its model, `helper_profile` for its
surface, `governed_roster` for its governance. What is missing is the reason, and a name added to be
ready for one is the capability that ships off and stays off.

**Gate:** `make check` green — lint, `mypy --strict`, and **6269 passed, 15 skipped**, run with
`dockerd` and `make up` first so the Postgres-backed suite actually executed. The skips are `helm`
not installed, a truncated git history the migration-additivity checks cannot use, and the live
prompt-caching probes whose credential has no credit.
