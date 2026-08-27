# D-2026-08-27-a-disconnect-is-a-detach-not-a-stop — a turn belongs to the request, not the socket

## Status

Accepted. Found by a whole-engine reliability audit; implemented in the same pass across this
repository and `Chemclaw3_ui`.

## Context

Closing the SSE turn stream was the only way a client could stop a turn, so the front door read
*every* disconnect as cancellation — the Stop button, a Wi-Fi handoff, a closed laptop lid and a
stalled reader past `service_sse_send_timeout_seconds` were one event. `Chemclaw3_ui`'s own
`streamTurn` documents the chain approvingly: aborting the fetch closes the socket, FastAPI sees a
client disconnect, the turn is cancelled and the session's turn lock released.

What that cost on the audit's reading: a 10-minute multi-tool turn died with the connection that
happened to be carrying it. The partial answer was lost from the live view *and* from the durable
transcript, because `_record_transcript` runs only after the answer is assembled — so the work, the
tokens and every tool result were spent and nothing anywhere could show the chemist what they
bought. The UI deliberately never retries the stream (retrying a non-idempotent POST double-spends
or collides with the turn lock, correctly), so there was no recovery path at all.

## Decision

The two meanings are separated.

**A disconnect detaches.** The turn runs on a pump task of its own from the moment the response is
handed off (`api/detach.DetachableTurn`); the SSE response is a *view* of it. Cancelling the view —
a drop, a send timeout — marks the reader gone and nothing else: the turn runs to completion, the
checkpointer and the transcript land exactly as they would have, and the generator's own `finally`
releases the admission permit, the in-process lease and the durable claim at the turn's **true**
end. A session therefore stays 409-busy for exactly as long as a turn is genuinely running,
watched or not. While a reader is attached, the pump `put`s into a bounded queue, so a slow client
still exerts the backpressure the direct generator gave; once the reader goes, events are
discarded — memory buffered for nobody is memory spent on nobody.

**A stop is a request.** `POST /sessions/{id}/turn/stop` — owner-gated by the same
`_resolve_session` dependency as the turn route, 404 either way so a stranger cannot learn whether
a session is mid-turn — cancels the pump, which delivers the same `CancelledError` into `run_turn`
that a disconnect used to. Every teardown path built for D-130 runs unchanged, in the pump task
whose context stamped the ambients. `Chemclaw3_ui`'s Stop now posts the stop first and aborts the
fetch second; on an *accidental* drop it polls the transcript instead of surfacing a dead-end
banner, because the answer is coming.

**The old posture is one setting away.** `service_turn_survives_disconnect=false` restores
disconnect-cancels-the-turn exactly, for a deployment that prefers cost over completion.

## Why the v3 veto does not apply

The shape that vetoed `stream_events(version="v3")` was an abandoned turn booking *less* — v3
booked 0 tokens where the driver books ~30, making "drop the connection just before the answer" a
free budget bypass. Detaching moves in the opposite direction: an abandoned turn now books *more*,
because it runs to completion and every token is metered per chunk exactly as before. The cost of
that honesty is stated rather than hidden — a chemist who closes the tab pays for the whole turn —
and it is bounded twice, by the loop cap (attached on every profile as of this pass) and by
`service_turn_timeout_seconds`, which keeps ticking inside the pump.

## Consequences

- A closed generator that was never advanced can no longer leak its lease: the pump starts the
  generator at handoff, so its `finally` always runs. The lease expiry in `_claim_turn_slot`
  remains as the backstop it always was.
- `resetSession` in the UI shrinks to the case the stop route cannot reach — a turn wedged on a
  *different* front-door replica, whose pump only that process holds. Stopping is process-local by
  construction; the client always calls the origin its stream was on, because the stream is how it
  knows a turn is running.
- Live *reattachment* to a detached stream — event sequence ids, a ring buffer, a
  `GET …?after=seq` — is deliberately not built. The transcript is the recovery, and the UI's
  poll delivers it; a streaming reattach is worth building when someone measures that watching the
  tail of a detached turn matters. `docs/planning/DEFERRED.md` holds that row.
- Two counters make the split observable: `chemclaw_turns_detached_total` and
  `chemclaw_turns_stopped_total`. A rising detach rate with a flat stop rate is a flaky network,
  not dissatisfaction with answers.
