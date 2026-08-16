# BFF server slice — security and hardening

Slice: `server/{index,proxy,routes,config,runtimeConfig,log}.ts`, `shared/events.ts`.
All findings below were reproduced against the real `server/index.ts` running under
`node --experimental-strip-types` with a fake upstream on loopback.

---

## Unauthenticated slow-body requests exhaust the 128-socket upstream pool and wedge the whole /api proxy indefinitely

- **Severity**: high
- **Location**: `server/index.ts:96` (`server.requestTimeout = 0`) together with
  `server/proxy.ts:29-34` (`new transport.Agent({ keepAlive: true, maxSockets: 128, timeout: 0 })`),
  `server/proxy.ts:110` (`transport.request(...)` fires before any body has arrived) and
  `server/proxy.ts:191` (`req.pipe(upstreamReq)`).
- **Trigger**: 128 plain TCP connections to the BFF. On each, send a *complete* header block for a
  whitelisted route with a body — e.g.

  ```
  POST /api/sessions/00000000000000000000000000000000/attachments HTTP/1.1
  Host: bff
  Content-Type: application/octet-stream
  Content-Length: 1000000

  x            <- one byte, then dribble one byte every 30s, forever
  ```

  No credentials, no valid session, no `Authorization` header. The session id only has to be 32
  lowercase hex to satisfy the whitelist (`routes.ts:16`); it does not have to exist.

- **Consequence**: every `/api/*` request from every other user hangs forever. `proxy()` calls
  `transport.request()` the instant the route resolves — before a byte of body has arrived — so each
  held connection claims one of the agent's 128 sockets and keeps it for as long as the attacker
  keeps the body open. Nothing in the process ever times these out: `headersTimeout` (125s) only
  covers the header phase, which the attacker completes immediately, and `requestTimeout` — the one
  setting that bounds *receipt* of a request — is explicitly set to `0`. The 129th proxied request
  queues inside the agent and never dispatches. The SPA becomes unusable while the process stays
  green on every health signal: `/healthz` (answered locally, `index.ts:57`) and all static assets
  keep returning 200, so a liveness probe sees a healthy pod.

  This is fully pre-authentication. FastAPI reads the request body *before* it solves route
  dependencies, so the backend's `require_principal` cannot answer 401 until the dribbled body
  finishes (measured below). I confirmed the counter-case too: when an upstream answers immediately
  without reading the body, Node releases the socket and the attack does not hold — so it is
  specifically the body-reading whitelisted routes (`POST …/attachments`, `POST …/messages`) that
  are the vector, and both are whitelisted.

  The same mechanism is also a plain capacity ceiling with no attacker: `src/hooks/useJobStreams.ts:48`
  holds `MAX_JOB_STREAMS = 3` long-lived `GET /sessions/{id}/events` streams per tab, each occupying
  one pool socket for its lifetime. 43 concurrent tabs (43 × 3 = 129) stall the entire deployment
  permanently.

- **Evidence**:

  `server/index.ts:90-96` reasons about exactly this setting and then removes the bound:

  ```ts
  // `requestTimeout` measures time to RECEIVE a request, not to respond, so
  // disabling it does not affect long SSE responses — but set it explicitly so nobody has to
  // re-derive that when they see a 600s stream and a 300s default in the Node docs.
  server.requestTimeout = 0;
  ```

  The premise is right and the conclusion is inverted: time-to-receive is precisely the thing that
  needs bounding, and it is unrelated to the long SSE *responses* the comment is protecting.

  Reproduction (`/tmp/bffprobe/slow.mjs`, 128 raw sockets, headers complete + 1 body byte):

  ```
  opened 128 slow-body connections; holding.
  === now a normal proxied request (5s timeout) ===
  http=000
  real  0m5.010s
  CURL TIMED OUT / FAILED

  === static asset while blocked ===        http=200
  === own healthz (non-proxied) ===         http=200
  === upstream reachable directly ===       http=200
  === still blocked after 30s? ===          http=000
  === kill slowloris, retest ===            recovered http=200
  ```

  Threshold confirms the pool is the limiter, not the event loop:

  ```
  with 20 held:   http=200
  with 128 held:  http=000 (timeout)
  ```

  FastAPI reads the body before auth (`/tmp/bffprobe/fastapi_order.py`, a route with
  `dependencies=[Depends(deny)]` raising 401 and a `file: UploadFile` param, fed a slow
  multipart body in 3 chunks):

  ```
  status: 401 body chunks pulled before answer: 3
  ```

  All three chunks were pulled off the wire before the 401 was produced — i.e. the upstream holds
  the socket for the full body regardless of credentials.

  Counter-case (upstream answers 401 immediately without reading the body): 128 held connections,
  proxied request still returns `http=401` — the attack depends on a body-reading route, which the
  whitelist provides.

