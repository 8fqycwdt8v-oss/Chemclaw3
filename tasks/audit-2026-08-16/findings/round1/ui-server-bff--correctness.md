# ui-server-bff — CORRECTNESS (round 1)

Slice: `server/{index,proxy,routes,config,runtimeConfig,log}.ts` + `shared/events.ts` in
`/workspace/chemclaw3_ui`.

Method: every finding below was reproduced by running the **real** `server/index.ts` under
`node --experimental-strip-types` against purpose-built upstream servers on loopback, and reading
the bytes off the wire. Commands and printed output are quoted verbatim.

Five findings. Two are high (a request that hangs forever, twice, by two independent mechanisms);
two are medium; one is low. The route whitelist, the 502 path, the client-disconnect propagation,
the config validator and the raw-byte handling in the request target were all probed and are
**sound** — see "Checked and clean" at the end.

---

## A lost upstream connection leaves the browser's response open forever

- **Severity**: high
- **Location**: `server/proxy.ts:120-145` (the `transport.request` response callback,
  `upstreamRes.pipe(res)`), and `server/proxy.ts:179-187` (`upstreamReq.on('error')`)
- **Trigger**: any request whose upstream TCP connection dies *after* the response headers have
  been written and *before* the body is complete. In production this is: a backend pod restart or
  rolling deploy mid-turn, an OOM-kill, an ingress/service-mesh idle-timeout between the BFF and
  the Python front door, or an `RST` from a scaled-down replica.
- **Consequence**: the BFF **never terminates the client's response**. No `end`, no `error`, no
  `aborted`, no truncation the browser can detect, and nothing logged. `fetch()`'s body reader
  simply blocks. On the turn stream (`POST /api/sessions/{id}/messages`) the SPA sits on
  "thinking" indefinitely and the user's turn is lost silently; on a plain JSON route the response
  promise never settles. The client socket is leaked on the BFF side too — `server.requestTimeout`
  is deliberately `0` (`server/index.ts:96`), `upstreamReq.setTimeout(0)` (`proxy.ts:148`) and the
  agent is `timeout: 0` (`proxy.ts:33`), so there is no timer anywhere in this process that would
  ever end it.
- **Evidence**: the cause is that when a Node HTTP *client* connection closes mid-response, the
  error is delivered to the **`IncomingMessage`** (`upstreamRes`), not to `upstreamReq`. So
  `upstreamReq.on('error')` — the only error handler in the file — never fires, and
  `Readable.prototype.pipe` does not forward a source error to the destination or call
  `dest.end()`. `upstreamRes` has an `'error'` listener only inside `attachHeartbeat`
  (`proxy.ts:82`), and that listener only calls `clearInterval`.

  Upstream (`/tmp/bff/upstream3.mjs`) writes a valid SSE frame, then `res.socket.destroy()` after
  300 ms. Client is a plain `fetch()` (`/tmp/bff/kill3.mjs`):

  ```
  $ node kill3.mjs
  +134ms status=200 ct=text/event-stream
  +135ms chunk "event: token\r\ndata: {\"type\":\"token\",\"text\":\"partial\"}\r\n\r\n"
  +8134ms STILL READING, no close, no error — giving up
  ```

  Same for a non-streaming route, where the BFF has even forwarded a `content-length: 100` it then
  never satisfies (`/tmp/bff/kill2.mjs`, upstream sends 10 of the promised 100 bytes then dies):

  ```
  $ node kill2.mjs
  status=200 content-length=100
  +21ms data "{\"items\":["
  +8014ms STILL HANGING - giving up
  ```

  The BFF log for both is empty beyond the three boot lines — `upstreamReq.on('error')` did not run:

  ```
  === BFF log ===
  2026-08-16T21:06:24.348Z INFO  chemclaw3-ui listening on http://127.0.0.1:9102
  2026-08-16T21:06:24.349Z INFO  proxying /api -> http://127.0.0.1:9097
  2026-08-16T21:06:24.349Z INFO  auth mode: dev
  ```

  Note the asymmetry: the file goes to real trouble to propagate a **client** disconnect into the
  upstream (`res.on('close')`, `proxy.ts:175-177`, called "the single most important line in the
  file" — and it does work, verified: the upstream saw `res closed for GET /events` when I killed
  the client). The mirror direction — upstream gone, tell the client — is missing entirely.
