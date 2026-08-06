# D-2026-08-06-the-caller-chooses-the-kid-not-the-workload — The caller chooses the `kid`, not how much work we do about it

**Status:** accepted · **Date:** 2026-08-06

## Context

A whole-codebase security sweep — eleven disjoint review lanes, each finding adversarially
re-checked by a second agent required to *execute* a repro rather than re-read the code — reported
this as the only HIGH on the front door. It reproduced on the first probe.

Everything below was measured. Prose is evidence about what its author believed.

## Decision

### 1. `PyJWKClientError` is not an `InvalidTokenError`, and it escaped every handler

`api/auth.py` caught `jwt.InvalidTokenError` in `validate_token` and `AuthError` in
`require_principal`. `PyJWKClientError`'s MRO is `(PyJWKClientError, PyJWTError, Exception)` — it
is neither. Two credential-free ways to raise it:

- a token whose `kid` is absent from the tenant JWKS;
- an unreachable JWKS endpoint (`PyJWKClientConnectionError`, a subclass).

Both surfaced as **HTTP 500**, breaking the generic-401 contract `tests/test_auth.py` asserts for
the malformed-token case and turning the whole auth path into 5xx during an event — which also
destroys any 5xx-based alerting exactly when it is needed.

The unknown `kid` is now an `AuthError` → 401. The unreachable JWKS is a **new**
`IdentityProviderUnavailable` → **503**, deliberately *not* an `AuthError`. A 401 there tells a user
holding a perfectly good token that it was rejected, and files a dependency outage under a metric
operators read as "someone is probing us".

### 2. The `kid` is attacker-controlled, so it must not decide how much work we do

`PyJWKClient.get_signing_key` retries with `refresh=True` on **every** `kid` miss. The `kid` comes
from the unverified header of an anonymous caller's token. So one credential-less request bought
one outbound request to the tenant IdP, each occupying a thread in the shared `asyncio.to_thread`
pool while it ran.

**Measured, by removing the fix and watching the test fail:** 50 anonymous unknown-`kid` tokens
produced **50** forced refreshes; with the cooldown, **1**. The verifier's independent probe, with a
2 s JWKS latency and 40 concurrent anonymous requests, measured a legitimate *warm-cache* request —
one needing no fetch at all, 0.0 s when idle — taking **9.46 s**, and all 40 attacker requests
returning 500 rather than being rate-limited.

An earlier review had already met this shape and cached the `PyJWKClient` for it; the comment
explaining that is still above the client map. It bounds the warm path and never touched the miss
path. **The docstring asserted an amplification bound the code did not have** — this repo's
recurring anti-pattern, and the reason the sweep's rule is that a finding needs a repro, not a
reading.

The fix is a floor between forced refreshes: `entra_jwks_refresh_cooldown_seconds`, default 60 s.

**What this costs, stated rather than buried.** A genuinely rotated signing key is picked up after
at most the cooldown instead of on the first token that uses it. That is a real regression in
rotation latency and it is bounded, configurable, and smaller than the 300 s `lifespan` staleness
the key cache already admits. `0` restores PyJWT's unthrottled behaviour, and a test pins that
direction too — so the gate is demonstrably a *rate* limit and not a permanent refusal.

### 3. `_match_kid` is ours

Key resolution called `PyJWKClient.match_kid`, a static method on a module-global the tests
legitimately replace — so a fake client broke key *matching*, which has nothing to do with the
client. The lookup is one line of data matching; it lives here now.

### 4. `/openapi.json` was unauthenticated, and the guard documented its own blind spot

`create_app` set `docs_url=None, redoc_url=None` and left `openapi_url` at its default. FastAPI
serves the schema from a plain `Route`, not an `APIRoute`, so `require_principal` never applied —
and `tests/test_route_auth_coverage.py` skipped it, saying so in a docstring, for the true but
insufficient reason that it carries no dependency tree to inspect. The full route, parameter and
model surface was readable by anyone who could reach the pod.

Nothing consumes it (the UI is static, both doc pages already off), so it is **closed**, not gated.

The general defect is not the route; it is that "cannot be gated" was treated as "need not be
looked at". `test_route_auth_coverage.py` now pins the entire ungatable surface to exactly
`{("Mount", "")}`, with a mutation proof that re-registering the schema route fails it. Re-enabling
`openapi_url`, mounting a second sub-app, or adding a bare `Route` now fails by name.

## Consequences

- New setting `entra_jwks_refresh_cooldown_seconds` (default 60.0, `ge=0`), in the `entra` section
  mixin with the rest of identity, and in `.env.example` per the CI-enforced parity check.
- New exception `IdentityProviderUnavailable`; a JWKS outage is 503 rather than 401 or 500.
- No OpenAPI schema is served. A client that wanted one would need it re-enabled deliberately, at
  which point the surface test names it.
- `SECURITY.md` corrected on three counts it was already wrong about, independent of this change:
  four pre-R2 module paths (`service/auth.py`, `agents/authz.py`, `agents/skill_access.py`), and the
  claim that an unauthenticated non-loopback bind "warns loudly at startup" when it has refused to
  boot since SEC-2. `tests/test_docstring_paths.py` only scans `src/` and `tests/`, so root Markdown
  had no guard at all — extending it is tracked separately.

## Alternatives rejected

- **Widening the `except` to `PyJWTError`.** It would map an IdP outage to 401, which is the
  misdiagnosis this ADR exists to stop.
- **Rate-limiting on the bearer token.** `_within_budget` runs *after* validation, by design
  (D-2026-07-31), and the flood never reaches it — measured: 40 requests, 40× 500, zero 429.
- **Negative-caching rejected `kid`s.** The `kid` is attacker-chosen and unbounded in cardinality,
  so the cache is the amplifier wearing a different hat. A single global cooldown has no key space.
- **Gating `/openapi.json` behind `require_principal`.** Not possible without re-implementing the
  route as an `APIRoute`, to serve a document with no consumer.