- **Fix**: three changes, all in this slice.
  1. `server/index.ts`: set a real `server.requestTimeout` (e.g. 120s — comfortably above the 4 MB
     `service_max_request_bytes` upload on a slow link). It bounds only request *receipt*, so long
     SSE responses and 600s turns are unaffected; that is exactly what the existing comment
     establishes.
  2. `server/proxy.ts`: do not claim an upstream socket before the request is usable. Either raise
     `maxSockets` well past the intended concurrent-stream ceiling and make it configurable
     (`cfg.upstreamMaxSockets`), or track in-flight proxied requests and shed with 503 past a
     configured cap rather than queueing invisibly inside the agent. A silent unbounded queue behind
     a hard 128 is the failure mode here.
  3. Bound total connections (`server.maxConnections`) so header-phase sockets are capped too.

  A metric for in-flight upstream requests would have made this visible; today the process reports
  healthy while serving nothing.

---

## `GET /api/readyz` is whitelisted, so the internal connector fleet and database state are readable by any anonymous visitor

- **Severity**: low
- **Location**: `server/routes.ts:86` —
  `{ method: 'GET', pattern: /^\/api\/readyz$/, target: () => '/readyz', sse: false }`
- **Trigger**: `curl https://<ui-host>/api/readyz` with no `Authorization` header, from anywhere the
  UI is reachable.
- **Consequence**: `/readyz` is one of the three routes the backend leaves deliberately
  unauthenticated for a kubelet (`src/chemclaw/api/routes/ops.py:3`, and it does not depend on
  `require_principal` — confirmed by reading `auth.py:252` and the route wiring at `ops.py:171`).
  Whitelisting it in the BFF republishes a kubelet-facing endpoint on the *public* surface. Its body
  enumerates every connector by name with its state plus whether Postgres is answering:

  ```json
  {"status": "ready", "connectors": "qm=up, eln=down, calc=up, …"}
  ```

  That is an inventory of the deployment's internal capability fleet and a live outage signal,
  handed to an unauthenticated caller. It is not rate limited either — the backend's token bucket is
  wired inside `require_principal`, which this route does not use (`rate_limit.py:21`).

  The route is dead weight as well as exposure: nothing in the SPA calls it. `grep -rn readyz src/`
  returns nothing; the only health check the client makes is `/api/healthz`
  (`src/components/TopBar.tsx:206`, `src/api/client.ts:225`). It appears in the whitelist and in
  `tests/routes.test.ts:12` and nowhere else.

- **Evidence**: header echo through the real proxy shows a request with no `Authorization` reaching
  the upstream unchanged on the same code path (`resolveRoute` → `proxy`):

  ```json
  {"path":"/healthz","method":"GET",
   "headers":{"user-agent":"curl/8.5.0","accept":"*/*", … ,"host":"127.0.0.1:9911"}}
  ```

  Backend side, `readyz` runs `_connector_health()` and `_database_reachable()` and returns their
  names/states with no principal in scope (`ops.py:91-125`).

- **Fix**: delete the `/api/readyz` row from `ROUTES` (and its line in `tests/routes.test.ts`). The
  readiness probe belongs to the orchestrator on the pod's own address, not to the browser-facing
  proxy. If an operator affordance is genuinely wanted, proxy it behind the same reviewer-role check
  the SPA already applies to privileged affordances, and have the backend gate it.

---

## Dev auth mode also switches off both clickjacking defences, coupling framing policy to the auth posture

- **Severity**: low
- **Location**: `server/config.ts:90` (`'frame-ancestors': mode === 'dev' ? ['*'] : ["'none'"]`) and
  `server/index.ts:44` (`if (cfg.authMode !== 'dev') res.setHeader('x-frame-options', 'DENY')`).
- **Trigger**: run with `AUTH_MODE=dev` (the default in `config.ts:33` and what `start.sh` selects)
  and request any document.