- **Fix**: handle the response stream's own failure. Add, inside the response callback:

  ```ts
  const abortDownstream = (err: Error): void => {
    log.warn(`upstream stream ended early for ${req.method} ${upstreamPath}: ${err.message}`);
    // Headers are already sent; the only honest signal left is an abnormal close, which
    // fetch() surfaces as a rejected read rather than a clean `done`.
    res.destroy();
  };
  upstreamRes.on('error', abortDownstream);
  upstreamRes.on('aborted', () => abortDownstream(new Error('upstream aborted')));
  ```

  `res.destroy()` (rather than `res.end()`) is the right call precisely because a clean `end` on
  an SSE stream is indistinguishable from a finished turn — the client must be able to tell a dead
  stream from a completed one. Pair it with an `error` event on the SPA side if the surface wants
  to offer a retry.

---

## 128 concurrent SSE streams wedge every other request through the BFF, forever

- **Severity**: high
- **Location**: `server/proxy.ts:29-34` — `new transport.Agent({ keepAlive: true, maxSockets: 128, timeout: 0 })`
- **Trigger**: 128 simultaneously-open long-lived upstream requests. Each open browser tab holds
  one `GET /api/sessions/{id}/events` push-back stream open indefinitely by design
  (`routes.ts:110-116`: "Long-lived and legitimately silent for minutes at a time"), and a tab
  mid-turn holds a second socket for `POST .../messages`. So the ceiling is roughly **64–128
  concurrent users**, not 128 requests/second.
- **Consequence**: the 129th request is queued by the agent and **never dispatched**. It does not
  fail, does not time out and is not logged — the whole BFF goes silently unresponsive for every
  new session, every message send, every proposal decision, while the already-open streams keep
  working. There is no timeout that can rescue it: the agent is `timeout: 0`, the request is
  `setTimeout(0)`, and `upstreamConnectTimeoutMs` only arms when a socket has already been
  *assigned* and is `connecting` (`proxy.ts:156`) — a queued request has no socket, so the connect
  timeout never arms either.
- **Evidence**: `/tmp/bff/saturate.mjs` opens N `/api/sessions/{sid}/events` streams against a
  silent upstream, then issues one ordinary `GET /api/jobs` (which the upstream answers in
  microseconds).

  ```
  $ node saturate.mjs 9104 127          # control
  127/127 SSE streams open through the BFF
  ordinary GET /api/jobs answered in 20ms: 200 {}

  $ node saturate.mjs 9101 128
  128/128 SSE streams open through the BFF
  GET /api/jobs STILL PENDING after 10001ms — no response, no error, no timeout
  ```

  The boundary is exactly `maxSockets`. 127 → 20 ms; 128 → indefinite.
- **Fix**: long-lived streams must not compete for the same socket budget as short requests. Two
  changes, both needed:
  1. Use **two agents**: `maxSockets: Infinity` (or a high, configured bound) for routes with
     `sse: true`, and a bounded pool for the rest. `proxy()` already receives `expectSse`, so the
     selection is one ternary.
  2. Make the bound configuration rather than a literal (`UPSTREAM_MAX_SOCKETS` in `config.ts`,
     which is where every other threshold in this process already lives), and log a warning when
     the pool saturates so the failure is visible instead of silent.

---

## The SSE heartbeat never fires against the real backend, and once suppressed it stays suppressed

- **Severity**: medium
- **Location**: `server/proxy.ts:61-83`, `attachHeartbeat` — specifically the boundary test
  `tail[0] === 0x0a && tail[1] === 0x0a` at `proxy.ts:69`
