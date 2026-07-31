# D-159 — The turn stream reports a tool's lifecycle, not just that a call happened

## Status

Accepted. Implements W1.1–W1.3 of the dataflow review's plan; W1.4 is deliberately not in it (see
"What this does not do").

## Context

Token streaming in this service is genuinely incremental — one `TokenEvent` per model update, no
accumulation, and a pure-ASGI security middleware chosen specifically so nothing re-buffers the
body. Everything *around* the tokens was where the real-time experience broke.

**A tool call was announced only after it had already run.** `_ToolCallTrace` completes a call when
"an update goes by without adding to it" (D-138). For a streamed call, the next update to arrive is
the one carrying the call's *result*: the provider closes the argument JSON, the framework invokes
the tool, and nothing else comes down the wire until it returns. So the flush condition fired after
execution. The trace announced `predict_pka(...)` once the wait was already spent — and the waits
are not small: a calc job blocks inline for up to `inline_wait_seconds` (20s), an MCP tool up to its
`request_timeout` (60s for calc, 120s for bo). From the chemist's side, a working twenty-second
calculation and a hung server were the same thing.

**A tool's result never reached any surface.** The event union had `ToolCallEvent` (name plus a
truncated argument preview) and `ToolFailedEvent` (name plus a reason), and nothing carrying a
return value. The runner actively discarded result content, using it only as a signal that the call
had ended. So a computed number reached the chemist exclusively through whatever the model chose to
say about it, and a turn that died after a successful calculation lost the value with nothing on the
wire to recover it from. The UI's `TracePanel` documents this as an honesty constraint — it says
what was called and never implies it is showing what came back.

**Neither SSE response configured a keepalive.** No `ping=` on the turn stream or the push-back
stream, so a long tool wait put nothing at all on the connection. Anything between browser and pod
that reaps idle connections was free to drop it, and the client could not distinguish that from a
slow answer.

## Decision

**A call is announced the moment its arguments are complete, and its result is a first-class event.**

- `_ToolCallTrace` completes a call when its accumulated fragments **parse as JSON**. That is
  exactly when the provider has finished sending them and before the tool is invoked, so it needs no
  new provider signal and keeps D-138's promise that the event carries the whole argument preview —
  it simply stops waiting for the result to prove the arguments ended.
- The old "an update went by without adding to it" rule stays underneath as the fallback, so a
  provider streaming a non-JSON argument format still gets its call announced at the previous, later
  moment rather than never.
- `ToolResultEvent` (`tool`, truncated `preview`) is emitted when the result content arrives,
  matched back to its call by `call_id` — which is why the trace now remembers a call's name past
  the flush, since the result content does not carry one.
- `service_sse_ping_seconds` (default 15) is passed to both `EventSourceResponse` constructions.

### Why the result event is success-only

A call that raised already surfaces as `ToolFailedEvent` through the tool middleware, which holds
the exception and its message. Emitting both for one outcome would make every consumer decide which
to believe. The two are exhaustive — a call ends in exactly one of them — so `ok: bool` on the
result event, which the plan sketched, would have been a third way to say something already said.

### Why completeness-by-parse rather than a "tool started" event

A separate start event would have been the obvious shape, and it is worse in two ways. It doubles
the events per call for a surface that already has to correlate them, and it re-opens the question
D-138 settled: a start event cannot carry arguments, because the arguments have not arrived yet, so
the trace would go back to showing a tool name with no inputs. Moving *when* the existing event
fires gets the timing without either cost.

## What this does not do

**W1.4 — opening the stream before admission control — is not in this ADR.** The turn claim and the
semaphore still complete before `EventSourceResponse` is constructed, so a queued turn waits up to
`service_turn_admission_timeout_seconds` with no response and may then be shed with 503.

Moving acquisition inside the generator changes what a client sees under load: a stream that says
`queued` rather than an HTTP 503, which means a client retrying on 503 would instead receive a
200 whose body reports the problem. That is an API contract change, not a rendering change, and it
was raised as an open question with the plan rather than decided here.

## Consequences

- The worst dead-air window in the product becomes visible progress, for a change of flush
  condition rather than a new protocol.
- A computed value reaches the surface as data. The UI's honesty constraint — "shows invocations,
  never results" — can now be lifted, which is a follow-up in `Chemclaw3_ui`, not here.
- The event union grows to thirteen members. The UI drops unknown types by design
  (`normalizeEvent`'s allowlist), so the backend ships first with no breaking window; the same
  ordering the last two UI changes used.
- A long tool wait now keeps its connection alive on both streams.
- Three existing trace tests asserted the old, later timing and were rewritten to assert the new
  one. That is the change, not collateral: one of them named the result update explicitly as the
  moment of the flush.
