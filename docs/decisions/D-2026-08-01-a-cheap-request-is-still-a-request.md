# D-2026-08-01-a-cheap-request-is-still-a-request — A cheap request is still a request, and a checked upload is still an ingested one

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** AG-15 (turn admission control),
D-144 (the token budget guard), D-152 (declared metric labels), SEC-5 (the pure-ASGI middleware rule)

## Context

The front door had two admission controls and both were scoped to the expensive path.
`service_max_concurrent_turns` caps turns in flight; the budget guard meters tokens per session.
Nothing counted **requests**.

So one authenticated caller could hold both of those at zero and still drive `GET /proposals`,
`GET /jobs`, `GET /schedules`, `GET /sessions` and the attachment route as fast as the network
allowed. Every one of them does real work — `/schedules` fans out to Temporal, `/jobs` and
`/proposals` query Postgres, `/readyz` sweeps the connector fleet — and a loop with no LLM call in
it was free. The guards were built around the model endpoint because that is the expensive
resource, which is right, and left the database and the broker behind nothing at all.

Separately, the attachment size cap was in the wrong place, and the mistake is easy to make
*because the check exists*: `parse_attachment` refuses anything over `attachment_max_bytes`. But it
runs in the route handler, and by the time a handler runs, Starlette's multipart parser has already
consumed the entire request body into a `SpooledTemporaryFile` — memory to 1 MB, then the pod's
ephemeral disk. A 5 GB upload was written out in full and *then* refused, and the route's own
`await file.read()` would have pulled whatever survived into RAM. The cap described what the parser
would accept; it never described what the process would ingest.

And below all of it, uvicorn ran with its defaults: no connection ceiling, no keep-alive timeout, no
bound on the request line and headers.

## Decision

**Three layers, each at the only level that can enforce it.**

| Layer | Bounds | Where it has to live |
|---|---|---|
| uvicorn flags | connections, idle keep-alives, header size | the launcher — the app never sees these |
| `_BodySizeLimit` | the request body | ASGI middleware, above body parsing |
| `enforce_request_budget` | requests per principal | after authentication, so it knows *who* |

**The rate limit is a token bucket, spent inside `require_principal`.** A fixed window lets a caller
spend a whole allowance in its last millisecond and the next in its first, so the observed peak is
twice the configured rate at the moment the system can least absorb it; a bucket has no edge to
align to, and its `burst` states plainly what a window only implies. It is called from
`require_principal` for the reason D-2026-07-31 put the proposal record inside `propose_note`: every
authenticated route already funnels through that one dependency, so one call there is a gate a new
route cannot forget, while a decorator on twenty routes is a gate the twenty-first silently skips.
`/healthz`, `/readyz` and `/metrics` do not depend on it and are therefore never limited — a
throttled probe reads as a down pod and a throttled scrape as a down target.

**`_BodySizeLimit` truncates rather than raises.** Raising inside the wrapped `receive` surfaces
wherever the body is being read, and FastAPI wraps *any* failure during body parsing in
`HTTPException(400, "There was an error parsing the body")` — so the caller would be told their JSON
was malformed when what actually happened is that it was too big. Instead the stream is truncated,
the app answers however it likes, and the wrapped `send` substitutes the 413 that is true. The
declared-`Content-Length` fast path stays beside the counting path even though the counting path
alone would refuse the request: it turns a client away *before* the transfer instead of after
`service_max_request_bytes` of it has crossed the network.

## Why not the alternatives

**Rate-limit in middleware, keyed on the bearer token.** Tempting because it would also protect the
JWKS-backed validation path. Rejected: two tokens for one user are two buckets, so the limit becomes
per-credential rather than per-person, and rotating a token resets it. The limit must mean "this
human", which is only knowable after validation.

**An app-level FastAPI dependency.** It would catch `/healthz`, `/readyz` and `/metrics`, and
carving them back out with a path exemption list is a second place for the policy to live.

**Keep only `parse_attachment`'s size check.** It is not redundant with the middleware — it is a
different check with a different shape and a different caller. The middleware bounds what the
*process* ingests and answers 413 (transport); `parse_attachment` bounds what an *attachment* may be
and answers 422 (data), and it has a second caller (the backfill CLI) that never passes through the
front door at all. Both stay.

**A fleet-wide limit.** Not something this process can implement. `maxReplicas: 6` multiplies the
real ceiling by six, exactly as it does for the admission cap, and the honest place for a
deployment-wide limit is the ingress. Stated here rather than papered over; the backlog row about
the autoscaler defeating the admission guard is the same finding from the other side.

**On by default.** Rejected for the same reason `budget_enabled` is off in code and on in the chart
(REV-16): a CLI, a test and a single-user dev run have no reason to be throttled, and a limiter that
fires in those contexts is one people switch off everywhere. The chart sets 120/min with a burst of
30 — far above a chemist clicking through a UI, far below what a script does.

**`--limit-concurrency` near the turn cap.** It bounds *connections*, not turns, and a connection
waiting for an admission permit or holding an SSE stream costs almost nothing. Setting it near
`service_max_concurrent_turns` would turn the transport backstop into the admission policy, refusing
at the socket what the queue exists to absorb. A test pins that it stays well above.

## Consequences

- A runaway client is refused with a 429 and a `Retry-After` instead of saturating Postgres and
  Temporal through routes nothing metered.
- An oversized upload is refused before it is transferred, rather than after it is spooled to the
  pod's disk.
- Two new counters, both unlabelled: a per-principal label would key a metric on user identity, and
  `/metrics` is unauthenticated (D-152's allowlist is what says so). The rate is what an operator
  alerts on; who hit it is in the log.
- The bucket map is an LRU with a configured cap. That bound is not incidental — the key is
  attacker-influenced, since minting tokens for many `oid`s is precisely the way around a
  per-principal limit, so an unbounded map would make the limiter the first thing to fail. This
  codebase has fixed unbounded identity-keyed maps three times; this one is bounded at birth.

## Not in this change

A fleet-wide request ceiling, and the autoscaler row it belongs with. Tracing spans, structured
logging and redaction — the two remaining `[M]` rows in the same backlog section — are about seeing
what happened, not about bounding it.
