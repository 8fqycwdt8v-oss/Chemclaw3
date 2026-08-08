# D-2026-08-08-a-slot-lives-as-long-as-its-response — a slot lives as long as its response, and a check that runs before the queue checks nothing

**Status:** accepted

## Context

Lane T7 of the 2026-08-08 hardening campaign: the front door's own robustness. Five findings, four
about *admission accounting that is off by the amount of concurrency in the system*, one about a
handler that reads a body FastAPI cannot see. Every one of them was reproduced by execution first;
one of the five turned out to be aimed at the wrong file (below).

**1. A push-back stream's per-user slot leaked permanently.** `GET /sessions/{id}/events` counts
open streams per user (`service_max_event_streams_per_user`, default 5) and released the slot in
the body generator's `finally`. sse-starlette starts that generator only *after* writing
`http.response.start`; if the client vanishes while that write is still in flight, its disconnect
listener cancels the task group before the first `__anext__`. **A never-started async generator
runs no `finally` at all**, so nothing released. Measured, driving the real ASGI contract:

```
stalling send + disconnect -> sent=['http.response.start'] body_iterator_advanced=0
                              event_streams={'oid-alice': 1}      (control: {})
5 x the same          -> event_streams={'oid-alice': 5}
6th, honest connect   -> HTTP 429 "too many concurrent event streams; close one and retry"
```

with nothing open to close, for that user on that pod, for the pod's lifetime. It survived
`gc.collect()` — closing an unstarted generator is a no-op.

**2. The token/turn budget overshot by the concurrent-request count.** `budget.check` ran at
request entry, before the admission permit, and never again. `BudgetTracker`'s own docstring
promised an overshoot "up to `service_max_concurrent_turns`" — a claim about a call site, and the
call site made it false. Measured with production-shaped values (8 permits, 40 concurrent POSTs on
40 sessions of one user, a **one-turn** cap): `{'answer': 40, '429': 0}`, `turns=40, tokens=40000`.
Documented bound 8, measured 40. Posting sequentially bound correctly, so it was purely ordering.

**3. `pypdf` 6.14.2 carried two CVEs reachable from the authenticated upload route.**
CVE-2026-71852 and CVE-2026-71870, fixed in 6.15.0. Reachability and cost measured in-process on a
crafted one-page PDF whose font declares a single `/ToUnicode` `bfrange` line with a 200 KB
destination string:

```
pypdf 6.14.2: input 201 KB -> 33.83 s, peak RSS 35 MB -> 1948 MB | parsed, 50000 chars
pypdf 6.15.0: input 201 KB ->  0.00 s, peak RSS 36 MB ->   36 MB | LimitReachedError:
              Maximum /ToUnicode string length exceeded: 200000 > 1024.
```

`MAPPING_DICTIONARY_SIZE_LIMIT = 100_000` did not stop it because it bounds the *number* of map
entries, never the size of each one; 6.15.0 adds the per-token length check that does.

**4. Attachment parsing ran on the event loop, which is the more general defect.** Independent of
any CVE: `upload_attachment` is `async def` and called `parse_attachment` inline. `Settings` pins
uvicorn to **one** worker, so the 33.8 s above — or any decompression-bomb DOCX/XLSX/PPTX inside
the 2 MB cap — froze every session, every SSE stream and every health probe on the pod. Nothing
bounded it: `service_max_concurrent_turns` meters LLM turns, `BodySizeLimit` meters bytes, and
neither meters parse cost.

**5. A malformed webhook body was a 500.** `POST /events/knowledge-merged` reads the raw body (the
signature covers bytes) and called `model_validate_json` inside the handler — the one place
FastAPI's request-validation layer cannot see — so a `ValidationError` escaped unhandled.
Repeatable at will by anyone who can sign a body, which also makes it a way to move the 5xx rate
operators alert on.

## Decision

### The stream slot is held by the response, not by the generator

`_SlotBoundEventStream` subclasses `EventSourceResponse` and releases the slot in its `__call__`'s
`finally`. Starlette awaits a response exactly once per request, so that block runs on a completed
stream, on an exception, and on cancellation alike — including the window where the generator never
started. The generator's own `finally` is **deleted**, not kept as a second guard: keeping both
would decrement twice for one stream, and the site being kept is the one that does not run.

**It is deliberately not a lease**, which is the shape `api/state._claim_turn_slot` uses for the
turn slot's identical window and the shape this lane was handed. It does not transfer, and the
reason is a property of the resource: a turn has a widest wall clock (admission wait + turn
timeout), so an expired entry provably belongs to no live turn — while a push-back stream is
*unbounded by design*, polling until its client leaves. Any deadline short enough to clear a leak
would also evict a healthy long-lived stream's accounting and let one user exceed the very cap the
ledger exists to enforce. Response scope is exact where a deadline can only be a guess.

Residual, stated rather than hidden: if the request task is cancelled between the handler returning
the response and Starlette awaiting it, nothing runs. There is no `await` in that gap on this path,
and the process is being torn down in the cases that produce it.

### The budget is checked again after the permit

`_turn_events` re-runs `budget.check` immediately after acquiring the admission permit. A turn
holding a permit is one of at most `service_max_concurrent_turns`, and every turn that finished
ahead of it has already been booked by `record` — which is exactly what makes the documented bound
true. The entry check stays as a fast path: an already-exhausted session gets a clean 429 without
queueing for a permit it will not be allowed to use.

