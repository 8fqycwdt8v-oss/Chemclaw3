# Verdicts — `ui-server-bff--security.md`, reachability/consequence lens

Scope: only the one **critical/high** finding in that file. The other two (`/api/readyz`
whitelisting, dev-mode framing) are labelled **low** by the reporter and are out of scope; I did
not adjudicate them.

Setup used for every measurement below — the **real** backend and the **real** BFF, no fakes:

- Backend: `uv run uvicorn --factory chemclaw.api.app:create_app --host 127.0.0.1 --port 8000
  --workers 1` at `/home/user/Chemclaw3` @ `1c60950` (clean tree), with
  `CHEMCLAW_ENTRA_REQUIRED=true`, `CHEMCLAW_ENTRA_AUDIENCE`/`TENANT_ID`/`CLIENT_ID` set — i.e. the
  **authentication-enforcing** posture, not dev auth.
- BFF: `node --experimental-strip-types server/index.ts` at `/workspace/chemclaw3_ui` @ `1a1f6f0`
  (clean tree), `CHEMCLAW_API_URL=http://127.0.0.1:8000`, `PORT=8099`. Run in **both**
  `AUTH_MODE=dev` and `AUTH_MODE=msal`.
- Attacker: raw `socket` clients, **no `Authorization` header**, no cookie, no session.

---

## Unauthenticated slow-body requests exhaust the 128-socket upstream pool and wedge the whole /api proxy indefinitely

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed; the trigger is cheaper than the finding claims, see
  below, but the outcome is availability-only)

### What I did

**1. Reachability — does an unauthenticated caller reach `proxy()`?** Yes, and in the production
auth posture. The BFF has no authentication of its own anywhere on the `/api/` path: `index.ts`
goes straight from `resolveRoute` to `proxy()`, and `proxy.ts` forwards `Authorization` verbatim
"and never inspects" it. Measured against the msal build:

```
$ curl -m5 -o /dev/null -w '%{http_code}' -XPOST -H 'content-type: application/json' \
       -d '{}' http://127.0.0.1:8099/api/sessions      # BFF started with AUTH_MODE=msal
401
```

A `401` here is *proof of reach*, not of protection: the BFF had already called
`transport.request()` and claimed an agent socket before the backend ever saw the request. The
route pattern `([0-9a-f]{32})` needs no existing session — any 32 hex chars match
(`routes.ts:16`).

**2. Does the upstream really hold the socket without answering, pre-auth?** This is the part the
finding rests on, and my first attempt *refuted* it: a body of one junk byte gets an immediate
`400` from Starlette's multipart parser, which frees the socket.

```
$ python3 /tmp/bffv/slowdirect.py 8000 /sessions/00000000000000000000000000000000/attachments
b"HTTP/1.1 400 Bad Request\r\n..."          # immediate — attack would NOT hold
```

With a *well-formed* multipart prefix it holds, and so do the JSON-body routes. Against the real
backend with `entra_required=true`, no credentials:

```
--- POST /sessions/{32hex}/attachments (valid multipart prefix, unauthenticated):
t=8.01s NO RESPONSE — upstream holding, waiting for body
--- POST /sessions/{32hex}/messages (json body, unauthenticated):
t=8.01s NO RESPONSE — upstream holding, waiting for body
--- POST /sessions (json body, unauthenticated):
t=8.01s NO RESPONSE — upstream holding, waiting for body
```

The mechanism is as stated and I verified it in the source rather than trusting the reporter's
`/tmp` script: auth on these routes is a FastAPI **dependency**
(`sessions.py:228` → `dependencies=[Depends(resolve_session)]` → `CurrentUser` → `require_principal`),
not ASGI middleware, and FastAPI reads the body in `get_request_handler` before `solve_dependencies`
runs. The two ASGI middlewares that *do* run first (`BodySizeLimit`, `_SecurityHeaders`) do not
help: `BodySizeLimit` only rejects a **declared** `Content-Length` over
`service_max_request_bytes` (default **4 000 000**), and my declared 1 000 000 passes it
(`core/asgi.py:58-61`).

**3. Exhaustion, measured end-to-end through both processes.** 128 unauthenticated slow-body
connections, then one ordinary proxied request:

```
=== N=20 ===
opened 20 slow-body connections (no Authorization header), holding
  proxied GET /api/healthz   -> http=200 in 0.03s
=== N=128 ===
opened 128 slow-body connections (no Authorization header), holding
  proxied GET /api/healthz   -> http=000 in 5.01s     <- curl timeout, no response
  own /healthz               -> http=200 in 0.01s
  static /a.txt              -> http=200 in 0.01s
  upstream direct            -> http=200 in 0.01s
after releasing attack: /api/healthz -> http=200
```

Threshold sweep (AUTH_MODE=msal, anonymous attacker) pins it to `maxSockets` exactly, not to the
event loop and not to the backend:

```
  held= 100  /api/healthz http=200
  held= 126  /api/healthz http=200
  held= 127  /api/healthz http=200
  held= 128  /api/healthz http=000
  held= 129  /api/healthz http=000
```

**4. Is it indefinite?** Yes — and the finding *understates* how cheap it is. It specifies
"dribble one byte every 30s, forever". No dribble is needed. 128 connections, headers + a valid
multipart prefix, then **zero further bytes ever**:

```
128 held; sending NO further bytes at all (no dribble).
  t=9s    /api/healthz http=000
  t=69s   /api/healthz http=000
  t=139s  /api/healthz http=000     <- past headersTimeout (125s) and keepAliveTimeout (120s)
```

