# D-2026-08-01-a-turn-you-can-follow-across-a-process — A turn you can follow across a process

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** F6-T5 (the OTel pipeline), D-141 (the
advisory `X-Chemclaw-*` headers), D-2026-08-01-every-process-carries-its-own-witness

## Context

`configure_telemetry` calls MAF's `configure_otel_providers`, and that was the entire tracing story.
What a collector received was the LLM client's own spans and nothing else: model calls with no
parent, no turn to hang them from, no tool call around them — and nothing at all from a connector,
because each connector process began an unrelated trace. `deploy/README.md` meanwhile stated that
"spans cover a turn and a job" and that "dashboards track loop iterations, tool latency, and job
status", none of which existed anywhere in the repository.

The readiness review's parenthesis is the part worth reading twice: *"the tell is that
`connectors/identity` propagates a **custom** correlation header."* That header is not merely
unrelated to tracing — it exists **because** the standard one was not being sent. Someone needed to
join a connector's records to a turn, W3C trace context was not available, and a bespoke mechanism
grew in its place.

## Decision

**Two spans, one propagation, and a deletion.**

- `chemclaw.turn` wraps a turn, pushed onto the `AsyncExitStack` `run_turn` already opens so the
  span's lifetime is exactly the turn's teardown and no second place has to remember to close it.
- `chemclaw.tool` wraps each tool invocation, inside the audit middleware that already brackets
  every call and times it.
- `traceparent`/`tracestate` ride on every connector request beside the existing headers, and
  `CallerLogMiddleware` adopts them, so a connector's spans are children of the turn.
- The claims in `deploy/README.md` are **deleted**, and what remains absent is named there.

`core/tracing.py` is inert when tracing is off — the default. `start_span` yields, `trace_headers`
returns `{}`. That matters more than it reads: this runs per tool call on the event loop that also
serves every SSE stream.

## Why not the alternatives

**Replace the correlation header with `traceparent`.** They look redundant and are not. The
correlation id is what `audit_events` is keyed on (`chemclaw explain <session>` walks it), it is a
value a chemist can paste into a bug report, and it works with no collector configured at all —
which is the shipped default. `traceparent` joins *spans*, live, and vanishes when tracing is off.
One is a durable join key in a database; the other is a live parent pointer. Deleting either would
lose a capability the other does not provide.

**More spans — per loop iteration, per retriever, per activity.** The complaint being answered is
that the documentation *overstated* the tracing. Answering it with a pile of spans nobody reads
would be the same error mirrored. A turn and a tool call are the two units a chemist and an operator
both already reason in ("40 seconds, 31 of them one xTB call"); everything finer is available from
the histograms `/metrics` already exposes.

**Trust `traceparent` the way the `X-Chemclaw-*` headers are trusted — i.e. not at all.** The
identity headers are advisory and must never reach an access decision, because a connector is
reachable by anything inside the network boundary. Trace context is different in kind and is
adopted without hesitation: the worst a forged `traceparent` achieves is attaching spans to a trace
that is not theirs. It grants no authority, so the rule that governs the identity headers does not
apply. Both arrive on the same request, so the asymmetry is stated where a reader meets it.

**A span around a durable job**, which the old docs claimed. It spans two processes and a Temporal
boundary, so the workflow payload would have to carry the trace context — and a payload is
*replayed*, so a stale `traceparent` would silently attach a replay to the original trace and
report a job as taking days. That is a design question, not another `start_span`, and it is now a
backlog row instead of a sentence in a README.

## Consequences

- A trace is a tree: the turn, its tool calls, MAF's model calls beneath them, and a connector's
  work beneath the tool that called it.
- `deploy/README.md` no longer describes a system that was never built, and says what is missing.
- One mutation is recorded because the test it beat was the wrong kind. Replacing
  `stack.enter_context(start_span(...))` with a plain assignment builds the context manager, never
  enters it, exports nothing — and passed a source-inspection check that could only see the call
  was *written*. A `with`-less context manager is a plausible refactor and a silent loss of every
  turn span, so the boundary is now exercised for real through `run_turn` with a fake agent. This is
  the third instance on this branch of a check too narrow to see the thing it was added for; the
  first two are in `tasks/lessons.md`.
- The tests drive a real in-memory OTel SDK rather than a mock, deliberately: the property under
  test is whether a parent-child relationship forms *across a header boundary*, and a mock would
  assert that this module called an API.
