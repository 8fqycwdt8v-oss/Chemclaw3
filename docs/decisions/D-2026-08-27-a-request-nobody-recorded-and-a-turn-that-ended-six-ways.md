# D-2026-08-27-a-request-nobody-recorded-and-a-turn-that-ended-six-ways — the front door keeps the record it was already computing

## Status

Accepted, 2026-08-27. Scope: `src/chemclaw/api/`, `src/chemclaw/core/asgi.py`,
`infra/sql/060_turn_outcome.sql`, and the `turn_costs` shape in `src/chemclaw/core/turn_cost.py`.

## Context

An observability review of the front door found the same shape in nine places: the system *knew*
something and kept no record of it. Each was measured rather than inferred.

- **No first-party access log.** The only record of an HTTP request was uvicorn's — client address,
  method, raw path, status. No latency, no route template, no actor, no session, no correlation id
  (all three rendered `-`, because nothing outside `run_turn` ever stamped them), no bytes. And no
  HTTP metric of any kind, so "p95 on `/jobs`", "which route is 5xx-ing" and "is this the model or
  the database" were unanswerable from outside the process.
- **22 of 23 routes returned no correlation id**, so a chemist had nothing to quote in a bug report
  anywhere except on an SSE error event.
- **A 422 emitted zero log records** and moved no metric: a client looping on a malformed body was
  indistinguishable from silence.
- **Every authorization refusal was silent.** Five `HTTPException(404)` raises in `api/deps.py`,
  no log, no metric. 404-not-403 is correct and is exactly what makes the server-side record the
  only place the distinction can survive — so a session-id enumeration scan looked like ordinary
  404 traffic.
- **A missing bearer token logged nothing** while an invalid one logged, so "a client is
  misconfigured and sending no header" and "a healthy service" produced the same evidence.
- **A healthy turn produced no log line at all** (`grep -c logger.info api/runner.py` → 0), and
  `turn_costs.completed` is one boolean over what are really six distinct endings.
- **Nothing measured time-to-first-token**, which is the latency a chemist actually experiences.
- **`chemclaw_turns_failed_total` counted error *events*, not turns**, and `runner.py` can yield two
  for one turn (the loop cap and the empty answer are independent predicates, and a runaway turn
  satisfies both) — so a "failure rate" could exceed 1.0. The `TimeoutError` handler sits outside
  the `async for`, so timeouts counted as failures **zero** times: an all-timeout deployment showed
  a zero failure ratio.
- **`chemclaw.turn` ended before the turn did.** The span was pushed onto the `AsyncExitStack`
  under a comment claiming "the span's lifetime is exactly the turn's teardown"; that stack closes
  when the model stream is exhausted, so the guards, the plan-approval read, `build_answer_event`
  — which under `verifier_enabled` makes a **second LLM call** — the transcript write, the audit
  flush and the `yield` a disconnect lands on all ran outside it.

And one live defect: **`DetachableTurn` could lose its end-of-stream marker and hang the reader
forever.** `_pump` blocks on `await put` once the bounded queue fills, so its last blocking put
returns with the queue full again, and the `finally` one line later offers `_DONE` through
`put_nowait` — dropped. Reproduced at 256 and 512 events with a reader momentarily behind, which is
an ordinary token-streamed answer to a slightly slow client. Nothing sends on such a connection, so
the SSE send timeout never fires and the 15 s ping keeps succeeding: the stream holds a slot against
`--limit-concurrency` for the pod's lifetime, silently.

## Decisions

**1. End-of-stream is a fact about the pump, not a message in the queue.** `_next_event` races
`queue.get()` against the pump task and stops when the task is done and the queue is drained.
`_DONE` stays as the fast path — it costs one comparison and covers every healthy stream — but it
is no longer the *only* path, so dropping it under backpressure is survivable rather than fatal.

**2. The request record is a pure-ASGI middleware, installed innermost.** Pure ASGI and never
`BaseHTTPMiddleware`, which re-tasks the body and turned every cancelled SSE stream into a spurious
500 (44 measured in one 50-user run). Installed *first* in `create_app`, which under Starlette's
`insert(0)` makes it the innermost user middleware: inside `_SecurityHeaders`, so the 500 it answers
carries them — Starlette's own `ServerErrorMiddleware` sits above every user middleware, which is
why the default 500 had neither headers nor an id — and outside `ExceptionMiddleware`, so the 401s,
404s, 422s and 429s the handlers produce are recorded as the responses they are. The 413 from
`BodySizeLimit` is deliberately outside it and is logged where it is refused instead.

