# Verdicts — `api/auth.py` security & hardening, reproduction lens

Scope: only finding **1** is `high`. Findings 2–5 are `medium`/`low` and are out of scope; no
verdict is recorded for them.

Working tree check: `diff -r --brief` of `src/chemclaw/api` against the pristine `HEAD` copy shows
only `__pycache__`. No mutation contamination; everything below is against unmodified source.

---

## 1. An unauthenticated caller drives one outbound request to the tenant IdP per HTTP request, and starves the process-wide thread executor while doing it

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

I did not run the reporter's probes. Four scripts of my own, all under
`/tmp/claude-0/…/scratchpad/`, driving the real `chemclaw.api.auth` against a local
`http.server` / raw socket standing in for the tenant JWKS.

**A — outbound fetch count (`v_probe_a.py`).** Counts requests hitting the fake JWKS while 100
copies of the reporter's 69-byte hand-typed token go through the real `validate_token`.

```
token: eyJhbGciOiAiUlMyNTYiLCAia2lkIjogImFueXRoaW5nIn0.eyJvaWQiOiAieCJ9.AAAA len 69
header parses to: {'alg': 'RS256', 'kid': 'anything'}
MODE=500  100 requests -> outbound JWKS fetches=100  outcomes={'IdentityProviderUnavailable': 100}  0.05s
cooldown setting = 60.0
MODE=200-empty  100 requests -> outbound fetches=1  outcomes={'PyJWKSetError': 100}
```

**B — executor starvation (`v_probe_b.py`).** A real `FastAPI` route with
`Depends(require_principal)`, driven over ASGI by `httpx`, against a socket server that accepts and
never answers; `entra_http_timeout_seconds = 10`.

```
cpu_count = 4  default executor max_workers = 8
baseline unrelated asyncio.to_thread latency: 1.40 ms
same call with 64 unauthenticated bearer tokens in flight: 79.08 s
regression factor: 56598x
attacker outcomes: {503: 64}
sockets accepted by the fake IdP: 64
```

**C — the same flood against a *healthy* JWKS (`v_probe_c.py`)**, real RSA key set, HTTP 200:

```
HEALTHY IdP: baseline 1.49 ms -> under 64-request flood 28.77 ms
outbound JWKS fetches for the whole flood: 6
attacker outcomes: {401: 64}
```

**D — cold cache with a realistic 200 ms IdP RTT, 64 concurrent (`v_probe_d.py`):**

```
HEALTHY-but-200ms IdP, COLD cache: baseline 1.30 ms -> under 64-request flood 26.20 ms
outbound JWKS fetches for the whole flood: 9
```

Supporting reads/greps:

- `jwt` 2.13.0, `.venv/lib/python3.11/site-packages/jwt/jwks_client.py:129-135` — the cache is
  written only after a successful `urlopen`; the reporter's cited `127-131` is off by two lines but
  names the right code.
- `grep -rn "client.host\|X-Forwarded-For\|remote_addr\|X-Real-IP" src/ --include=*.py` → **no
  matches**. `grep -rn "enforce_request_budget\|RequestLimiter(" src/` → the only enforcement call
  site is `auth.py:256`, inside `_within_budget`, i.e. after validation.
- `grep -rn "set_default_executor" src/` → none, so `asyncio.to_thread` really does use the
  process-wide default executor.
- Every co-tenant of that executor the finding names is real and current:
  `retrieval/retrievers.py:377`, `agent/graph_tools.py:94/166/203`,
  `science/fingerprints/molfp/search.py:171`, `ingest/eln/warehouse/retriever.py:143`. They are
  in-process with the front door (`api/app.py` and `api/routes/notes.py` both import `graph_tools`;
  `api/runner.py` runs the LangGraph agent in the API process), so this is not a cross-process
  claim. `deploy/entrypoint.sh:49` does set `--limit-concurrency 256`.
