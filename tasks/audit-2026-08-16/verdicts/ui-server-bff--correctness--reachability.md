# ui-server-bff — CORRECTNESS: reachability/consequence verdicts (round 1)

Both in-scope findings were re-run against the **real** `server/index.ts`
(`node --experimental-strip-types`, Node v22.22.2) on a clean checkout of
`/workspace/chemclaw3_ui` @ `1a1f6f0` (`git status --porcelain` empty, no `MUTANT` markers in
`server/` or `shared/`). Purpose-built upstreams on loopback; scripts under `/tmp/bffv/`.

---

## A lost upstream connection leaves the browser's response open forever

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

BFF on :9102 → stub upstream on :9197 (`/tmp/bffv/upstream_kill.mjs`): SSE route writes one CRLF
frame then `res.socket.destroy()` after 300 ms; `/jobs` promises `content-length: 100`, writes 10
bytes, then destroys. Client is a plain `fetch()` reader (`/tmp/bffv/client_read.mjs`).

```
$ node client_read.mjs http://127.0.0.1:9102/api/sessions/aaaa…aaaa/events
+90ms status= 200 ct= text/event-stream cl= null
+92ms chunk "event: token\r\ndata: {\"type\":\"token\",\"text\":\"partial\"}\r\n\r\n"
+8091ms STILL READING — giving up

$ node client_read.mjs http://127.0.0.1:9102/api/jobs
+79ms status= 200 ct= application/json cl= 100
+80ms chunk "{\"items\":["
+8079ms STILL READING — giving up
```

BFF log after both: the three boot lines only. No `upstream error for …` line, i.e.
`upstreamReq.on('error')` (proxy.ts:179) did not run.

**Longer run, to kill the "some timer eventually saves it" objection.** The same JSON case held
open past `server.keepAliveTimeout` (120 s) and `server.headersTimeout` (125 s):

```
$ node client_long.mjs http://127.0.0.1:9102/api/jobs 150
+0.1s status= 200 cl= 100
+0.1s chunk "{\"items\":["
+150.1s STILL READING after 150s — giving up
```

**Mechanism, isolated** (`/tmp/bffv/mech.mjs` — bare `http.request` with the same agent options,
listeners on every event of both objects):

```
response headers received, cl= 100
res data "{\"items\":["
res ABORTED
REQ CLOSE
res ERROR: ECONNRESET aborted
res CLOSE
```

The `ClientRequest` gets `close` and **never** `error`; the `IncomingMessage` gets `aborted` +
`error`. Exactly as the finding states.

**Graceful FIN, not just RST** — the rolling-deploy shape, which the finding names but did not
show. Second upstream (`/tmp/bffv/upstream_fin.mjs`) calls `res.socket.end()` instead of
`.destroy()`; BFF on :9107:

```
+91ms status= 200 ct= text/event-stream cl= null
+93ms chunk "event: token\r\ndata: …\r\n\r\n"
+8091ms STILL READING — giving up
```

Same hang. So a `kubectl rollout restart` / pod SIGTERM of the backend — not just a crash —
produces it.

### Why

Reachability is not merely "possible", it is **routine**: any backend deploy, restart, OOM-kill or
mesh idle-timeout while a turn or a job stream is open. It needs no crafted request at all, only a
normally-shaped one that is in flight at the wrong moment. Nothing upstream stands in the way: the
route whitelist is irrelevant (this is a whitelisted route behaving normally), and there is no
timer left in the process — `requestTimeout = 0` (index.ts), `upstreamReq.setTimeout(0)` and
`agent.timeout: 0` (proxy.ts), measured above at 150 s.

Consequence is as claimed, and I could not soften it:
- The response never ends, never errors, is never truncated in a way `fetch()` reports, and is not
  logged.
- No SPA-side rescue exists. `src/api/streamTurn.ts` has no timeout — its only abort path is the
  user pressing Stop (`opts.signal`). So the turn genuinely sits on "thinking" forever.
- **One consequence the reporter missed, which makes it worse.** `src/hooks/useJobStreams.ts`
  reconnects in a `while (!controller.signal.aborted)` loop that is driven by the stream *ending*.
  A stream that hangs instead of ending never re-enters that loop, so the tab's job push-back
  channel is permanently dead — silently, and for all three watched sessions — until the user
  reloads. That is the notification path for multi-day DFT runs.