- **Consequence**: the deployment with *no authentication at all* is also the one served with
  `frame-ancestors *` and no `X-Frame-Options`, so any origin on the internet may embed it. The
  practical gain for an attacker is reaching a UI that is only routable from the victim's network:
  the attacker cannot script cross-origin calls (a JSON `POST /api/sessions` needs a preflight, and
  `OPTIONS` matches no route so it 404s without CORS headers — verified), but an invisible frame plus
  a click gets a user to press Approve on a knowledge proposal, cancel a durable job, or send a
  prepared turn. Framing is the widest cross-origin surface left, and it is open exactly where
  nothing else is defending.

  The design flaw behind it is the coupling: the knob for framing is the *auth mode*. There is no
  way to get a hardened frame policy in dev, or a relaxed one under `msal`, and the comment
  ("Allow framing from any origin in dev mode so the Replit preview iframe works") names a single
  deployment's need while the branch applies to every dev-mode deployment.

- **Evidence**: live headers from the running BFF with `AUTH_MODE=dev` (note `frame-ancestors *`,
  and no `x-frame-options` line at all):

  ```
  HTTP/1.1 200 OK
  content-security-policy: default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self';
    style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:;
    connect-src 'self'; frame-src 'none'; form-action 'self'; base-uri 'none';
    frame-ancestors *; object-src 'none'
  x-content-type-options: nosniff
  referrer-policy: same-origin
  ```

- **Fix**: give framing its own setting instead of deriving it from `authMode` — e.g.
  `FRAME_ANCESTORS` (default `'none'`), read in `config.ts` and consulted by both `buildCsp` and the
  `x-frame-options` branch in `index.ts`. The Replit preview then sets one explicit variable and
  every other dev deployment keeps `DENY`. Set both headers consistently from that one value; today
  they are decided in two files by two separate expressions.

---

## Checked and found sound (no finding)

Recorded so a later pass does not re-derive them:

- **Route whitelist holds.** No bypass found via absolute-form request URIs
  (`GET http://evil.example/metrics` → falls to the SPA fallback, never proxied), `#` fragments,
  double slashes, lowercase methods, or `OPTIONS`. Encoded traversal through the deliberately-wide
  `NOTE`/`APPROVAL`/`JOB` character sets forwards still-encoded
  (`/api/notes/%2e%2e%2f%2e%2e%2fmetrics` → `/notes/%2e%2e%2f%2e%2e%2fmetrics`), and Starlette's
  `[^/]+` path converter refuses the decoded form — verified against a real Starlette router: all
  three traversal shapes 404. The claim in `routes.ts:33-37` is accurate.
- **Static serving is traversal-safe.** `/../secret.txt`, `/..%2fsecret.txt`, `/%2e%2e/secret.txt`,
  `/..%252fsecret.txt` all return the SPA `index.html`, never a file outside `CLIENT_DIR`.
- **No crash on hostile HTTP.** 13 malformed-request shapes (high-byte query, `%00`/`%0d%0a` in the
  query, 60 KB request line, TE+CL smuggling, duplicate `Content-Length`, `Expect: 100-continue`,
  3000 headers, obs-text header values) — Node rejects the dangerous ones at the parser and the
  process survived all of them. A client abort racing the upstream connect timeout also does not
  produce an uncaught `write-after-destroy` (tested with a blackhole upstream and a 2s connect
  timeout: one `WARN upstream error … socket hang up`, process alive).
- **No secret reaches a log, the bundle or an error body.** `log.ts` emits only method + path;
  `Authorization` is never logged. `/config.js` carries only operator-supplied tenant/client
  ids and role names, and `renderConfigScript`'s `<` escape does what it claims.
- **No CSRF surface.** Cookies are dropped at `proxy.ts:94`, the backend sets no cookies and
  `allow_credentials` is false, and `service_cors_origins` defaults to empty. The comment's claim
  checks out.
- **Security headers on proxied API responses** are the backend's own (`nosniff`, `X-Frame-Options`,
  HSTS, CSP, on by default via `service_security_headers`), relayed verbatim — so the BFF not
  setting its own on the `/api` path is not a gap.
- **Client-supplied `X-Forwarded-For` / `X-Forwarded-Host` / `X-Real-IP` are forwarded verbatim and
  the BFF sets none of its own** (verified by header echo). The backend reads no header but
  `Authorization` (grep over `src/chemclaw/api/` finds exactly one other, an HMAC webhook signature),
  and rate limiting is keyed per principal, not per IP — so there is no consequence to demonstrate
  today. It is a latent trap for the first component that starts trusting a forwarded header.
- **`shared/events.ts`** coerces every field it claims to, with one exception: `job_completed.summary`
  is cast (`as JobSummary`) with no coercion at all, despite the docstring's "every field is
  defensively coerced". I looked for a sink and found none — the three consumers read named keys
  behind `typeof` guards and there is no `Object.assign`-style merge — so there is nothing to
  reproduce and I am not filing it.