- No admission control upstream either: `api/middleware.py` adds only a body-size cap, security
  headers and CORS, and `deploy/helm/chemclaw/templates/service-route.yaml` is a plain Route with
  balancing annotations and an *empty-by-default* `ip_whitelist` — no oauth-proxy, no rate-limit
  annotation. Unauthenticated traffic reaches the pod.
- Cited line numbers in the finding (`auth.py:132`, `:226`, `:240-265`, the comments at `:126-127`
  and `:71-77`) are all accurate against current `HEAD`.

### Why

The mechanism is real and I reproduced **both** halves, one of them harder than the reporter did.
100 credential-less requests really do cost 100 outbound fetches while the JWKS endpoint is
erroring, because PyJWT writes its cache only on success and `_signing_key:132` calls
`get_signing_keys()` outside the cooldown that guards line 137. And 64 credential-less requests
against a hanging IdP really do jam the shared default executor: my unrelated `asyncio.to_thread`
call went 1.40 ms → **79.08 s** (the reporter measured 9.01 s — mine is ~9× worse, because 64
requests over 8 workers at a 10 s timeout is eight FIFO rounds, not one). Nothing meters that
traffic; `_within_budget` is unreachable until validation returns.

So why not CONFIRMED. Four things in the finding do not survive re-derivation, and together they
change what the defect is:

1. **The headline is unconditional and the behaviour is not.** Probe C is the normal state of the
   world — a healthy tenant — and there the code does exactly what it was designed to do: 64
   unauthenticated requests cost **6** outbound fetches and 28.77 ms of executor delay, not 64 and
   not 79 s. Both consequences require the JWKS endpoint to be *failing* (leg a) or *hanging*
   (leg b). "one outbound request to the tenant IdP per HTTP request" is true only during an IdP
   outage, and the title states it as the steady state.
2. **"or simply a cold process" is false.** Probe A, MODE=200: 100 requests on a cold process →
   **1** fetch. Probe D, 64 concurrent on a cold cache with a 200 ms RTT → **9** fetches, and the
   herd is bounded precisely by the 8-worker executor the finding complains about. Cold start is
   not a trigger.
3. **The reporter's own transcript is wrong about the outcome.** They print
   `attacker outcomes: {'401'}` for the stalling case and build on it ("hide a dependency failure
   inside a metric operators read as 'someone is probing us'"). I get **503 for all 64** — a stalled
   fetch raises `TimeoutError` → `PyJWKClientConnectionError` → `IdentityProviderUnavailable` →
   503, with `logger.warning("identity provider unavailable: …")` on every one. The 401-vs-503
   distinction the module was built for holds on exactly this path.
4. **"Amplification" is not what I measured.** The outbound ratio is 1 request in → 1 request out,
   not multiplicative; a pod capped at 256 concurrent connections is not a retry storm against
   `login.microsoftonline.com`. The genuine asymmetry is the *thread-hold* — 70 bytes buys up to
   `entra_http_timeout_seconds` of a shared worker — and that is leg (b), not leg (a).

What is left after those subtractions is still a real hardening defect, and I would not dismiss it:
**pre-auth, unmetered, unbounded-concurrency blocking network I/O on the process's shared executor,
with no per-peer admission control anywhere in the stack.** Its consequence is that a dependency
blip escalates — in-flight authenticated SSE turns and every KG/embedding/fingerprint worker call
in the pod stall for tens of seconds while anonymous traffic holds the threads, and the pod
multiplies its own retries against the IdP at a moment when being throttled would prolong the
outage. The reporter's fix (2) — a dedicated bounded executor for validation — is the right two
lines and removes the coupling on its own. But this is a resilience defect that needs a prior
outage to bite, not a standalone high: at the moment it triggers, the front door is already
answering 503 to everyone.

One thing the reporter did not note that cuts the other way, in the finding's favour: the 8-worker
executor is simultaneously the vulnerability and the only single-flight there is (probes C and D
show the cold-start herd capped by it). Giving validation its own dedicated pool, as fix (2)
proposes, *widens* the outbound fetch concurrency unless fix (1)'s negative cache lands with it.
The two fixes are not independent and should not be taken one at a time.
