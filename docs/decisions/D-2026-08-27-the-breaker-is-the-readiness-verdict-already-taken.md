# D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken — a connector known to be down is not dialled again this window

## Status

Accepted. Found in the blind-spot audit (BS-18) and fixed in the same pass.

## Context

`connectors.transport.HeldConnectorSession.__aenter__` opens one connector for one turn, bounded by
`connector_open_timeout_seconds` (15 s). A connector that will not open is absorbed — the turn
proceeds without its tools, the name comes back in `unreachable`, and the next turn tries again.
That degradation is right and is not in question here.

What is in question is the price of rediscovering it. **Every turn paid the full open bound against
a host already known to be down, with no backoff, for the whole duration of an outage.** Six
bundles open concurrently, so the wall clock is the slowest rather than the sum — but 15 s in front
of the first token, on every turn, for as long as a sidecar is dead, is the shape of an outage
amplifier rather than a graceful degradation.

**And the answer already existed.** `connectors.health.probe_connectors` sweeps every enabled
bundle's `/healthz` at startup and again on every `/readyz`, which the kubelet runs every ten
seconds per pod. Its verdict feeds three consumers — the readiness body's count, the
`chemclaw_connectors_unhealthy` gauge and the `connectors_required` fail-fast check — and the open
path was not one of them. The two halves could not even see each other: the snapshot lived on
`app.state` in the front door (`api/state.py::connector_health`), which is a structure
`chemclaw.connectors` may not import.

So this is not a missing mechanism. It is a producer with no reader.

## Decision

**`chemclaw/connectors/reachability.py` is the reader's state, and it is deliberately not a circuit
breaker framework.** It holds one dict — connector name to `(monotonic timestamp, reachable)` —
plus a recorder, a predicate and a `forget_…` for tests. That is the whole module. This codebase's
rule is no abstraction without a second real caller, and a general breaker would be an abstraction
with none: the one thing being protected is a connect, and the state it needs was already being
produced.

- `connectors.health._probe` records every verdict it reaches, healthy and unreachable alike.
- `HeldConnectorSession.__aenter__` consults `recently_unreachable` before creating its holder task
  and, if the answer is yes, **skips the dial entirely** rather than shrinking its timeout. A
  shrunk timeout still pays a TCP handshake attempt per connector per turn against a dark fleet and
  still has to pick a second number nobody can derive; skipping is the honest form of "we already
  know".
- The open records its own outcome either way, which is what gives a process with no readiness
  route (the CLI, a template activity on a worker) a verdict at all — and it sees something
  `/healthz` cannot: a server that accepts the socket and fails the MCP handshake.

**Its own module, not a function in `health.py`.** `connectors.health` imports
`connectors.registry`, which imports `connectors.transport`; a reader placed in `health` would
close that cycle. Nothing in `reachability.py` imports from this package, so both sides can.

**Process-local.** The timeout being saved is paid per process, and every process can observe the
fact for itself in one round trip — so a shared store would add an invalidation and a failure mode
of its own to distribute something nobody else needs.

**Recovery is the half that must not be omitted, and it has two independent paths.** A breaker with
no way back keeps a recovered connector out of the fleet for as long as nothing dials it, which is
strictly worse than the problem it was added for.

1. **The readiness sweep readmits it.** It runs on the kubelet's timer regardless of what turns are
   doing, records `healthy`, and last writer wins — so a recovered connector is dialled on the very
   next turn, within one probe interval plus `service_readiness_cache_seconds`.
2. **The verdict expires.** `connector_breaker_window_seconds` (default 30 s) is how long an
   "unreachable" is trusted at all. Past it the next turn dials for real and records what it finds,
   with no probe involved. This is the path a process with no readiness route takes, and it is why
   the number is chosen as a **recovery bound rather than a savings one**: a longer window saves
   more and takes longer to forgive.

`0` disables the breaker outright and restores the behaviour before this existed, which is what a
deployment sets if it would rather pay the timeout than reason about staleness.

**What a turn is told does not change.** A skipped connector is still `connected == False`, so it
still lands in `open_connector_specs`'s `unreachable` list, still increments
`chemclaw_connectors_unreachable_total`, still produces the `CapabilityDegradedEvent` a chemist
sees. What a chemist and an operator must be told cannot depend on how we found out. The log line
*is* different — `absorb_connect_failure` would have reported a `TimeoutError` that never happened
— so the skip logs its own sentence naming the window.

## Consequences

- A dark connector costs one process one open timeout per `connector_breaker_window_seconds`
  instead of one per turn. In a cluster it usually costs *none*, because the startup probe or a
  `/readyz` sweep reaches the verdict before any turn does.
- A connector whose `/healthz` answers while its `/mcp` does not is dialled once per window rather
  than every turn — the sweep keeps recording `healthy`, the open keeps recording `unreachable`,
  and the two alternate. That is the correct outcome: the MCP surface is the thing that matters and
  only the open can test it.
- A connector with no `health_url` (`unprobed`) is never recorded by the sweep, so its only
  verdicts come from opens and its only recovery path is the window expiring. Named here rather
  than special-cased, because it is the designed behaviour of the `unprobed` state.
- The memory is per process, so a fleet of front-door pods each learns independently. That is the
  same shape as every other per-process guard here and needs no coordination.

## Evidence

`tests/test_connector_breaker.py`, against a real dark address with `create_session` spied rather
than stubbed, so the failure being reacted to is a real one:

- two consecutive opens dial **once**, and both turns still report the connector unreachable;
- a verdict older than the window dials again;
- a window of `0` dials every time;
- a readiness sweep that finds `/healthz` healthy readmits a connector the open path had blocked;
- a readiness sweep that finds it unreachable spares the *first* turn its open timeout — the half
  of the saving the open path cannot produce for itself.

The first and last of those fail on the unfixed tree; the middle three are the recovery and
opt-out guards.