- **Trigger**: any SSE stream from the actual Chemclaw3 front door. The backend serves SSE through
  `sse_starlette.sse.EventSourceResponse` (`Chemclaw3/src/chemclaw/api/routes/turns.py:231`,
  `routes/streams.py:135`) and passes no `sep=`, so frames are terminated with
  `ServerSentEvent.DEFAULT_SEPARATOR`, which in the pinned `sse-starlette 3.4.8` is **`"\r\n"`**
  (`.venv/.../sse_starlette/event.py:13`). Every frame therefore ends `\r\n\r\n`, whose last two
  bytes are `0x0d 0x0a` — not `0x0a 0x0a`.
- **Consequence**: `atFrameBoundary` goes `false` on the first upstream chunk and, because nothing
  else can ever set it back to `true`, **stays false for the life of the stream**. Not one `: hb`
  frame is ever written. The mechanism the file exists to provide — keeping an intermediary with an
  idle timeout from dropping a healthy but silent stream — is inert. Today the damage is bounded
  because the backend runs its own `ping=15s` (`service_sse_ping_seconds`, default 15), so the
  stream is rarely silent; the defect bites the moment someone sets `SSE_HEARTBEAT_MS` *below* the
  backend ping to bridge a shorter ingress idle timeout, which is the only reason to touch that
  knob at all. Then the ingress kills silent turn streams and the answer is lost.

  A second, framing-independent instance of the same bug: the check is on the **last chunk**, not
  on the last bytes forwarded, so a complete frame delivered as two TCP chunks (`...\n` then `\n`)
  also latches it off permanently — even with LF framing.
- **Evidence**: the docstring at `proxy.ts:57-59` claims *"It also only writes at a frame boundary:
  if the last bytes we forwarded were not `\n\n`, we may be mid-frame"*. The bytes forwarded **are**
  a frame boundary in both cases below; the check is what is wrong.

  Control — upstream emitting LF-terminated frames in one chunk, `SSE_HEARTBEAT_MS=1000`:

  ```
  status=200 ct=text/event-stream
  +0.0s ": open\n\n"
  +1.0s ": hb\n\n"
  +2.0s ": hb\n\n"
  +3.0s ": hb\n\n"
  +4.0s ": hb\n\n"
  ```

  Same BFF, same 1 s heartbeat, upstream emitting exactly what sse-starlette emits (CRLF):

  ```
  status=200 ct=text/event-stream
  +0.1s "event: token\r\ndata: {\"type\":\"token\",\"text\":\"hi\"}\r\n\r\n"
  done watching                       # 6 s elapsed, zero heartbeats
  ```

  And the split-chunk case, with LF framing, where the frame *is* complete on the wire:

  ```
  status=200 ct=text/event-stream
  +0.0s "event: token\ndata: {\"type\":\"token\",\"text\":\"hi\"}\n"
  +0.1s "\n"
  done watching                       # 6 s elapsed, zero heartbeats
  ```
- **Fix**: track the last two bytes *forwarded*, not the tail of the last chunk, and accept both
  spellings of a frame terminator. Keep a 2-byte rolling tail:

  ```ts
  let tail = Buffer.alloc(0);
  upstreamRes.on('data', (chunk: Buffer) => {
    lastChunkAt = Date.now();
    tail = Buffer.concat([tail, chunk]).subarray(-4);   // enough for \r\n\r\n
    const s = tail.toString('latin1');
    atFrameBoundary = s.endsWith('\n\n') || s.endsWith('\r\n\r\n') || s.endsWith('\r\r');
  });
  ```

  (`\r\r` is the third terminator the SSE spec permits.) Worth a test that drives the real
  `attachHeartbeat` with CRLF framing, since nothing in `tests/` exercises `proxy.ts` at all.

---

## The SPA fallback serves `index.html` with no `cache-control`, so a deploy does not take on any deep link