- One thing that does *not* hold and is worth recording: the two findings do not compound. When the
  upstream socket dies, the agent socket is released, so a hung downstream response does not also
  consume one of the 128 sockets in the next finding.

Also worth naming for whoever fixes it: the intended behaviour is already written. The
`upstreamReq.on('error')` handler's first branch is `if (res.headersSent) { res.destroy(); return; }`
— precisely the fix the finding proposes. It is simply wired to the object that never emits the
event.

---

## 128 concurrent SSE streams wedge every other request through the BFF, forever

- **Verdict**: CONFIRMED — with one factual correction to "forever"/"never dispatched", and the
  reachability threshold is **lower** (worse) than the finding states
- **Severity I would assign**: high

### What I did

Silent upstream (`/tmp/bffv/upstream_silent.mjs`, `maxConnections` raised so it is not the binding
constraint) + a fresh BFF per trial so no pooled state carries over. `ulimit -n` = 20000, so file
descriptors are not the limit either. `/tmp/bffv/saturate.mjs` opens N `/api/sessions/<sid>/events`
streams, then one ordinary `GET /api/jobs`.

```
$ node saturate.mjs 9104 127
127/127 SSE streams open through the BFF
ordinary GET /api/jobs answered in 4ms: 200 {}

$ node saturate.mjs 9105 128
128/128 SSE streams open through the BFF
GET /api/jobs STILL PENDING after 15003ms — no response, no error, no timeout
```

Boundary is exactly `maxSockets`. BFF log across the 15 s stall: three boot lines, nothing else.

**The default the brief asked me to name.** There is none that bounds this — the 128 is an explicit
narrowing *below* Node's default, not a default anyone can raise by config:

```
$ node -e "…"
globalAgent.maxSockets= Infinity maxTotalSockets= Infinity default new Agent maxSockets= Infinity
```

**Where "forever" breaks (`/tmp/bffv/recover.mjs`).** I saturated to 128, issued the `/api/jobs`,
then destroyed exactly one browser stream at t=4 s:

```
128/128 streams open
t=4005ms: /api/jobs still pending; now closing ONE browser stream
/api/jobs answered after 4010ms: 200 {}
```

5 ms after a socket frees, the queued request is dispatched.

### Why

**Reachability: confirmed, and the finding is too generous to the code.** The finding puts the
ceiling at "roughly 64–128 concurrent users" on the assumption of one `/events` stream per tab.
`src/hooks/useJobStreams.ts` sets `MAX_JOB_STREAMS = 3` and opens one `fetch` per watched session,
so a single idle tab holds **three** upstream sockets, four while a turn is in flight. The real
ceiling is therefore ~**32–42 tabs**, not 64–128.

I looked for something upstream that would cap the streams below 128 and kill the finding. The
backend does cap them — `service_max_event_streams_per_user = 5` and
`service_max_event_streams_total = 200`
(`src/chemclaw/core/config/service.py:223,237`, enforced in `api/routes/streams.py`) — but **200 >
128**, so the BFF's own pool saturates first and the backend cap never gets a chance to be the
protective bound. A 429 is also not a rescue: it completes quickly and returns its socket, so it
does not hold budget.

Deployment shape does not rescue it either. The UI is not in the backend's Helm chart
(`deploy/helm/chemclaw/templates/` has service/workers/connectors only — no UI deployment, no
replica count for it), and `docker-compose.yml` runs a single `ui` service. One process, one
module-level agent, one 128-socket pool for every user.

**Consequence: real, but "never dispatched … forever" is wrong.** The queued request is dispatched
as soon as any of the 128 in-flight requests completes — measured at 5 ms above. The honest
statement is *head-of-line blocking for as long as 128 upstream requests are in flight*, which in
this application's steady state (tabs left open all day, streams that are silent by design and
never end) is indefinite in practice but self-clearing rather than terminal. I did not drop the
severity for that, because everything that makes it high survives: the bound is a literal with no
env override, the stall is completely silent (no log, no metric, no 503), and the finding is right
that no timeout can rescue a queued request — `upstreamConnectTimeoutMs` is armed inside
`upstreamReq.on('socket')`, and a queued request has not been assigned a socket, so that handler
has not run.
