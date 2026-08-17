# api/auth.py, api/rate_limit.py, api/budget.py — CORRECTNESS

Four findings, all reproduced with runnable scripts against the live venv. Scripts are under
`/tmp/audit/` (`repro_rotation.py`, `repro_expiry_and_blip.py`, `repro_burst.py`).

Claims I checked and found **true**, recorded so the next reviewer does not re-do them:

- `_within_budget`'s 429 + `Retry-After` works end to end, and `/healthz` is genuinely unlimited
  (`repro_e2e_limit.py`: `GET /jobs` 200, 200, 429 `Retry-After: 1`, 429; three `/healthz` all 200).
- The `Retry-After` arithmetic `max(1, int(retry_after + 0.999))` never under-waits: for
  rate `r` and deficit `d`, `ceil(d/r)` seconds always refills at least one whole token.
- `budget.py`'s boundary claim ("a cap of 100 allows 100 turns, refuses no. 101") is exactly what
  `_over(cap, used) = cap > 0 and used >= cap` does.
- `budget.py`'s claim that `routes/turns.py:post_message` re-checks the budget *after* taking an
  admission permit is true (`routes/turns.py:140`, inside the `permit = True` block).
- `PyJWKClientError` is genuinely **not** a subclass of `jwt.InvalidTokenError` (checked in
  `jwt/exceptions.py`), so the comment at `auth.py:141-143` about which class used to escape as a
  500 is accurate, and the ordering comment at `auth.py:138` is right
  (`PyJWKClientConnectionError` ⊂ `PyJWKClientError`).
- I could **not** reproduce a race in `_forced_refresh_allowed` itself: 40 concurrent unknown-`kid`
  validations through `asyncio.to_thread` produced exactly **1** outbound fetch. The check-then-act
  window is real but too narrow to hit in practice. See finding 2 for the leg that *is* unbounded.

---

## A valid token is answered 401 for up to 60 s after a signing-key rotation, and an IdP outage arms the same window

- **Severity**: medium
- **Location**: `src/chemclaw/api/auth.py:98-108` (`_forced_refresh_allowed`), `:131-144` (`_signing_key`)
- **Trigger**: Any single request carrying a `kid` that is not in the cached JWK set arms a
  process-wide, per-endpoint cooldown (`entra_jwks_refresh_cooldown_seconds`, default **60 s**).
  Any *legitimately valid* token whose `kid` is new — i.e. every token minted after the tenant
  rotates its signing key — that arrives inside that window is refused. Two concrete sequences,
  both reproduced:

  1. `t=0` a stale client (or an unauthenticated probe) sends `kid="Z"` → cooldown armed.
     `t=1` the tenant rotates and starts signing with key `B`. `t=2` a real user's valid token
     signed by `B` → `AuthError`.
  2. `t=0` the tenant JWKS is briefly unreachable and a request with an unknown `kid` arrives.
     `_forced_refresh_allowed` writes the timestamp **before** the fetch, the fetch then fails with
     `PyJWKClientConnectionError` → correctly a 503 — but the cooldown is now armed *without a
     single successful key lookup having happened*. The IdP recovers, having rotated. A valid token
     on the new key → `AuthError`.

- **Consequence**: `require_principal` maps `AuthError` to `HTTP 401 "invalid or expired token"`.
  Users holding perfectly good credentials are told their credential is invalid, for up to the
  cooldown, on every request. This directly contradicts the rule this module states for itself at
  `auth.py:57-63`: *"answering 401 would tell a user with a perfectly good token that it was
  rejected, and would hide a dependency failure"* — which is exactly what sequence 2 does, and it
  does it for a reason that is entirely ours. `entra_jwks_refresh_cooldown_seconds`'s config comment
  acknowledges "rotation latency"; it does not acknowledge that the latency is served as a
  caller-blaming 401, nor that a *failed* refresh consumes the window.

- **Evidence**: `/tmp/audit/repro_rotation.py` — local JWKS server, real `chemclaw.api.auth`:

  ```
  cooldown = 60.0 s
  1. token signed by A -> user-1 | fetches so far: 1
  2. unknown kid 'Z' -> AuthError no signing key matches kid 'Z': Unable to find a signing key that matches: "Z" | fetches: 2
  4. token signed by B -> AuthError no signing key matches kid 'B' (refresh on cooldown)
     require_principal maps this to HTTP 401 'invalid or expired token'
  ```

  `/tmp/audit/repro_expiry_and_blip.py`, sequence 2 (IdP blip):

  ```
  D. during IdP blip, unknown kid -> IdentityProviderUnavailable (mapped to HTTP 503)
  E. valid token on new key B -> AuthError no signing key matches kid 'B' (refresh on cooldown) -> HTTP 401
  ```

  The code that proves it — the timestamp is written on the *attempt*, and the refusal is an
  `AuthError` regardless of why the key is missing:

  ```python
  def _forced_refresh_allowed(endpoint: str, now: float) -> bool:
      last = _last_forced_refresh.get(endpoint)
      if last is not None and now - last < settings.entra_jwks_refresh_cooldown_seconds:
          return False
      _last_forced_refresh[endpoint] = now   # armed before the fetch, kept if the fetch fails
      return True
  ```