**3. `route` is the route template, never the raw path — and the bound is asserted, not asserted
about.** The raw path is attacker-controlled: a cardinality bomb, and a redaction cost measured at a
21 s pod stall from a 115 KB request line reaching the filter unauthenticated. `_MAX_SERIES_PER_COUNTER`
is 64. Measured across 158 front-door tests the counter grew **35** series and no route produced
more than three status classes, so the worst case is 21 labels × 3 = **63** — safe today and one
route away from not being. `tests/test_api_observability.py` asserts that arithmetic, so the route
that would start silently dropping series turns a test red instead.

**4. One turn, one correlation id, and it is the request's.** `run_turn` minted its own
unconditionally, so the id now returned on every response and the id keying `turn_costs`,
`audit_events` and `session_messages` were two different strings for one event. The pump task copies
the request's context at creation, so the ambient id inside the turn *is* the request's; `run_turn`
adopts it and mints only where there is none (the CLI, a test).

**5. The turn's outcome is a closed six-value enum with exactly one producer.** `answered` /
`loop_capped` / `empty_answer` / `errored` / `timed_out` / `abandoned`, settled in
`_settle_outcome` and written by `_book_turn_spend`, which is the one function that runs on every
path a turn can take — including the disconnect path, which is the one any second site would forget.
`completed` stays and is *derived* (`outcome == "answered"`), so what already reads it is unchanged.

**Not nine values.** A turn refused for budget, shed by admission, or 409'd by a concurrent turn
never reaches `run_turn`: nothing was spent, so there is no cost row for them to be the outcome
*of*, each already has its own counter, and all three happen *before*
`chemclaw_turns_started_total` — so adding them would publish a second answer to a question already
answered and break the pairing an operator reads that counter against.

**`timed_out` is decided exactly, not by tolerance.** The wall-clock kill and an explicit Stop both
arrive inside `run_turn` as one `CancelledError`, and the route learns which it was only in its own
`except TimeoutError` — which runs *after* the cost row is booked, so it cannot tell the turn
afterwards. So the route passes the deadline *in* (`asyncio.timeout(...) as t` → `t.when()`) and the
comparison reads the same event-loop clock the timeout schedules itself on.

**6. The turn record is written twice, on purpose.** A row in `turn_costs` (which needs Postgres)
and a `turn.started`/`turn.finished` pair through `log_event` (which does not), so a deployment with
no ledger still has the record. `chemclaw_turns_finished_total{outcome}` is the aggregate.

**7. The turn span wraps the turn's body.** Measured against `HEAD` with an in-memory exporter and a
span opened where the judge call runs: **before**, a different trace id and no parent — one orphan
root trace per turn with the shipped chart's `OTEL_LLM_SPANS=true`, and a traced duration that
understated the chemist's wait by the length of the judge call. **After**, one trace, the judge a
child of `chemclaw.turn`. The span also carries `correlation.id` and `actor`, the join key being the
one attribute that was absent.

**8. Count the turn, not its events.** `chemclaw_turns_failed_total` is a flag set wherever a turn
fails and incremented once in the `finally`, which is also what brings the timeout branch inside the
count.

## Consequences

- `chemclaw_event_stream_send_timeouts_total` is **not** declared, so the push-back stream's new
  send-timeout closure is logged and not counted. Reusing `chemclaw_turn_send_timeouts_total` is
  refused by that declaration's own comment (two populations, one denominator). It belongs in
  `core/metrics.py`.
- `turn_costs` has no `model_calls` or `compactions` column. Both were wanted and neither has a
  producer this layer can reach — the loop-cap watch records only a boolean and the compaction
  middleware only a counter — and a column nothing can write is not an attribution
  (`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`).
- `deploy/entrypoint.sh` should pass `--no-access-log` now that a first-party record exists;
  uvicorn's line carries strictly less and puts an attacker-controlled request line through the
  redaction filter.
- `_MAX_SERIES_PER_COUNTER` will need raising to 128 the next time a route is added.
