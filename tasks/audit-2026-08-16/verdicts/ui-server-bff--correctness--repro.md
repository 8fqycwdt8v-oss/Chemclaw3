# ui-server-bff — CORRECTNESS (round 1) — adversarial reproduction

Scope: the two **high** findings. Medium/low ignored per brief.

All measurements below are mine. I did not run the reporter's `/tmp/bff/*` scripts (they were not
present) and did not accept their transcripts. I wrote my own stub upstreams
(`/tmp/mybff/upstream.mjs`, `/tmp/mybff/upstream_silent.mjs`, `/tmp/mybff/up_clean.mjs`), my own
clients (`/tmp/mybff/client_sse.mjs`, `/tmp/mybff/saturate.mjs`, `/tmp/mybff/saturate2.mjs`) and
drove the **real, unmodified** `/workspace/chemclaw3_ui/server/index.ts` under
`node --experimental-strip-types` (node v22.22.2). Working tree at `1a1f6f0`, `git status` clean —
no mutation markers, no diff against HEAD, so no need to consult the pristine copy.

Both findings reproduce. One of them is worse than filed.

---

## A lost upstream connection leaves the browser's response open forever

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

Stub upstream on :9201 that writes one valid SSE frame and then calls `res.socket.destroy()` after
300 ms; the real BFF on :9200 pointed at it; a plain `fetch()` client that reads the body and prints
exactly how the stream ends.

```
$ node /tmp/mybff/client_sse.mjs 9200 /api/sessions/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/events
+92ms status=200 ct=text/event-stream cl=null
+93ms chunk "event: token\r\ndata: {\"type\":\"token\",\"text\":\"partial\"}\r\n\r\n"
+10003ms STILL READING — giving up
+10007ms ERROR AbortError: This operation was aborted
```

Non-streaming route, upstream promises `content-length: 100`, sends 10 bytes, then destroys the
socket (`GET /api/proposals`):

```
+76ms status=200 ct=application/json cl=100
+78ms chunk "{\"items\":["
+10000ms STILL READING — giving up
```

BFF log for both, in full — no `upstream error for ...` line ever appeared:

```
2026-08-17T05:19:49.224Z INFO  chemclaw3-ui listening on http://127.0.0.1:9200
2026-08-17T05:19:49.225Z INFO  proxying /api -> http://127.0.0.1:9201
2026-08-17T05:19:49.225Z INFO  auth mode: dev
```

**"Forever" is literal, not a 10-second extrapolation.** I re-ran the SSE case with a 150 s budget,
which is past both `server.keepAliveTimeout` (120 s) and `server.headersTimeout` (125 s):

```
+0.1s status=200
+0.1s chunk "event: token\r\ndata: {\"type\":\"token\",\"text\":\"partial\"}\r\n\r\n"
+150.0s STILL READING after 150s — giving up
```

**Control** (this is the part that rules out "the harness just never ends anything"): same BFF, same
client, an upstream that calls `res.end()` instead of destroying the socket —

```
+85ms status=200 ct=text/event-stream cl=null
+86ms chunk "event: token\r\ndata: {\"t\":1}\r\n\r\n"
+382ms CLEAN done (stream ended normally)
```

So the hang is specific to abnormal upstream death, exactly as claimed.

**Mechanism, isolated from the BFF entirely** (`/tmp/mybff/mech.mjs` — a bare `http.request` whose
server destroys the socket after the headers, piping into a `PassThrough`):

```
res headers
RES aborted
REQ close
RES error -> ECONNRESET aborted
RES close, complete=false
--- 1s later: dest.writableEnded = false destroyed = false
```

The `ClientRequest` emits `close` and **no `error`**; the error lands on the `IncomingMessage`; and
`.pipe()` leaves the destination neither ended nor destroyed. That is precisely the finding's stated
cause.

### Why

Every element of the claim checks out on the current source. Line numbers and symbols are real and
current: `proxy.ts:143` `upstreamRes.pipe(res)`, `proxy.ts:179` `upstreamReq.on('error', ...)` — the
only error handler in the file — `proxy.ts:82` `upstreamRes.on('error', stop)` whose body is
`clearInterval` and nothing else, `proxy.ts:33` `timeout: 0`, `proxy.ts:148` `setTimeout(0)`,
`index.ts:96` `requestTimeout = 0`.

I looked for a default that would bound it and there is none:

```
http.Server defaults: timeout= 0 keepAliveTimeout= 5000 headersTimeout= 60000 requestTimeout= 300000
```

`server.timeout` — the only socket-inactivity timer on the browser-facing side — has defaulted to
`0` since Node 13, so nothing reaps a stalled response socket. `requestTimeout` would not have
helped even at its 300 s default (it measures time to *receive* a request), and it is explicitly
disabled here anyway. Confirmed with no reservations.

Two things to add that the reporter did not:

1. The fix is smaller than it looks. `proxy.ts:179-183` **already** contains the correct downstream
   action for a post-headers failure (`if (res.headersSent) { res.destroy(); return; }`) — it is
   just attached to the emitter that provably never fires in this case. Wiring the same handler to
   `upstreamRes`'s `error`/`aborted` is the whole change.
2. The `aborted` event fires *before* `error` in my trace, so a handler on `aborted` alone is
   sufficient for the destroy and the `error` handler only needs to not double-log.

---

## 128 concurrent SSE streams wedge every other request through the BFF, forever

- **Verdict**: CONFIRMED (and understated — see below)
- **Severity I would assign**: high, at the top of that band

### What I did

Silent upstream on :9211 that holds `/sessions/*/events` open forever and answers `/jobs` instantly;
real BFF on :9210. My saturation client uses **raw `http.request` with
`new http.Agent({ keepAlive: false, maxSockets: Infinity })`** rather than `fetch`, specifically so
that no client-side pool of my own could be mistaken for the BFF's — a control the finding does not
mention.

```
$ node /tmp/mybff/saturate.mjs 9210 127
127/127 SSE streams open through the BFF
ordinary GET /api/jobs answered in 2ms: 200 {}

$ node /tmp/mybff/saturate.mjs 9210 128
128/128 SSE streams open through the BFF
GET /api/jobs STILL PENDING after 12003ms — no response, no error
```

Nothing was written to the BFF log during either run. 12 s is past the 10 s
`upstreamConnectTimeoutMs` default, so the finding's claim that the connect timer never arms for a
queued request (no socket assigned → `socket.connecting` never evaluated) holds.

**Proof it is the agent's queue and not something else** (`/tmp/mybff/saturate2.mjs`): saturate to
128, issue `GET /api/jobs`, wait 4 s, then destroy exactly one stream —

```
128/128 streams open
+4000ms: still pending, now closing ONE stream
/api/jobs answered after 4009ms
```

9 ms after a socket was freed. That is a queue, not a drop.

**Proof the constant at `proxy.ts:32` is the cause**: I copied `server/` to `/tmp/mybff/srv`,
changed `maxSockets: 128` to `maxSockets: 8`, changed nothing else, and the cliff moved with it —

```
7/7 SSE streams open through the BFF
ordinary GET /api/jobs answered in 3ms: 200 {}
8/8 SSE streams open through the BFF
GET /api/jobs STILL PENDING after 12004ms — no response, no error
```

I also checked for a bounding default and found the opposite:

```
http.Agent defaults: maxSockets= Infinity maxTotalSockets= Infinity maxFreeSockets= 256
                     options.timeout= undefined scheduling= lifo
Agent option keys: [ 'noDelay', 'path' ]
```

`node:http`'s `Agent` exposes **no queue/scheduling timeout of any kind**, and its default
`maxSockets` is `Infinity`. So there is no setting anywhere that rescues a queued request, and the
defect exists only because the file narrows an unbounded default to a literal 128.

### Why

Reproduces exactly, at exactly the stated boundary, with causality pinned to the named line. Three
corrections, two of which make it worse:

1. **The user ceiling is ~2–4× lower than filed.** The finding says "roughly 64–128 concurrent
   users". `src/hooks/useJobStreams.ts:47` sets `MAX_JOB_STREAMS = 3` — a single tab watches the
   active conversation **plus the two most recently used**, so an *idle* tab holds three upstream
   sockets, and a tab mid-turn holds four. The module-level `agent` in `proxy.ts` is shared
   process-wide, so the real ceiling is roughly **32 tabs mid-turn / 42 idle tabs**, not 64–128.
2. **The healthcheck stays green through it**, which the finding does not mention and which turns a
   wedged pod into a permanently wedged pod. Measured with 128 streams open:

   ```
   /healthz   200 2ms
   /config.js 200 2ms
   /          200 4ms
   /api/jobs  PENDING >3000ms
   ```

   `Dockerfile:31` and `docker-compose.yml:109` both probe `/healthz`. So neither Docker nor a
   Kubernetes liveness probe will ever restart or evict a BFF that can serve zero API requests, and
   a readiness probe will keep routing new users into it.
3. Minor, in the finding's favour: "the whole BFF goes silently unresponsive" is loose — static
   assets, `/config.js` and `/healthz` keep working. Every enumerated consequence (new session,
   message send, proposal decision) is correct, and the SPA is unusable, so this changes nothing
   about severity. It is also fully self-healing once a stream closes, which is the one reason I
   stop at high rather than critical.

The proposed fix (two agents, selected on the `expectSse` argument `proxy()` already receives) is
sound. I would add that fix 1's `upstreamRes` error handling is a prerequisite for it to hold in
practice: without it, every mid-stream upstream death permanently leaks the client response, and
`res.on('close')` — the only thing that releases the upstream socket back to the pool — never fires
until the browser tab is closed. The two defects compound.
