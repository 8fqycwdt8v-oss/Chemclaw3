# D-2026-08-26-a-request-timeout-bounds-the-wait-not-the-work — a timed-out MCP call now tells the connector to stop

## Status

Accepted. Found reviewing the three seams between this repository, `Chemclaw3_ui` and
`Chemclaw3-mcp`, and fixed in the same pass.

## Context

`HttpEndpoint.request_timeout` is the manifest's statement of how long one tool call may take.
`registry.request_timeout_seconds` derives two bounds from it — the MCP session's
`read_timeout_seconds` and the looser httpx read timeout — and `D-131`'s successor work established
which of the two must trip first and why. Both of those bound **this side's wait**.

Neither bounds the connector's work, and nothing else did either.

`mcp.shared.session.send_request` waits inside `anyio.fail_after(read_timeout_seconds)` and, on
expiry, raises `McpError` and sends the server nothing at all: no `notifications/cancelled`, no
closed stream. The session is not closed either, because a turn holds it open for its whole
duration by design (`connectors.transport.HeldConnectorSession`, and the per-turn lifetime is what
makes the identity headers truthful in the first place). So the call is abandoned here and runs to
completion there.

Measured against a running server, a 30 s tool behind a 2 s bound: the tool body was interrupted
only when the session was finally torn down, which is the end of the turn. Its answer was computed
and discarded.

**Why that is worth an ADR rather than a shrug.** The fleet's own guidance says the promise is
statelessness, not speed, and names the consequence: "if a call is interrupted the caller calls
again." `servers/calc` is documented as "a call here can be minutes or hours, deliberately", and
`science/calc/store.cached_compute` is a check-then-act that recomputes a miss. Put together, an
abandoned CREST search held a pod's CPU while the next attempt started a second identical one
beside it — and the deployment has no way to see either, because from this side the call already
failed.

The server half of the protocol was already there: `mcp.shared.session` cancels the in-flight
request when it receives `notifications/cancelled`. Only the client never sent one.

## Decision

`core/mcp_session.cancel_on_timeout` wraps a live `ClientSession` so that a request which outlives
its read bound sends `notifications/cancelled` for that request, then re-raises the caller's
`McpError` unchanged. It is installed on both of this repository's MCP client paths — the kernel's
`open_session` (the calc backend, the reaction labeller) and the agent's per-turn sessions in
`connectors.transport` — because a rule with two call sites and one implementation is the shape
`core/mcp_session.py` exists for.

**The follow-up ping is part of the decision, not an implementation detail.** Sending the
notification alone does not work over streamable HTTP: the POST is issued and answered `202`, and
the server session does not observe the message until further traffic moves on that session.
Reproducibly, the cancelled tool ran on for the full ten seconds the probe waited. Issuing a `ping`
on the same session immediately afterwards makes delivery deterministic — measured both ways, over
repeated runs — so that is what ships. One round trip on a path that has just spent its entire
timeout is affordable, and the ping doubles as evidence the session survived (it does: an ordinary
tool call on the same session answers normally straight afterwards).

The ping is issued through the **unwrapped** `send_request`. Through the wrapper it would be
subject to the same bound, and a session that had stopped answering entirely would time out on the
ping, re-enter the wrapper, and ping again without end.

Two upstream shapes are read and both are pinned in `tests/test_upstream_surface.py`: the request id
comes from `BaseSession._request_id`, and the timeout is recognised by the SDK's own
`httpx.codes.REQUEST_TIMEOUT`. Neither is published API. Reading `_request_id` immediately before
delegating is safe under concurrent tool calls because a coroutine runs synchronously to its first
`await`, and `send_request`'s first `await` is the stream write — nothing can interleave between the
read and the increment.

Failure to cancel is logged and swallowed. A transport that is already gone cannot carry a
cancellation, and replacing the caller's real error with that one would trade a wasted computation
for a lost diagnosis.

## Consequences

- An abandoned call stops the connector at the moment the caller gives up rather than at the end of
  the turn. `tests/test_connector_transport.py::test_a_timed_out_call_tells_the_connector_to_stop_working`
  asserts the *tool body* observed cancellation, not that the client raised — the client raised
  before this change too, which is exactly why the gap survived the test that was already there.
- Every timed-out call now costs one extra round trip. That is the price of the flush and it is
  paid only on the failure path.
- **What this does not cover, stated rather than implied:** a caller that vanishes without an
  orderly timeout — a killed pod, a severed network — still leaves the connector computing. The
  belt for that is a server-side wall clock in `Chemclaw3-mcp`, which this ADR does not decide;
  what it settles is the case where this side *knows* it has stopped waiting.
- The two `test_upstream_surface.py` assertions are the tripwire for an SDK bump. Neither fails
  open in the dangerous direction — a missed timeout means no cancellation, which is the behaviour
  this replaces — but both mean the fix has silently stopped working, which is why they are pinned
  rather than trusted.
