# Round 1 — `api/auth.py`, `api/rate_limit.py`, `api/budget.py` — security & hardening

Five findings. All are in `auth.py`; `rate_limit.py` and `budget.py` held up under this lens
(details in "What I checked and found sound", at the end) — but the *absence* of any pre-auth
admission control is the reason finding 1 bites, so those two files are implicated by omission
rather than by a defect.

All scripts below were run under `uv run` in this sandbox. Verbatim output is quoted.

---

## 1. An unauthenticated caller drives one outbound request to the tenant IdP per HTTP request, and starves the process-wide thread executor while doing it

- **Severity**: high
- **Location**:
  - `src/chemclaw/api/auth.py:132` (`_signing_key`, the un-cooled `client.get_signing_keys()` call)
  - `src/chemclaw/api/auth.py:226` (`require_principal`, `asyncio.to_thread(validate_token, …)`)
  - `src/chemclaw/api/auth.py:240-265` (`_within_budget`, which runs *only after* validation succeeds)

### Trigger

70 bytes of hand-typed text. No signing key, no crypto, no credential:

```
eyJhbGciOiAiUlMyNTYiLCAia2lkIjogImFueXRoaW5nIn0.eyJvaWQiOiAieCJ9.AAAA
```

sent as `Authorization: Bearer <that>` to any gated route, repeatedly, while the tenant JWKS
endpoint is anything other than fully healthy (HTTP error, TLS failure, timeout, or simply a cold
process).

### Consequence

Two distinct amplifications, both from traffic that carries no credential and is never rate limited.

**(a) One outbound fetch to `login.microsoftonline.com` per request.** PyJWT's `fetch_data` caches
the JWKS **only on a successful fetch** (`jwt/jwks_client.py:127-131`). `_signing_key` line 132 calls
`client.get_signing_keys()` unconditionally, and that call is *not* covered by
`_forced_refresh_allowed` — the cooldown only guards the second-tier `client.get_signing_key(kid)`
path on line 137, which is reached only after line 132 has already succeeded. So while the IdP is
degraded, every unauthenticated request is a fresh outbound request to it. The front door turns an
IdP blip into a retry storm against the IdP, and the volume is chosen by an anonymous caller.

Measured (`/tmp/probe5.py`, JWKS endpoint returning HTTP 500):

```
token: eyJhbGciOiAiUlMyNTYiLCAia2lkIjogImFueXRoaW5nIn0.eyJvaWQiOiAieCJ9.AAAA
100 hand-typed unsigned tokens -> 100 outbound JWKS fetches (IdentityProviderUnavailable), 0.09s
```

and again in `/tmp/jwks_probe.py`, case C:

```
=== C: JWKS endpoint returns 500 -> nothing is cached; 50 unauth requests ===
  result=IdentityProviderUnavailable outbound_fetches=50  (cooldown=60.0)
```

**(b) Starvation of the process's shared default thread executor.** `require_principal` dispatches
`validate_token` through `asyncio.to_thread`, which uses the loop's *default* executor
(`min(32, cpu+4)` workers). Every other `asyncio.to_thread` caller in this process shares it —
`retrieval/retrievers.py:377` (`embed_texts`), `agent/graph_tools.py:94/166/203` (`build_graph`),
`science/fingerprints/molfp/search.py:171`, `ingest/eln/warehouse/retriever.py:143`. A slow JWKS
fetch (bounded at `entra_http_timeout_seconds`, default 10 s) holds one of those workers for its
whole duration.

Measured (`/tmp/probe2.py` original, JWKS endpoint accepting the connection and stalling 10 s):

```
### PROBE 4 — pre-auth work starves the process-wide default thread executor
  default executor max_workers = 8
  baseline unrelated asyncio.to_thread latency: 0.50 ms
  same call with 64 unauthenticated bearer tokens in flight: 9.01 s
  attacker outcomes: {'401'}; no rate limit was consulted for any of them
```

0.50 ms → 9.01 s, an 18,000× regression on every unrelated in-process worker call, from 64
unauthenticated requests. `deploy/entrypoint.sh:49` sets `--limit-concurrency 256`, so 256 is the
real per-pod ceiling on concurrent attackers, against 6-8 executor workers.

### Evidence — the two comments this contradicts

`auth.py:126-127` asserts the network is protected by structural validation:

```python
# Raises `DecodeError` (an `InvalidTokenError`) on a malformed token, which `validate_token`
# already turns into a 401 — so garbage never reaches the network at all.
kid = jwt.get_unverified_header(token).get("kid")
```

The token above is *structurally* well-formed and semantically garbage. It reaches the network 100
times out of 100.

`auth.py:71-77` presents the cooldown as the fix for exactly this failure:

```python
# When an unknown `kid` was last allowed to force a JWKS re-fetch, per endpoint. Caching the client
# — the earlier fix above — bounds the *warm* path but not this one: … That made one credential-less
# request cost one outbound fetch to the tenant IdP, and stalled the shared validation thread pool
# while it ran.
```

It bounds the warm path only. The stated failure ("one credential-less request cost one outbound
fetch … and stalled the shared validation thread pool") is still exactly what happens whenever the
cache is cold or the fetch is failing — reproduced above at 100/100.

Finally, `_within_budget`'s docstring (`auth.py:249-253`) argues the limiter must run *after*
validation, "never before". That argument is the vulnerability: it makes the cheapest-to-reach and
most-expensive-per-request path the only completely unmetered one. There is no per-IP limit anywhere
in `src/` (`grep -rn "client.host\|X-Forwarded-For\|remote_addr" src/` returns nothing).

### Fix

1. Put the outbound fetch behind the same cooldown as the forced refresh, not just the
   `refresh=True` leg — i.e. gate *entry to `_signing_key`'s network path* on a per-endpoint
   token bucket, and serve `IdentityProviderUnavailable` from a short negative cache while it is
   spent. That turns N unauthenticated requests during an IdP outage into ≤1 outbound fetch per
   cooldown instead of N.
2. Give validation a **dedicated, bounded** executor
   (`ThreadPoolExecutor(max_workers=k, thread_name_prefix="jwks")` +
   `loop.run_in_executor(that, …)`) so a stalled IdP can never contend with retrieval, KG loads or
   fingerprint scans. This is a two-line change and removes the coupling entirely.
3. Add a cheap pre-auth admission control keyed on the connection peer (or the ingress-supplied
   forwarded address), *before* `asyncio.to_thread`. `RequestLimiter` is already the right shape;
   it just needs a second instance and an earlier call site.

---

## 2. An unauthenticated request denies a legitimate Entra key rotation: valid tokens get 401 for the whole cooldown

- **Severity**: medium
- **Location**: `src/chemclaw/api/auth.py:98-108` (`_forced_refresh_allowed`), consumed at `auth.py:135-136`

### Trigger

1. The process has a warm JWKS cache containing key `old`.
2. An anonymous caller sends one token with `kid: "garbage-kid-0000"` (no valid signature needed —
   the header is read before any verification). This consumes the single per-cooldown forced-refresh
   allowance.
3. The tenant rotates its signing key; the JWKS now publishes `new`.
4. A real user presents a correctly signed, in-date, correct-audience token with `kid: "new"`.

### Consequence

Step 4 returns **HTTP 401 "invalid or expired token"** — for a perfectly valid credential — for up
to `entra_jwks_refresh_cooldown_seconds` (60 s default). Repeating step 2 once per cooldown window
keeps re-arming it. The user-visible failure is indistinguishable from a bad token, so it will be
diagnosed as a client problem.

### Evidence

`/tmp/probe3.py` (original), same code path, only the attacker request differs between the two runs:

```
CONTROL (no attacker) : legitimate token on the new key -> VALID (200)
ATTACKER first        : legitimate token on the new key -> 401 -> no signing key matches kid 'new' (refresh on cooldown)
```

The docstring on `_forced_refresh_allowed` claims the opposite property:

```python
"""Whether an unknown `kid` may pay for a JWKS re-fetch — at most once per cooldown.

Records the attempt when it grants one, so the *first* caller to hit a genuinely rotated key
pays the fetch and every later caller reads the refreshed cache.
"""
```

The first caller to hit *any* unknown kid pays the fetch and burns the allowance. A genuinely
rotated key is then refused. The config comment at `core/config/entra.py:82-84` makes the same
wrong claim in reverse ("a genuinely new signing key is picked up after at most this long").

### Fix

Make the cooldown per-`kid`, not per-endpoint: keep a small bounded set of kids already proven
absent from the current key set, refuse those without cost, and let a *new* unseen kid pay for one
refresh. That keeps the anti-amplification property (a flood of random kids is refused after the
first sighting of each, and the set is size-capped) while never letting a bogus kid block a real
one. Alternatively, invert the state: record the last *successful* refresh time and reset the
cooldown on any refresh that actually changed the key set.

---

## 3. Three exception classes escape every handler in `_signing_key`/`validate_token` and surface as HTTP 500

- **Severity**: medium
- **Location**: `src/chemclaw/api/auth.py:138-144` (the two `except` clauses) and `auth.py:165` (`except jwt.InvalidTokenError`)

### Trigger

The configured JWKS endpoint returns HTTP 200 with a JSON object that PyJWT cannot turn into a
usable key set. Any of:

- `{"keys": []}` — an empty key set
- `{}` — a JSON object with no `keys` member (an error envelope, a proxy/captive-portal page, a
  misconfigured `CHEMCLAW_ENTRA_JWKS_URL`)
- a key set whose entries all have an unsupported `kty`
- a key set with a malformed RSA modulus

### Consequence

`PyJWKSetError` and `PyJWKError` are **siblings** of `PyJWKClientError`, not subclasses, and none of
them is an `InvalidTokenError`:

```
PyJWKSetError MRO: ['PyJWKSetError', 'PyJWTError', 'Exception', 'BaseException', 'object']
subclass of PyJWKClientError: False
subclass of InvalidTokenError: False
```

A malformed modulus escapes as a bare `cryptography` `ValueError`. All four escape
`require_principal` entirely, so every request answers **500 Internal Server Error** instead of the
503 that `IdentityProviderUnavailable` exists to produce — and the deliberate 401-vs-503 distinction
(`auth.py:57-63`, "answering 401 would … hide a dependency failure inside a metric operators read as
'someone is probing us'") is bypassed in the *other* direction. `_jwks_clients` caches the client
and PyJWT caches the bad body for its 300 s lifespan, so the 500 persists rather than self-healing
per request.

### Evidence

`/tmp/probe2.py` (original), PROBE 1:

```
### PROBE 1 — JWKS documents that make PyJWT raise outside every handler
  empty key set  {'keys': []}        -> UNHANDLED->500: PyJWKSetError: The JWK Set did not contain any keys
  no 'keys' member  {}               -> UNHANDLED->500: PyJWKSetError: The JWK Set did not contain any keys
  all keys unusable kty              -> UNHANDLED->500: PyJWKSetError: The JWK Set did not contain any usable keys. Perhaps 'cryptography' is not installed?
  malformed RSA key                  -> UNHANDLED->500: ValueError: n must be >= 3.
```

The comment on the handler that *was* fixed makes the incompleteness explicit and self-refuting:

```python
except PyJWKClientError as exc:
    # Not an `InvalidTokenError` — this is the class that used to escape every handler here
    # and surface as a 500.
```

One of four siblings was caught; the reasoning that justified catching it applies unchanged to the
other three.

### Fix

Catch `PyJWTError` (the common base of all four PyJWT classes) plus `ValueError` in `_signing_key`
and map them to `IdentityProviderUnavailable` — an unparseable key set is our dependency's problem,
not the caller's credential, which is precisely the distinction `IdentityProviderUnavailable`'s own
docstring draws:

```python
except PyJWKClientConnectionError as exc:
    raise IdentityProviderUnavailable(...) from exc
except PyJWKClientError as exc:
    raise AuthError(...) from exc
except (PyJWTError, ValueError) as exc:          # PyJWKSetError, PyJWKError, cryptography
    raise IdentityProviderUnavailable(f"tenant JWKS is not a usable key set: {exc}") from exc
```

---

## 4. Non-string identity claims escape as HTTP 500 instead of 401

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:181-184`, `auth.py:206` (`_principal_from_claims`)

### Trigger

A token that validates (correct signature, audience, issuer, `exp`) whose `oid` claim is not a JSON
string, or whose `roles` claim is not iterable. Reachable when the IdP is a non-Entra OIDC provider,
a test/staging stub, or a mock (`Chemclaw3_mock` stands in for real integrations), or after any
change to `entra_issuer`/`entra_jwks_url` that points at something other than Entra.

### Consequence

`Principal(oid=oid)` raises pydantic `ValidationError`; `list(claims.get("roles", []))` raises
`TypeError`. Neither is an `AuthError`, so both escape `require_principal` (lines 227-236) as a 500
rather than the 401 the module's own contract promises for "missing identity" (`AuthError`'s
docstring, `auth.py:53-54`, names exactly this case).

### Evidence

`/tmp/probe4.py`:

```
### claim shapes that escape AuthError and become HTTP 500
  oid is a JSON number     -> UNHANDLED -> 500: ValidationError
  roles is not a list      -> UNHANDLED -> 500: TypeError
  oid is a JSON object     -> UNHANDLED -> 500: ValidationError
```

### Fix

Validate the claim shapes explicitly in `_principal_from_claims`, raising `AuthError`:

```python
oid = claims.get("oid")
if not isinstance(oid, str) or not oid:
    raise AuthError("token has no usable 'oid' claim")
raw_roles = claims.get("roles", [])
if not isinstance(raw_roles, list):
    raise AuthError("token 'roles' claim is not a list")
entitlements = [r for r in raw_roles if isinstance(r, str)]
```

The same guard applies to `claims.get("groups", [])` at line 205.

---

## 5. `_client_for` freezes `entra_http_timeout_seconds` at first use, contradicting its own comment, and leaks a client per endpoint change

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:66-85` (`_jwks_clients`, `_client_for`)

### Trigger

Change `CHEMCLAW_ENTRA_HTTP_TIMEOUT_SECONDS` (or change it in a test/runtime settings override)
after the first token has been validated.

### Consequence

The new timeout is never applied — the cached `PyJWKClient` keeps the value captured at
construction. `core/config/entra.py:72-73` states that `entra_http_timeout_seconds` "also bounds the
front door's JWKS fetch, which is a live reader"; it is not a live reader. This matters for
finding 1's mitigation: an operator lowering the timeout to shorten a stall-under-load will see no
effect without a pod restart. Additionally, `_jwks_clients` never evicts, so each distinct endpoint
value leaves its client (and its cached key set) resident for the process lifetime.

### Evidence

`/tmp/probe3.py` (rewritten):

```
first build: timeout = 5.0
after raising config to 30.0: timeout = 5.0 | same object: True
cached clients after endpoint change: 2 (old one never freed)
```

The comment being contradicted:

```python
# One JWKS client per endpoint, cached: … Keyed by endpoint so a config change is still picked up.
_jwks_clients: dict[str, PyJWKClient] = {}
```

The endpoint change is picked up; the timeout change is not, and the stale client is retained.

### Fix

Key the cache on `(endpoint, timeout)` and hold at most one entry (replace rather than accumulate) —
the deployment has exactly one JWKS endpoint at a time, so a one-slot cache is both correct and the
smallest change:

```python
_jwks_client: tuple[tuple[str, float], PyJWKClient] | None = None

def _client_for(endpoint: str) -> PyJWKClient:
    global _jwks_client
    key = (endpoint, settings.entra_http_timeout_seconds)
    if _jwks_client is None or _jwks_client[0] != key:
        _jwks_client = (key, PyJWKClient(endpoint, timeout=key[1]))
    return _jwks_client[1]
```

---

## What I checked and found sound (no finding)

Recording these so a later round does not re-spend the time.

**`validate_token` fails closed on every path I could construct.** RS256 is pinned (no `alg`
confusion, no `none`); `audience` and `issuer` are both verified; `options={"require": ["exp"]}`
merges with PyJWT's defaults rather than replacing them (verified against
`jwt/api_jwt.py` — `{**self.options, **options}`), so `verify_signature`/`verify_aud`/`verify_exp`
stay on. An empty `entra_audience` still rejects (PyJWT takes the "audience specified" branch on
`""`), and `Settings._entra_enforcement_is_configured` refuses that config at startup anyway.
`_signing_key` reaches the network *before* signature verification, which is finding 1, but it never
returns a key an attacker chose: `jku`/`x5u` are ignored, and PyJWT's own constructor rejects
non-`http(s)` JWKS schemes.

**No alternate path bypasses the gate.** `validate_token` and `require_principal` have exactly the
call sites in `auth.py` and `deps.py` (`grep -rn "validate_token\|require_principal" src/`);
`openapi_url=None`/`docs_url=None` close the ungatable `Route`s; the only `Mount` is
`src/chemclaw/api/static/` (two files, `app.js` + `index.html`, no data), and Starlette's
`StaticFiles` handles traversal itself.

**`rate_limit.py`.** The token bucket is monotonic-clocked, the LRU bound is real, `per_minute <= 0`
is rejected at construction and short-circuited at the call site, `chemclaw_requests_rate_limited_total`
carries no attacker-influenced labels, and the only value logged is the Entra `oid` (a pseudonymous
GUID, not PII, not attacker-chosen under enforcement). `RequestLimiter` holds no lock while
`BoundedLru`'s module docstring warns it is not thread-safe — but every call site
(`_within_budget` ← `require_principal`, an `async def`) runs on the event loop, so there is no
concurrent access today. Latent, not a defect. `Retry-After` uses `max(1, int(x + 0.999))` rather
than `math.ceil`, which under-reports by one second whenever the fractional part is under 0.001
(measured: `2.0005 → 2`, ceil `3`); the consequence is one wasted early retry, so I am not raising it
as a finding.

**`budget.py`.** `check`/`record` are both under the lock; the check/record gap is bounded by the
admission semaphore as documented and as `turns.py:140/217` implements (the check *after* the
permit is the binding one); `_over` treating 0 as unlimited matches the `ge=0` config bounds; both
counter maps are LRU-bounded. The `session_id` reaching `budget.check` is the same path parameter the
`CurrentSession` dependency has already ownership-checked before the handler body runs, so there is
no cross-tenant read of another user's counter. `BudgetExceeded`'s message reaches the client
verbatim (`turns.py:143`, `turns.py:219`) but discloses only the caller's own scope and count.
Session-scoped budget is trivially reset by opening a new session — that is what the user-scoped cap
exists for, and resetting the *user* counter requires evicting it, which needs
`budget_max_tracked_users` (10,000) distinct principals a single caller cannot mint.
