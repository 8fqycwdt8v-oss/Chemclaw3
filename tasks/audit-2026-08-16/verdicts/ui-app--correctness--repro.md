# ui-app — CORRECTNESS — adversarial re-derivation (lens: does it actually reproduce?)

In scope: the one **high** finding. The other three are **medium** and were not examined.
Every number below is mine, from my own harnesses; the reporter's scripts were not run.

---

## The job push-back stream reconnects in an unthrottled tight loop when the response body ends

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. The cited code is real and current.** `git log -1` = `1a1f6f0`, tree clean.
`src/hooks/useJobStreams.ts` is 183 lines; `openStream` spans :88–166, `attempt = 0` is at :131,
`if (done) break;` at :140, the inner loop closes at :156, the `catch` at :160. All as quoted.

**2. Mechanism — reproduced, from my own harness, and it is worse than reported.**
I wrote `tests/audit_repro/repro.test.tsx` (deleted after the run): the real `useJobStreams`
rendered inside the real `AuthGate`, real `useChatStore`, `fetch` replaced with a handler that
answers `/events` and a **hard cap of 5000** so vitest could finish. Real wall clock, no fake timers.

```
[EVENT-STREAM EOF] fetches=5001 in 630ms      <- 200 text/event-stream, body closes with 0 bytes
[JSON 200]         fetches=5001 in 454ms      <- 200 application/json, "[]"
[500 CONTROL]      fetches=1    in 302ms      <- 500: exactly one request. backoff works.
```

The 500 control is what makes this a mechanism proof rather than a harness artifact: the same
harness, the same window, one request instead of five thousand. The spin is specific to the
`res.ok && res.body` + clean-EOF path, exactly as the finding says.

Steady-state rate (`rate.test.tsx`, 1000 ms window, first 100 ms discarded):

```
[N=1] total fetches in 1000ms = 20001; steady-state ~21108 req/s
[N=3] total fetches in 1000ms = 20003; steady-state ~19722 req/s
```

That is stubbed-fetch, so I re-measured over a **real socket** — a `node:http` server that answers
`200 text/event-stream` + `flushHeaders()` + `res.end()`, driven by the same loop shape over
loopback:

```
CLIENT: 4037 connections in 3s = 1346 req/s over a real socket (1 stream)
server side: req/s = 1008 / 1535 / 1495
```

So ~1,350 req/s per stream on loopback, ~4,000 req/s for the `MAX_JOB_STREAMS = 3` budget.
The reporter's "50 requests in 250 ms → roughly 600 requests/second" is a floor produced by their
own cap of 50, not a rate; the true figure is ~7x higher. Their qualitative claim — unthrottled,
no `backoff()`, no `attempt++`, unbounded — is exactly right.

Two supporting claims also check out. `streamTurn.ts:59-64` does carry the content-type guard
`openStream` lacks, verbatim as cited. And no `429` is produced on this path, so `consecutive429`
and `setJobStreamsThrottled` genuinely cannot intervene.

**3. Reachability — this is where the finding breaks.** The finding's primary trigger is:
*"The backend reaches this state on its own … a Postgres outage or an exhausted pool terminates
every stream at connect time."* I ran that exact scenario against the **real front door**
(`chemclaw.api.routes.streams.session_events`) under uvicorn over real HTTP, with
`chemclaw.api.app.stream_new_events` replaced by a generator that raises `RuntimeError("connection
pool exhausted")` before its first yield — the precise failure the route's docstring names:

```
$ curl -sS -i --max-time 10 -N -H "accept: text/event-stream" \
    "http://127.0.0.1:8791/sessions/$SID/events"
curl: (18) transfer closed with outstanding read data remaining
HTTP/1.1 200 OK
content-type: text/event-stream; charset=utf-8
Transfer-Encoding: chunked
```

`curl: (18)` is the point. The body is **not** cleanly terminated — the chunked stream is cut, no
terminating 0-chunk. I then measured what that means to `fetch`, against three servers:

```
A abrupt-destroy: status=200 ct=text/event-stream -> THROW: TypeError: terminated / other side closed
B clean-end:      status=200 ct=text/event-stream -> DONE (clean EOF)
C json-200:       status=200 ct=application/json  -> DONE (clean EOF)
```

An abrupt close **rejects** `reader.read()`. That rejection is not caught by the inner
`try { … } finally { reader.cancel() }` at :137-159 — it propagates to the outer `catch` at :160,
which does `attempt += 1; await backoff(attempt, …)`. **The backend's own failure mode takes the
correct backoff path.**

Every other backend-originated ending I could produce does the same:

- tailer raises **mid-stream**, after one delivered event → abrupt (server logs
  `ASGI callable returned without completing response.`).
- **SIGTERM to uvicorn** (a rolling deploy) with a healthy silent stream open:
  `RESULT: THROW (terminated / UND_ERR_SOCKET) -> UI takes the backoff path`.
- `stream_new_events` with the service's `max_polls=None` is `while True` and cannot return, so
  sse-starlette never gets to write a clean end-of-body for this route.

**4. The production topology makes it further away, not closer.** The browser talks to the BFF
(`server/proxy.ts`), not to FastAPI. I ran the real `proxy()` between a fake backend and a real
`fetch` client:

```
upstream=abrupt            -> client: HANG after 2501ms
upstream=bytes-then-abrupt -> client: HANG after 2500ms
upstream=clean             -> client: DONE after 12ms
upstream=bytes-then-clean   -> client: DONE after 20ms
```

Through the BFF an abrupt upstream close does not even surface as an error — it **hangs**.
`upstreamRes.pipe(res)` has no error path that ends or destroys `res` (the only `upstreamRes`
error handler, in `attachHeartbeat`, just does `clearInterval`), so the browser is left holding an
open, permanently silent 200. That is a real defect on the same line of code, but it is the
*opposite* of the reported one: notifications stop silently, no requests are issued.

I also closed the two obvious in-family sources of a non-stream 200:
- the BFF answers an un-whitelisted `/api/…` with `404 {"detail":"not found"}` (`server/index.ts:71-76`), not a 200;
- `/workspace/chemclaw3_mock` has no `/events` route at all → 404 → backoff.

### Why

The mechanism is genuine, the code is exactly as quoted, and the spin is more violent than
reported (~1,350 req/s per stream over a real socket, vs. the claimed ~200). The `500` control
proves the backoff path is intact and that the defect is specific to the `ok` + clean-EOF branch.
The finding's fix is the right fix.

What does not survive is the **trigger**, which is the load-bearing half of a "high": the finding
asserts that this system produces the condition by itself, and cites the tailer's docstring as the
evidence. I ran that exact failure against the real route and the wire says otherwise — a raised
tailer, a mid-stream failure and a SIGTERM all yield an *abrupt* close, which `fetch` turns into a
rejected read and `openStream` handles correctly at :160. A docstring sentence ("a connection
failure ends the stream") was read as "ends the body cleanly"; measured, it does not. With that
gone, the "600 req/s against a service that is already unhealthy" story does not occur: in the
unhealthy case the client backs off (direct) or hangs (through the BFF).

What is left is the finding's *secondary* trigger — a cleanly-terminated or non-event-stream 200
arriving from something between the BFF and the backend. `openStream` genuinely lacks the guard
`streamTurn` has, and `streamTurn`'s own comment ("Nearly always means something between us and
the service swallowed the stream") says the team has met that condition — so this is plausible,
but I could not produce it from any component in this system, and the burden is on the finding.

One caveat I could not settle and that argues against grading this lower than medium: in the real
deployment the browser↔ingress hop is likely HTTP/2. Whether an OpenShift/HAProxy router forwards
a backend abrupt-close as `RST_STREAM` (→ throw → backoff) or as `END_STREAM` (→ clean EOF → the
spin) decides reachability, and I have no h2 ingress here to measure it against. If someone wants
to promote this back to high, that is the single measurement that would do it.

Medium: a real, unbounded, self-inflicted-DoS defect with a demonstrated mechanism, a one-line
fix, and no trigger anyone has shown this system to produce.
