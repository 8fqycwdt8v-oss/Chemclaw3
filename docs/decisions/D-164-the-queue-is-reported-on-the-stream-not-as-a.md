# D-164 — The queue is reported on the stream, not as a refusal

## Status

Accepted. Implements W1.4 of the dataflow review's plan, the one item D-159 left open because it
changes an HTTP contract rather than an event contract.

## Context

`POST /sessions/{id}/messages` is admission-controlled (D-057): the turn takes one of
`service_max_concurrent_turns` permits and holds it for its whole streamed run, so a burst cannot
pile onto the shared internal LLM endpoint. The acquire waited up to
`service_turn_admission_timeout_seconds` (default 5) and then shed with **503**.

All of that happened *before* `EventSourceResponse` was constructed. So a turn that had to wait
produced no bytes at all for the length of the wait — no headers, no event, nothing — and then
either began streaming or returned a bare 503. From the browser, five seconds of that is
indistinguishable from a hung server, an unreachable pod, or a dropped connection. It is the same
defect the rest of W1 fixed everywhere else in the turn (a tool call announced only after it
returned, a result that never reached the wire at all): work was happening and the surface could
not say so. This was its last instance, and the worst one, because it is the *first* thing a turn
does — the dead air lands before a single token, when a chemist has the least evidence that
anything was received.

D-057 recorded the constraint that produced the ordering: "acquiring before the response is
constructed is what lets the route return a clean 503 — moving the acquire into the generator, as
would be needed to stream a `queued` event, is a larger change." That was true, and it is the
change here.

## Decision

**The permit is acquired inside the stream.** The route hands back `EventSourceResponse`
immediately; the generator takes the permit as its first act. A turn that has to wait emits a
`QueuedEvent` first, and a turn that never frees a permit ends with an `ErrorEvent` carrying the
same "server at capacity; retry shortly" wording the 503 body used.

### `queued` is emitted only when there is an actual wait

`asyncio.Semaphore.locked()` is false exactly when `acquire()` will return without suspending, and
there is no `await` between the test and the acquire for another turn to slip through, so the
uncontended path costs one attribute read and emits nothing.

Emitting it unconditionally would have been simpler by one branch and wrong twice over: every
surface would render a queue state that is already gone by the time it paints, and the new
`chemclaw_turns_queued_total` counter — the point of which is to tell an operator whether the
front door is contended — would read as saturated while the process is idle.

The event carries no fields. The client is already connected and has nothing to decide with a
number; the next event says which way the wait went. A `timeout_seconds` field would be a server
policy that no consumer acts on.

### What stays a status code

The 409 (a turn already running, in-process and durable) and the 429 (budget exhausted) pre-checks
keep their place ahead of the response. They are **refusals**, not waits: nothing is going to change
in five seconds, there is no progress to report, and a refusal that a client must parse out of a
200's body is worse for every consumer than the status code it already handles. The distinction is
the whole rule — *a wait is streamed, a refusal is a status.*

### The cost: 503 no longer appears on this route

This is a real contract change and the reason the item waited for a decision rather than shipping
with the rest of W1. A client whose retry policy keys on 503 will see a 200 whose body reports the
problem, and will not retry unless it is taught the `error` event. There is no way to have both:
the status line is sent when the response is constructed, and constructing it is precisely what
must happen before the wait to make the wait visible.

Two things bound the cost. `_database_unavailable` still answers 503 on the same route, so a client
that treats 503 as retryable stays correct — it just stops being the *capacity* signal. And the
shed path was never the common case: the load test that motivated admission control recorded zero
sheds at the shipped worker count.

## Consequences

- A busy front door and a dead one are distinguishable, which they were not, for the whole of the
  admission window.
- `chemclaw_turns_queued_total` is added alongside `chemclaw_turns_shed_total`. They mean different
  things and are the two halves of a contended door: queueing is the system absorbing a burst,
  shedding is it declining one. Neither has an HTTP status left to be counted by at the load
  balancer, which is exactly why both must be counted in the process.
- `chemclaw_turns_started_total` now increments *after* the wait rather than before it, so it
  counts turns that actually ran. A queued-then-shed turn is in the queued and shed counters and in
  neither the started nor the failed one.
- The permit release moves onto a flag (`permit`), because the generator can now end without ever
  having held one. Releasing a permit that was never taken would have manufactured capacity out of
  a shed turn — the failure mode that collapses admission control silently, in the direction that
  looks like everything working.
- `_AT_CAPACITY` is now one literal, said in two shapes: an error event here and the 503 body in
  `_database_unavailable`, which has always deliberately reused this path's wording.
- The bundled dev page renders it; `tests/test_dev_page_events.py` (D-159) would have failed the
  build otherwise, which is what that test is for.
- The UI's `normalizeEvent` allowlist drops an unknown type, so the frontend is safely behind until
  it adds `queued` — the same ordering every event in W1 shipped under.
