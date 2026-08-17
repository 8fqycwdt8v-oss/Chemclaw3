# Verdicts — ui-server-bff--security (repro lens)

Only the **high** finding is in scope. The file's other two findings are marked **low**
(`/api/readyz` exposure, dev-mode framing coupling) and are out of scope by the audit's severity
filter, so they are not adjudicated here.

Setup for the one in-scope finding: I ran the **real** backend (`chemclaw.api.app.create_app`) with
identity enforced (`CHEMCLAW_ENTRA_REQUIRED=true`, non-loopback refusal satisfied) on `:9800`, and
the **real** BFF (`node --experimental-strip-types server/index.ts`, `AUTH_MODE=dev`) on `:9099`
proxying to it. I wrote my own raw-socket probes (`/tmp/repro/*.mjs`); I did not run the reporter's
scripts.

---

## Unauthenticated slow-body requests exhaust the 128-socket upstream pool and wedge the whole /api proxy indefinitely

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**:

  1. **Auth precedes nothing; the body is parsed first.** A raw slow-body POST straight to the
     backend `POST /sessions/<32-hex>/attachments`, no `Authorization`, valid multipart preamble,
     `Content-Length: 1000000`, then stall:
     ```
     NO ANSWER after 8s — socket held (body precedes auth)
     ```
     (A *malformed* first byte instead answers `400` in 3 ms — the parser trips early. A valid
     preamble makes it wait.) This is FastAPI-intrinsic: `fastapi/routing.py` awaits
     `request.form()` (line ~430) **before** `solve_dependencies` (line ~481), so `require_principal`
     — reached only through `Depends(resolve_session)` on this route — never runs until the multipart
     body finishes. The 401 I *can* get (`POST …/attachments` with a complete small body → `401`)
     only arrives after the body is consumed.

  2. **End-to-end pool exhaustion through the real BFF**, threshold sweep with the finding's exact
     `Content-Length: 1000000` and a 1-byte dribble:
     ```
     20 held:  /api/healthz -> 200 OK
     127 held: /api/healthz -> 200 OK
     128 held: /api/healthz -> TIMEOUT
     128 held: /healthz (non-proxied) -> 200 OK
     128 held: / (static) -> 200 OK
     still blocked after 3s: /api/jobs -> TIMEOUT
     after killing slowloris: /api/healthz -> 200 OK
     ```
     The boundary is exactly `maxSockets = 128` (`proxy.ts:32`): 127 held still serves, the 128th
     wedges every subsequent `/api/*` request. The process stays green on `/healthz` (answered
     locally, `index.ts:57`) and on static assets — a liveness probe sees a healthy pod while the API
     serves nothing. Killing the held connections recovers it immediately. No credentials, no valid
     session; the all-zeros 32-hex id satisfies `SID` (`routes.ts:16`) without existing.

  3. Cited lines all real and current: `index.ts:96` `server.requestTimeout = 0`; `index.ts:95`
     `headersTimeout = 125_000`; `proxy.ts:29-34` `Agent({maxSockets:128, timeout:0})`;
     `proxy.ts:110` `transport.request(...)`; `proxy.ts:191` `req.pipe(upstreamReq)`;
     `useJobStreams.ts:48` `MAX_JOB_STREAMS = 3`.

- **Why**: The mechanism reproduces on the real code, unauthenticated, with the stated consequence.
  The single keep-alive agent (`maxSockets:128`) is shared between long-held body-reading proxied
  requests and every short one; `proxy()` claims the socket the instant the route resolves and the
  backend holds it for the whole multipart parse, which — for a whitelisted body-reading route —
  precedes the 401. Nothing bounds receipt: `requestTimeout` is explicitly `0` and `headersTimeout`
  only covers the header phase the attacker completes immediately. 128 trivially-cheap held
  connections (1 byte / 30 s) queue the 129th proxied request invisibly inside the agent, forever.

  **One correction to my own first attempt, which strengthens rather than weakens the finding.** My
  initial probe used `Content-Length: 100000000` (100 MB) and got an instant `413` from the
  backend's `BodySizeLimit` (`core/asgi.py`), which refuses a *declared* length over
  `service_max_request_bytes` (4 MB) before reading a byte — the socket is released and the attack
  fails. That is the finding's own documented counter-case, not a refutation: `BodySizeLimit` only
  defeats the *naive* "declare huge, send nothing" slowloris. The working attack declares a length
  **under** the 4 MB cap (the finding specifies 1 MB) and dribbles below it — the declared check
  passes, the streaming byte-counter never trips because 1 byte/30 s never reaches 4 MB, and the
  socket is held indefinitely. The reporter got this exactly right; I reproduced it verbatim once I
  used their length.

  Severity **high** (not critical): unauthenticated, trivially cheap, total availability DoS of the
  entire `/api` surface with health probes staying green so no orchestrator remediates it — but
  availability only, and it needs 128 concurrent connections held. High is the right call.

- **Overlap with the correctness file's second finding ("128 concurrent SSE streams wedge every
  other request through the BFF, forever")** — asked for explicitly:

  **Same root cause, genuinely distinct vectors — not one defect filed twice.** Both bottom out in
  the identical defect: one bounded `maxSockets:128` agent shared between long-lived proxied
  requests and short ones, with a silent unbounded queue behind it. But:
  - The *correctness* finding is a **capacity ceiling with no attacker** — legitimate
    `GET …/events` SSE streams (3 per tab, `MAX_JOB_STREAMS=3`), ~43 normal tabs, no malice, the
    system wedges itself under ordinary load.
  - The *security* finding is **adversarial and unauthenticated** — deliberate slow-body on
    body-reading POST routes, held indefinitely with negligible resources, no login.

  The security finding *explicitly cross-references* the SSE ceiling ("the same mechanism is also a
  plain capacity ceiling with no attacker … 43 concurrent tabs stall the entire deployment"), so the
  reporter already flagged the shared cause. The core fix is common (separate the long-lived pool
  from the short one, or raise+configure `maxSockets` and shed past a cap), but the security finding
  adds two mitigations the correctness one does not: a real `server.requestTimeout` and a
  `server.maxConnections` bound. Fixing the shared pool closes both; neither should be dropped as a
  duplicate of the other, because a pool-separation change alone still leaves the slow-body receipt
  unbounded.