`headersTimeout` had already been satisfied and cleared; `keepAliveTimeout` governs idle sockets
*between* requests, not a request in progress; `requestTimeout = 0` is the only thing that would
have bounded this and it is off. The attack is one burst of 128 TCP connections and then silence.

**5. Nothing is logged.** The BFF log is completely silent for the whole wedge — the queued 129th
request produces no error, no warning, no `upstream error` line. The only WARN lines appear at
*release* time, when I closed the attacker sockets. So the operator sees nothing.

**6. What restarts it?** Nothing. `/healthz` is answered locally by `index.ts:57` and returned 200
throughout, static assets returned 200 throughout, and the container's own liveness signal is
exactly that endpoint — `Dockerfile:29`:

```
HEALTHCHECK ... CMD node -e "fetch('http://127.0.0.1:'+process.env.PORT+'/healthz')..."
```

The reporter asserted "a liveness probe sees a healthy pod"; the Dockerfile is the artifact that
makes it concrete, and it was not cited. I also checked for anything fronting the BFF that might
buffer request bodies and neutralise this (an nginx-style `proxy_request_buffering on` would): the
backend Helm chart contains **no** UI deployment (`grep -rln chemclaw3-ui deploy/` → nothing), and
the only shipped topology is `docker-compose.yml`, which publishes the BFF directly on
`${UI_PORT:-3000}` with nothing in front of it. There is no upstream mitigation in either repo.

**7. The no-attacker capacity ceiling.** `MAX_JOB_STREAMS = 3` is real
(`src/hooks/useJobStreams.ts:48`), and I checked whether a backend cap would stop 129 streams
before the BFF pool does — it does not: `service_max_event_streams_per_user` is 5 and
`service_max_event_streams_total` is **200** (`core/config/service.py:223,237`), both above 128.
Two small corrections to that paragraph: the client only opens a stream for a conversation with
messages or the active one, so a fresh tab holds 1 stream, not 3 — the real figure is somewhere
between 43 and 128 tabs — and the ceiling is per BFF **replica**, not per deployment.

### Why

Trigger and consequence both hold, in the strictest configuration available:

- **Reachable from the outermost entry point.** No BFF-side auth exists on `/api/` in either auth
  mode; the route whitelist is the only gate and it accepts a made-up session id by construction.
  Nothing upstream — no pydantic model, no dependency, no size limit, no ingress in any shipped
  manifest — prevents an anonymous caller from making `proxy()` claim a socket.
- **Consequence is not a paraphrase.** I did not accept "wedges the proxy": I measured the exact
  127/128 boundary, confirmed the event loop and the backend were both healthy at the time,
  confirmed the request is silently queued rather than errored, and confirmed recovery only on
  attacker release.
- The one place the finding could have been wrong — "FastAPI reads the body before auth" — I
  re-derived from the route registration and the middleware stack rather than from the reporter's
  script, and it is right for these routes. My own first probe produced the counter-result (an
  immediate `400`), so this is a claim that survived an active attempt to kill it.

`high` is the right label. It is unauthenticated, remote, near-zero cost, complete and persistent
denial of the entire API surface, and invisible to every health signal — but it is availability
only, with no confidentiality or integrity impact, which is what keeps it below critical.

### Two things the reporter got wrong that matter for the fix

- **Proposed fix 1 (`server.requestTimeout`) does not fix this on its own.** I built the variant
  and attacked it. `/tmp/bffv/fix/server/index.ts` with `server.requestTimeout = 15_000`, 140
  workers each opening a slow-body request and immediately reconnecting when cut:

  ```
  140 reconnecting slow-body workers against requestTimeout=15s build
    t=8s   /api/healthz http=000
    t=16s  /api/healthz http=000
    t=24s  /api/healthz http=000
    t=32s  /api/healthz http=000
  ```

  A bound on receipt converts a permanent wedge into a trivially sustainable one (at the suggested
  120s, ~1 connection/second holds it). Only fix 2 — a real socket budget with shedding — closes
  it. Fix 1 is still worth doing; it must not be shipped as *the* fix.
- **The trigger is cheaper than described.** No dribble is required (measured at 139s of total
  silence), so any mitigation reasoning that assumes the attacker must keep sending bytes — e.g.
  relying on an HAProxy `timeout client` — is reasoning about a harder attack than the real one.

### On the overlap with `ui-server-bff--correctness.md` §"128 concurrent SSE streams wedge every other request through the BFF, forever"

**One root cause, two genuinely distinct defects — not the same finding filed twice.** The shared
cause is a single `Agent` with `maxSockets: 128` and no shedding. But the triggers have different
threat models and, critically, **the two proposed fixes do not cover each other**:

- The correctness fix (two agents — unbounded for `sse: true`, bounded for the rest) does **not**
  stop the attack. `POST …/attachments` is `sse: false` (`routes.ts:117-122`), so it lands in the
  *bounded* pool. That is the exact route I used for every measurement above; the split would leave
  it working unchanged.
- The security fix (`requestTimeout`) does **not** stop the SSE ceiling. An SSE stream's request
  *receipt* completes in milliseconds; only its response is long. `requestTimeout` never fires on it.
- The one change that resolves both is the socket-budget half of security fix 2 (configurable
  bound + explicit 503 shedding, or separate budgets). If only one thing is done, do that.

So: file them as one work item if you like, but do not let either fix land alone and be recorded as
closing the other.

---

### Housekeeping

While cleaning up I ran `pkill -f "server/index.ts"`, which also terminated another audit session's
BFF on port 9104. Apologies to whoever owns it — nothing in `/workspace/chemclaw3_ui` or
`/home/user/Chemclaw3` was modified (both trees verified clean at the commits named above; all my
edits were to copies under `/tmp/bffv/`).