- **Severity**: medium
- **Location**: `server/index.ts:38-49`, the `sirv` `setHeaders` callback — the
  `pathname === '/index.html' || pathname === '/'` test at line 46
- **Trigger**: a browser loading any client route other than `/` — a reload of `/chat/<id>`, a
  bookmarked deep link, or the MSAL redirect landing on `/auth/callback` (the route the comment
  directly above at line 31 names as the reason `single: true` exists).
- **Consequence**: `sirv`'s `single: true` fallback passes the **requested** pathname to
  `setHeaders`, not `/index.html`, so the comparison fails and the `cache-control: no-cache` is
  never set. `sirv` sets no `Cache-Control` of its own, but does send `ETag` and `Last-Modified`,
  which is precisely the shape browsers apply *heuristic* freshness to (RFC 9111 §4.2.2 — commonly
  10% of the Last-Modified age) and serve from cache **without revalidating**. After a deploy, a
  user reloading `/chat/<id>` gets the previous `index.html`, whose `<script src>` points at hashed
  bundle filenames that no longer exist → blank page or a chunk-load error, with no way for the
  user to recover other than a hard refresh. The comment at line 45 states the invariant the code
  then fails to hold: *"index.html must never be cached or a deploy won't take."*
- **Evidence**: live BFF, `CLIENT_DIR` containing one `index.html`:

  ```
  --- GET /
  HTTP/1.1 200 OK
  cache-control: no-cache
  Last-Modified: Sun, 16 Aug 2026 21:03:15 GMT
  ETag: W/"15-1786914195161"
  --- GET /index.html
  HTTP/1.1 200 OK
  cache-control: no-cache
  ...
  --- GET /chat/abc
  HTTP/1.1 200 OK
  Last-Modified: Sun, 16 Aug 2026 21:03:15 GMT
  ETag: W/"15-1786914195161"          <-- no cache-control at all
  --- GET /auth/callback
  HTTP/1.1 200 OK
  Last-Modified: Sun, 16 Aug 2026 21:03:15 GMT
  ETag: W/"15-1786914195161"          <-- no cache-control at all
  ```

  The security headers (`content-security-policy`, `x-content-type-options`) *are* applied on the
  fallback, so `setHeaders` is running — only the pathname branch is wrong.
- **Fix**: invert the test so the *hashed asset* path is the special case, since that is the set
  that is actually knowable from the pathname, and everything else (including every fallback) gets
  `no-cache`:

  ```ts
  if (pathname.startsWith('/assets/')) {
    res.setHeader('cache-control', 'public, max-age=31536000, immutable');
  } else {
    res.setHeader('cache-control', 'no-cache');
  }
  ```

  Vite emits hashed output under `assets/` by default; if that prefix is configured, read it from
  `cfg` rather than hardcoding.

---

## `normalizeEvent` drops `plan_hash` from the `plan` event, forcing back the round trip the field was added to remove

- **Severity**: low
- **Location**: `shared/events.ts:45-50` (`PlanEvent`) and `shared/events.ts:448`
  (`case 'plan': return { type: 'plan', todos: asStringArray(o.todos) }`)
- **Trigger**: any turn in harness mode that emits a `plan` event.
- **Consequence**: a field that crosses the process boundary is silently discarded on
  deserialization. The backend's `PlanEvent` carries `plan_hash` (verified from the live model:
  `plan ['plan_hash', 'todos', 'type']`) specifically so a client watching the stream can answer
  the plan it just rendered; without it the SPA must issue a second `GET /sessions/{id}/plan`
  (`src/components/Prompts.tsx:164`) purely to obtain the hash. Consent integrity is preserved
  in practice only because `Prompts.tsx` renders the *fetched* todo list rather than the streamed
  one, so the hash and the rendered plan stay consistent — but the extra round trip is real, the
  approval card is bound to a single mount-time fetch and therefore goes stale on every subsequent
  plan revision (recovering only via a 409 `plan_changed` round trip), and the streamed plan the
  trace panel shows can disagree with the card.

  This is the exact class of drift the file's own header warns about at length ("this file mirrors
  a contract that lives in another repository, and nothing mechanical connects them"); the header
  tracks *members* of the union, and this is the same failure one level down, at the *field*.

  Also dropped, but genuinely harmless: `token.agent` (backend has it, UI does not) and
  `answer.challenged` / `answer.review_hold_id` — all three are permanently at their defaults now
  that the specialist team and challenge panel are gone.
