# D-2026-08-29-a-guard-that-names-one-file-guards-one-file — the in-tool model-call scan derives its own module set

**Status:** accepted · **Date:** 2026-08-29 · Generalises the guard added with
`agent/spend_cap.py` (`D-2026-08-29-an-iteration-cap-is-not-a-cost-cap`). Found while examining
whether an advisor should be built (`D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`);
**the advisor is still not built.**

## Context

A tool body may call a model. `condense_protocols` does, and that call's tokens reach
`agent/spend_cap.py` through a chain whose load-bearing link is an **absent** argument: the call
passes no `config`, so it inherits the graph's callbacks, so its usage rides the stream
`api/graph_stream` meters, so the cap sees it. `agent/turn_usage.py` states the hazard in as many
words — an explicit `callbacks` config **replaces** the inherited ones rather than joining them,
measured there at 55 tokens booked to the ambient ledger and 0 seen by the stream.

`tests/test_spend_cap.py::test_no_in_tool_model_call_passes_its_own_callbacks` guards that absence,
and guarded it in **`agent/condense.py`, by name** — correct on the day it was written, when that
was the only tool making a model call. It is a guard over one file rather than over the invariant:
a second in-tool model call walks past it in silence, and the failure is invisible by construction,
because the whole point of the defect is that spend stops being counted.

**The mistake it would walk past is one edit away rather than hypothetical.**
`agent/verifier.py` passes `config=off_stream_metering()` and is **right** to: a judge runs outside
the graph, where nothing else is watching, so it must meter itself. `off_stream_metering`'s own
docstring says attaching it to an in-graph call would take that call off the stream. Copying that
line into a tool body is the entire defect, and it is the natural thing to copy.

## Decision

**Derive the modules to scan: every module that defines a registered tool *and* builds a model.**

Both halves are what make the pair dangerous. A model call in a module with no tool runs outside
the graph and must meter itself — that is `verifier.py`, correctly excluded. A tool in a module
that calls no model has nothing to take off the stream. Only the intersection is this shape, and
the derivation reads the tool **registry**, so a module added next year is scanned the day its tool
is registered rather than the day somebody remembers this test.

**Module granularity, not per-function, and that is deliberate.** In `agent/condense.py` the
`.ainvoke` is in `_read_prose` while the registered tool is `condense_protocols`, so a scan of tool
bodies would miss the only call that exists to be found. The cost is that a module holding both a
tool and a legitimately off-stream call would read as an offender; none does, and the right answer
if one ever should is to split the module rather than to loosen the scan.

**The derivation asserts it found something.** A scan that silently matches nothing is a green test
that checks nothing, which is the same class of defect one level up.

## Consequences

- Verified by planting one: a second module with a registered tool and an
  `ainvoke(..., config=off_stream_metering())` fails the test naming the file and the line, where
  the by-name scan passed. Then removed. The assertion was checked to fail rather than assumed to.
- The advisor row in `docs/planning/BACKLOG.md` loses its "must be added to that scan in the same
  commit" clause — the scan now covers whatever module an advisor lands in, without an edit here.
- **The limitation is stated rather than implied**: a tool whose module calls a model only through
  a helper in *another* module is outside this scan, as is any model client not built through
  `build_chat_model`. The second is bounded by that function being the one place a model is built.