The second refusal is an **error event, not a status code** (D-166): the response is open by the
time it fires, and the shed branch three lines above answers the same way for the same reason. It
carries `code="budget_exhausted"`, `retryable=False` — the budget is spent, so an immediate retry
fails identically. `BudgetTracker`'s docstring now says where its bound is enforced instead of
merely asserting it.

### `pypdf>=6.15.0`, with the behaviour pinned by a test

The pin is a floor; the test is what fails if a future resolution walks back under it.
`test_a_font_map_that_expands_far_past_its_size_is_refused` builds the crafted PDF above and asserts
the refusal. `parse_document`'s boundary net (D-2026-08-07-one-bad-file-must-not-stop-the-corpus)
already turns the library's `LimitReachedError` into the same 422-shaped `AttachmentError` every
other unreadable file gets, so no route change was needed for it.

### Uploads are parsed in a bounded worker thread, and shed rather than queued

`parse_attachment_off_loop` runs `parse_attachment` through `run_in_executor` under two config
bounds: `attachment_max_concurrent_parses` (2) and `attachment_parse_timeout_seconds` (30).

The concurrency cap is the load-bearing half, and it is a *cap on the default executor's
occupancy*: that pool is where `chemclaw.api.auth` validates every bearer token, so uploads allowed
to fill it would move the outage one layer out rather than fix it. Past the cap an upload is shed
with a retryable **503** — never queued, the same discipline the turn admission uses. A queue would
let an attacker accumulate work that keeps burning CPU long after every requester has gone.

Two details are the difference between a cap and a decoration:

- The slot is released by the future's **done callback**, not by the awaiting request. A request
  whose parse timed out has stopped waiting; Python cannot stop its thread. Releasing on timeout
  would let one attacker hold every CPU while the counter read zero. `asyncio.shield` is what keeps
  `wait_for`'s cancellation off that future.
- A timeout is a 422 (a statement about the file — sending it again does the same thing), while a
  full pod is a 503 (a statement about the moment). Two meanings, two types, two codes.

Residual, measured and stated: **the timeout bounds the wait, not the thread.** A parse past the
limit runs to completion against the cap. That is the honest ceiling of an in-process fix; the
CVE bump is what removes the known way to reach it, and a killable subprocess is the thing that
would remove the rest (BACKLOG).

### The webhook answers 422 for a body it cannot parse

`ValidationError` becomes an `HTTPException(422)` naming the failing locations. The signature gate
is untouched and still runs first — an unsigned malformed body is still a 401 — and the detail
carries `loc` and `msg` only, never `errors()` whole, which would echo a 2 MB malformed body back
into the response and the access log.

## Consequences

- Nine new tests; **six of them fail against the unfixed code**, which is what makes them
  discriminating rather than merely green. Each counterfactual was run, not asserted: the inline
  parse restored (3 fail), the post-permit check deleted (10 answers against a one-turn cap), the
  old pypdf on `PYTHONPATH` (the bomb parses), the pre-fix stream release (one leaked slot per
  vanished client).
- One behaviour change a client can see: a turn refused by budget *after* admission ends its stream
  with an error event instead of running. Callers already handle `error`; nothing previously
  produced this event at this point because nothing refused there at all.
- Two new config fields with `.env.example` rows. `service_uvicorn_workers` stays 1 — this changes
  what one worker survives, not how many there are.

## Alternatives rejected

**Release the stream slot from `EventSourceResponse(background=...)`.** Fewer lines and uses a
library feature. It runs after the task group exits *cleanly*, which covers the disconnect case —
and not the case where the first `send` raises (a broken pipe), where the exception propagates out
of `__call__` and the background task is skipped. A `finally` around `super().__call__` covers both.

**Give the stream slot an expiring lease, mirroring the turn slot.** The instruction this lane was
given. Rejected on measurement of the resource, not on taste: see above.

**Move the budget check to `record`-time or make the tracker itself reserve.** A reservation would
make the tracker exact rather than best-effort, which is a real improvement — and it needs an
answer for a turn that dies without recording, i.e. the same lease machinery, for a guard whose
documented contract is best-effort and in-process. Re-checking at the permit makes the *existing*
promise true for two lines.

**A dedicated `ThreadPoolExecutor` for parsing.** Bounds the threads, and leaves an unbounded queue
of abandoned work behind them — the amplification the shed answer exists to prevent. The counter
bounds occupancy of the shared pool directly, which is the property that actually matters.

**Refuse the upload timeout as a 504.** A gateway timeout says an upstream was slow. Nothing here
is upstream; the file is what could not be read.

## Refuted

**"Apply the same offload to `ingest/documents/crawl.py`."** `crawl.py` opens no files — it walks
directory entries and stats them, which is the whole point of its cost model. The share's parse is
`sync.py:200`, and it has run under `asyncio.to_thread` since it was written. The crawler worker
cannot be wedged by a hostile document the way the front door could be; what it lacks is a
*timeout*, which is a throughput concern in a background activity rather than an availability one,
and which is recorded in `BACKLOG.md` rather than changed from this lane (that file belongs to
another lane's edit in the same campaign).