- **Evidence**:

  ```
  $ uv run python -c "... print(t, sorted(obj.model_fields.keys()))"
  plan ['plan_hash', 'todos', 'type']
  token ['agent', 'text', 'type']
  answer ['challenged', 'confidence', 'review_hold_id', 'review_required', 'text', 'type', 'unsupported_claims', 'verified_by']
  ```

  against `shared/events.ts:46-50`:

  ```ts
  export interface PlanEvent {
    type: 'plan';
    todos: string[];
  }
  ```
- **Fix**: add `plan_hash: string` to `PlanEvent` and `plan_hash: asString(o.plan_hash)` to the
  `case 'plan'` branch (empty string already means "predates the hash / fetch it", which is the
  backend's own stated reading), then let `Prompts.tsx` bind its Approve button to the streamed
  hash and drop the mount-time `GET /plan`. Separately: `EVENT_TYPES` is checked against the
  backend by hand; nothing checks *fields*. `scripts/check-openapi.mjs` already exists — extending
  it to diff each event model's field set against `shared/events.ts` would end this class of
  finding rather than fixing its fifth instance.

---

## Checked and clean

Probed and found correct, so a later reviewer does not repeat the work:

- **Route whitelist** (`routes.ts`). Patterns are anchored; `SID`/`RESULT_REF` are structurally
  traversal-proof; the wider `APPROVAL`/`NOTE`/`JOB` sets forward still-encoded and a raw `/`
  cannot match. `GET /api/notes/%2e%2e%2f%2e%2e%2fmetrics` resolves to `/notes/%2e%2e%2f…`, which
  uvicorn unquotes to a path containing `/` that Starlette's `/notes/{note_id}` route then refuses
  — no traversal. `approval-Suzuki(A)` and `approval-a%20b` both match as documented. Method and
  hex-length boundaries behave (`0000…0` 31 chars → `null`, 20-digit proposal id → `null`).
- **The 502 path.** Dead upstream, `GET` and a 1 MB `POST` alike, both return a complete
  `502 {"detail":"upstream unavailable","code":"ECONNREFUSED"}` with a log line. The re-entrant
  `upstreamReq.on('error')` from `req.pipe()` writing into a destroyed request does not truncate it.
- **Client-disconnect propagation** (`proxy.ts:175-177`). Verified end-to-end: killing the client
  socket mid-SSE closed the upstream request (`UPSTREAM res closed for GET /sessions/…/events`).
- **Raw bytes in the request target.** `0x7f`, `0x80`, `0xff` in the query or a path segment are
  rejected by Node's own parser with `400` before the handler runs; no `ERR_UNESCAPED_CHARACTERS`
  throw reaches `transport.request`, and the process survives.
- **`config.ts` / `validateConfig`.** `num`/`bool`/`str` coercions hold at their edges (`PORT=abc`
  → default, `SSE_HEARTBEAT_MS<=0` and `UPSTREAM_CONNECT_TIMEOUT_MS<=0` are both guarded at their
  use sites); the `authModeIsValid` refusal is genuine, not a warning.
- **`runtimeConfig.ts`.** `<` escaping is correct, `content-length` uses `Buffer.byteLength`, and
  Node suppresses the body on `HEAD` so the length is not a lie.
- **`log.ts`.** Unknown `LOG_LEVEL` falls to `info` rather than silencing output.
- **Event union membership.** `EVENT_TYPES` (17) matches the backend's 17 discriminators exactly —
  the drift the file's header documents has been fixed at the member level. Only the field-level
  drift above remains.