- **Fix**: two changes, both small.
  1. Move the timestamp write to *after* a refresh actually completes (success or a definite
     "key not found"), so a `PyJWKClientConnectionError` does not consume the window. Concretely,
     split it into `_forced_refresh_allowed(endpoint, now)` (read-only) and a
     `_record_forced_refresh(endpoint, now)` called in the branch that returns a key or raises
     `PyJWKClientError`.
  2. Raise `IdentityProviderUnavailable` (→ 503) rather than `AuthError` (→ 401) for the
     cooldown refusal. The service has *not decided* that the token is bad — it has declined to
     look. 503 is what this module's own docstring prescribes for "we could not reach the tenant
     to decide", and it makes the failure visible to operators instead of appearing in the
     "someone is probing us" 401 metric.

---

## N concurrent requests cost N outbound JWKS fetches whenever the key-set cache is cold or expired, saturating the 8-slot thread pool the rest of the process shares

- **Severity**: medium
- **Location**: `src/chemclaw/api/auth.py:132` (`client.get_signing_keys()` in `_signing_key`),
  `:226` (`await asyncio.to_thread(validate_token, ...)`)
- **Trigger**: Any burst of concurrent requests arriving while `PyJWKClient`'s JWK-set cache is
  empty (process start) or expired. The cache lifespan is PyJWT's default **300 s** —
  `_client_for` passes only `timeout`, never `lifespan` — so this recurs every five minutes for as
  long as the process runs. No valid credential is needed: `_signing_key` calls
  `client.get_signing_keys()` **before** any cooldown check, so a token that is complete garbage
  apart from having a `kid` header reaches the network path.
- **Consequence**: `PyJWKClient.fetch_data` has no in-flight de-duplication, so every one of the N
  threads performs its own blocking HTTP fetch to `login.microsoftonline.com`. Each is bounded by
  `entra_http_timeout_seconds` (10 s), and each occupies a slot in asyncio's **default** executor —
  `min(32, cpu_count+4)` = **8 slots on this 4-CPU pod**, shared with `agent/graph_tools.py`,
  `retrieval/retrievers.py`, `retrieval/vector_index.py` (embeddings), `agent/attachments.py` and
  `science/fingerprints/molfp/search.py`. Eight simultaneous cold validations therefore stall every
  KG build, embedding call and attachment write in the process for up to 10 s. `asyncio.to_thread`
  also cannot cancel the underlying thread, so a client that disconnects mid-fetch does not free
  its slot. This falsifies the property `_signing_key`'s own docstring asserts at `auth.py:120-122`:
  *"the caller must not be able to choose how much work we do about it."* On the cold/expired leg
  the caller chooses it exactly, one fetch per concurrent request.
- **Evidence**: `/tmp/audit/repro_expiry_and_blip.py`, counting requests that actually reach the
  JWKS HTTP server. `C` expires the cache the same way the 300 s lifespan does
  (`jwk_set_cache.jwk_set_with_timestamp.timestamp`):

  ```
  A. cold cache, 40 concurrent valid tokens        -> 40 JWKS fetches
  B. warm cache, 40 concurrent valid tokens        -> 0 JWKS fetches
  C. cache EXPIRED (300s lifespan), 40 concurrent  -> 40 JWKS fetches
  ```

  For contrast, the leg the cooldown *does* guard, same harness
  (`/tmp/audit/repro_concurrent.py`): `unknown kid 'Z' -> 1 outbound JWKS fetch` for 40 concurrent
  requests. So the guard works where it is applied and is simply not applied here.

- **Fix**: put the whole key resolution behind one process-wide `threading.Lock` (or a single
  dedicated single-thread executor) so that at most one thread fetches and the rest read the
  refreshed cache — the same "first caller pays, everyone else reads" shape `_forced_refresh_allowed`
  already documents for the unknown-`kid` leg. Cheapest correct version: guard the body of
  `_signing_key` from `get_signing_keys()` through the forced refresh with a module-level lock;
  the warm path is pure in-memory matching, so the lock is uncontended in the common case (leg `B`
  above: 0 fetches). Also pass `lifespan=` explicitly to `PyJWKClient` from config rather than
  inheriting PyJWT's 300 s default, since the code reasons about that number in comments but never
  sets it.

---

## A `service_rate_limit_burst` below 1 is schema-valid and permanently 429s every request from every principal

- **Severity**: medium
- **Location**: `src/chemclaw/api/rate_limit.py:73-108` (`RequestLimiter.__init__` / `check`),
  `src/chemclaw/core/config/service.py:148` (`service_rate_limit_burst: float = Field(default=30.0, gt=0)`)
- **Trigger**: `CHEMCLAW_SERVICE_RATE_LIMIT_BURST=0.5` (or any value in `(0, 1)`) with
  `service_rate_limit_per_minute > 0`. The field is a `float` with `gt=0`, so the schema accepts it
  and `RequestLimiter.__init__` constructs without complaint.
- **Consequence**: total, permanent denial of the entire authenticated API. A fresh bucket starts
  at `self._burst = 0.5`; refill is capped by `min(self._burst, ...)`, so `bucket.tokens` can never
  reach `1.0`, so `check` raises `RateLimited` on the first request and on every request
  thereafter, forever, for every principal. The 429 carries `Retry-After: 1`, which is a promise
  the limiter cannot keep — a client that honours it retries into an identical 429 indefinitely.
  This is precisely the failure class `__init__`'s own docstring says it validates against for the
  sibling parameter (*"A zero rate makes the retry-after arithmetic in `check` a division by
  zero — reached only after the bucket drains, so it would surface as a 500 on the `burst`-th
  request rather than at construction"*): `per_minute` is checked, the strictly worse `burst < 1`
  case is not.
- **Evidence**: `/tmp/audit/repro_burst.py`:

  ```
  schema accepts burst=0.5?  0.5
    t=      0.0s -> 429, Retry-After=1s
    t=      1.0s -> 429, Retry-After=1s
    t=     60.0s -> 429, Retry-After=1s
    t=   3600.0s -> 429, Retry-After=1s
    t=  86400.0s -> 429, Retry-After=1s
  compare: per_minute=0 is rejected at construction -> ValueError
  ```

- **Fix**: extend the existing guard in `RequestLimiter.__init__`:

  ```python
  if burst < 1.0:
      raise ValueError(f"burst must be >= 1, got {burst}; a bucket that cannot hold one token refuses every request")
  ```

  and tighten the config field to `Field(default=30.0, ge=1)` so the deployment fails at startup
  rather than at the first request.

---

## A non-list `roles` / `groups` claim is consumed without a type check: a string silently becomes a set of single characters, `null` is a 500

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:184` and `:205` (`_principal_from_claims`)
- **Trigger**: A validated token whose `roles` (or `groups`) claim is a JSON string rather than an
  array — e.g. a space- or comma-delimited `"roles"` value, which several OIDC providers emit and
  which this deployment can reach, because `entra_jwks_url` and `entra_issuer` are documented
  override fields (`core/config/entra.py:36-37`) that point the front door at a non-Entra IdP.
  Or a claim explicitly present as `null`.
- **Consequence**: no exception, no log, wrong answer. `list("admin")` yields
  `['a','d','m','i','n']`, so `Principal.roles` becomes `{'a','d','m','i','n'}`: the user loses the
  `admin` entitlement (every `entra_privileged_role_set` and `tool_role_gates` check they should
  pass now fails) and gains five bogus one-character entitlements that a mistyped gate could match.
  The `null` case is worse in a different direction: `list(None)` raises `TypeError`, which is
  neither an `AuthError` nor a `jwt.InvalidTokenError`, so it escapes both `except` blocks in
  `validate_token` and `require_principal` and surfaces as a **500** rather than a 401.
- **Evidence**:

  ```
  $ uv run python -c "from chemclaw.api.auth import _principal_from_claims; ..."
  {'oid': 'u', 'roles': None}    -> TypeError 'NoneType' object is not iterable
  {'oid': 'u', 'roles': 'admin'} -> ['a', 'd', 'i', 'm', 'n']
  ```

  The code:

  ```python
  entitlements = list(claims.get("roles", []))
  ...
  entitlements += [f"{GROUP_ROLE_PREFIX}{group}" for group in claims.get("groups", [])]
  ```

- **Fix**: one helper used for both claims — take the value, return `[]` for `None`/absent, wrap a
  bare `str` (or split it, if that is the intent — but decide explicitly rather than letting
  `list()` decide), and ignore any other type with a `logger.warning` naming the claim. That also
  removes the 500 path.
