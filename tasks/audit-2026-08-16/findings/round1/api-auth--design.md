# Round 1 — `api/auth.py`, `api/rate_limit.py`, `api/budget.py` — design & simplification

Slice: `src/chemclaw/api/auth.py`, `src/chemclaw/api/rate_limit.py`, `src/chemclaw/api/budget.py`.
Lens: design and simplification only — structure that costs more than it buys. Correctness and
security of these files are another reviewer's.

Overall the three files are small and mostly earn their keep. The findings below are concentrated in
two places: a **vocabulary collision** where the word "budget" names two unrelated guards, and a set
of **module-level singletons and one-caller helpers** in `auth.py` whose justifying comments assert
properties the code does not have.

---

## "Budget" names two unrelated guards, and `_within_budget` runs the wrong one

- **Severity**: medium
- **Location**: `src/chemclaw/api/auth.py:240` (`_within_budget`), `src/chemclaw/api/auth.py:221`
  and `:237` (its call sites); `src/chemclaw/api/rate_limit.py:1` (module docstring),
  `:137` (`enforce_request_budget`)
- **Trigger**: read `require_principal` top to bottom. Both of its return statements are
  `return _within_budget(...)`. The name says the request has been checked against *a* budget; the
  only budget class in `api/` is `BudgetTracker` in `api/budget.py`, whose exception is
  `BudgetExceeded` and whose settings are `budget_max_turns_per_session` etc. `_within_budget`
  touches none of it — it calls `rate_limit.enforce_request_budget`, which is a token-bucket
  *rate* limiter keyed by principal.
- **Consequence**: the two admission guards that both answer 429 cannot be told apart by name at
  their call sites. A reader auditing "which routes are metered against the turn/token budget?"
  reads `_within_budget` inside the one dependency every route funnels through and gets the wrong
  answer: `BudgetTracker` is applied only in `routes/turns.py`, and `require_principal` never
  consults it. The collision is already producing confused prose *inside the slice*:
  `rate_limit.py`'s own module docstring uses "the budget guard (D-144) meters tokens" to mean the
  **other** module (`budget.py`) while the function it is documenting is called
  `enforce_request_budget`; and `routes/turns.py:212` numbers the guards "budget #3".
- **Evidence**: every other name in the rate-limiter's own chain is already "rate", not "budget" —
  the config fields are `service_rate_limit_per_minute` / `_burst` / `_max_principals`
  (`core/config/service.py:147-154`), the exception is `RateLimited`, the metric is
  `chemclaw_requests_rate_limited_total` (`rate_limit.py:150`). "Budget" survives in exactly three
  places, all of them in prose or in the two names below:

  ```
  auth.py:240   def _within_budget(principal: Principal) -> Principal:
  auth.py:256       enforce_request_budget(principal.oid)
  rate_limit.py:137 def enforce_request_budget(principal_id: str) -> None:
  ```

  Cross-checked that the two guards share nothing: `grep -rn "BudgetTracker\|enforce_request_budget"`
  over `src/` shows `enforce_request_budget` has exactly one caller (`auth._within_budget`) and
  `BudgetTracker.check` has exactly two (`routes/turns.py:140`, `:217`).
- **Fix**: rename `enforce_request_budget` → `enforce_request_rate` and `_within_budget` →
  `_within_rate_limit`, and change `rate_limit.py`'s docstring to stop calling itself "a
  per-principal request budget" (its first line) while using "the budget guard" for a different
  module three lines later. Purely mechanical — two definitions, two call sites, one test import
  (`tests/test_request_limits.py`). Behaviour-preserving.

---

## `Principal.upn` is populated from token claims and read by nothing

- **Severity**: medium
- **Location**: `src/chemclaw/api/auth.py:49` (`Principal.upn`), `:183` (the two-claim fallback
  that fills it), `:206`, `:221`
- **Trigger**: any authenticated request. `_principal_from_claims` computes
  `claims.get("preferred_username") or claims.get("upn") or ""` and stores it on every `Principal`.
- **Consequence**: dead field plus dead extraction logic on the hot path of every request, and —
  worse for a design review — a field that *looks* like the display identity the audit trail and
  the ownership checks use. They do not: everything reads `.oid`. Anyone adding a UI or a log line
  will reasonably reach for `.upn` and will be the first consumer of a value nobody has ever
  verified is populated (it silently degrades to `""` for a token carrying neither claim, and
  nothing notices).
- **Evidence**: `grep -rn "upn" --include=*.py src/` returns only the four definition/assignment
  sites in `auth.py` plus one comment in `core/config/entra.py:27`. No read anywhere in `src/`.
  The broader `grep -rn "principal\.\|Principal(" --include=*.py src/ | grep -v auth.py` confirms
  the only attributes ever read off a `Principal` are `.oid` (12 sites) and `.roles` (2 sites):

  ```
  api/deps.py:65    return owner == principal.oid
  api/deps.py:93    return bool(principal.roles & settings.entra_privileged_role_set)
  api/routes/turns.py:165  actor=principal.oid,
  api/routes/turns.py:166  roles=principal.roles,
  ... (no .upn)
  ```

  `Principal` is never `model_dump`ed into a response either — it is not in any response model.
- **Fix**: delete the `upn` field, line 183, and the `upn=` arguments at 206 and 221. If a display
  name is wanted later it should be added *with* its consumer, and with a decision about which
  claim wins rather than an `or` chain nobody has exercised. Behaviour-preserving (no reader
  exists); touches ~6 test call sites that pass `upn=` positionally-by-keyword.

---

## The process-global limiter freezes its rate at first use while its on/off switch stays live

- **Severity**: medium
- **Location**: `src/chemclaw/api/rate_limit.py:111` (`_limiter` global), `:114` (`limiter()`),
  `:131` (`reset_limiter`), `:145` (the live config read in `enforce_request_budget`)
- **Trigger**: call `enforce_request_budget` once, then change `service_rate_limit_per_minute` or
  `service_rate_limit_burst`. The *enable* decision at line 145 reads settings on every call; the
  *rate*, *burst* and *capacity* were baked into `_limiter` at first use and are never re-read.
- **Consequence**: two halves of one policy read the same config family with different lifetimes,
  which is invisible at the call site. It also forces a public, production-shipped
  `reset_limiter()` whose only callers are tests, and makes the limiter leak across app instances
  in one process: `create_app()` twice shares one bucket map, unlike `BudgetTracker`, which
  `api/app.py:282` constructs per app onto `app.state` and reaches through
  `FrontDoorState.budget` (`api/state.py:435`). Two guards of the same family, two different
  lifecycle designs, for no stated reason.
- **Evidence**: `/tmp/probe1.py` (run with `uv run`) printed:

  ```
  built with rate = 60.0 burst = 2.0
  after config change, limiter rate = 60.0 burst = 2.0
  per_minute=0 -> enforce is a no-op even though a limiter exists: True
  ```

  and `grep -rn reset_limiter --include=*.py .` shows callers only in
  `tests/test_request_limits.py:53,158,177`.
- **Fix**: build the limiter in `create_app` next to `BudgetTracker()` and hang it on `app.state`
  with a `FrontDoorState.rate_limiter` property, then have `require_principal` — which already
  receives the `Request` — read it via `state(request)`. That deletes `_limiter`, `limiter()` and
  `reset_limiter()` (three module symbols and a global), makes the lifetime match the other guard,
  and removes the config-lifetime split because the limiter is constructed once per app boot from
  the settings in force at boot. Behaviour-preserving in production (one app per process); test
  setup changes from `reset_limiter()` to building a fresh app.

---

## `auth.__all__` does not describe the module's actual surface

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:34-38`
- **Trigger**: read the `__all__` line and the five-line comment above it.
- **Consequence**: two errors in opposite directions in one statement.
  1. `GROUP_ROLE_PREFIX` is exported with a comment claiming the re-export exists "so
     `auth.GROUP_ROLE_PREFIX` keeps resolving for anything that already reads it from this module."
     Nothing does. `grep -rn GROUP_ROLE_PREFIX` over `src/` and `tests/` shows every reader importing
     it from `chemclaw.core.identity_context` — `ingest/documents/binding.py:24`,
     `tests/test_document_share.py:31` — and there is no `from ... import *` anywhere in the repo
     (`grep -rn "import \*" --include=*.py src/ tests/` matches only the word "string" in a
     comment). The symbol is used *inside* the module at line 205, so the import is not unused; the
     `__all__` entry and its justification are pure residue.
  2. `IdentityProviderUnavailable` (`:57`) is *absent* from `__all__`, yet it is raised out of the
     exported `validate_token` and is what distinguishes a 503 from a 401. `validate_token`'s own
     docstring (`:148`) says "return its `Principal`, or raise `AuthError`" — which is false; it can
     also raise `IdentityProviderUnavailable`. Today the single production caller
     (`require_principal:227`) handles it, so this is a documentation/surface defect rather than a
     live bug, but the exported contract actively teaches a future caller to catch the wrong set.
- **Evidence**: see the greps above; and `auth.py:148` vs `auth.py:140`:

  ```
  148:    """Validate an Entra OIDC token and return its `Principal`, or raise `AuthError`.
  ...
  140:        raise IdentityProviderUnavailable(f"tenant JWKS unreachable: {exc}") from exc
  ```
- **Fix**: drop `GROUP_ROLE_PREFIX` and its comment from `__all__` (keep the import — it has a real
  use at line 205), add `IdentityProviderUnavailable`, and amend `validate_token`'s docstring to
  name both raisable types. Behaviour-preserving.

---

## `_match_kid`: a one-expression helper with one caller and a docstring twice its size

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:88-95`
- **Trigger**: n/a — structural.
- **Consequence**: a named module-level function, five lines of justification, and an `Any`-typed
  signature, standing in for `next((k for k in keys if k.key_id == kid), None)` used at exactly one
  site (`:132`). The docstring's stated reason ("so key resolution does not depend on a class
  attribute … reaching into the client for it couples this module to a surface it does not
  otherwise use") does not survive contact with the neighbouring lines: the same function body
  calls `client.get_signing_keys()` and `client.get_signing_key(kid)`, and reads `key.key_id` and
  `key.key` off PyJWT objects. The module is already coupled to that surface four ways; avoiding a
  fifth buys nothing and costs a named indirection.
- **Evidence**: `auth.py:88-95` is the whole definition; `auth.py:132` is the whole usage:

  ```
  132:        cached = _match_kid(client.get_signing_keys(), kid)
  ```
- **Fix**: inline the generator expression at line 132 and delete lines 88-95. Behaviour-preserving.

---

## `_forced_refresh_allowed` is a mutator with a predicate's name

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:98-108`, called at `:135`
- **Trigger**: call `_forced_refresh_allowed(endpoint, now)` twice with the same arguments. The
  first returns `True`; the second returns `False` — because the first *wrote*
  `_last_forced_refresh[endpoint] = now`.
- **Consequence**: the name reads as a question, the call site reads as a question
  (`if not _forced_refresh_allowed(endpoint, time.monotonic()):`), and the function is the thing
  that consumes the resource. Anyone who moves that call — above the cache-hit check, into a log
  line, into an assertion, or duplicates it for a metric — silently spends the cooldown slot. The
  docstring admits the side effect in its second paragraph, which is where a reader looks *after*
  they have already mis-read the name.
- **Evidence**: `auth.py:104-108`:

  ```python
  last = _last_forced_refresh.get(endpoint)
  if last is not None and now - last < settings.entra_jwks_refresh_cooldown_seconds:
      return False
  _last_forced_refresh[endpoint] = now
  return True
  ```
- **Fix**: rename to `_claim_refresh_slot` (or `_take_refresh_slot`) so the name says it consumes
  something, and reword the call site to `if not _claim_refresh_slot(...)`. Behaviour-preserving.

---

## `_client_for` bakes the JWKS timeout in forever, while the config comment calls it "a live reader"

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:66-85` (`_jwks_clients`, `_client_for`), and the claim in
  `src/chemclaw/core/config/entra.py:72-73`
- **Trigger**: build a client, then change `entra_http_timeout_seconds`.
- **Consequence**: `PyJWKClient` is constructed once per endpoint with `timeout=` frozen at that
  moment. The comment at `auth.py:68` says "Keyed by endpoint so a config change is still picked
  up", which is true only of the *endpoint*; and `core/config/entra.py:72-73` states outright that
  "`entra_http_timeout_seconds` also bounds the front door's JWKS fetch, which is a live reader."
  It is not a live reader. Separately, `_jwks_clients` and `_last_forced_refresh` are module dicts
  with no reset seam at all, which is why `tests/test_auth.py:274-275` has to monkeypatch the two
  private dicts by name — the same lifecycle problem as the rate limiter above, solved less well
  (`rate_limit.py` at least ships `reset_limiter`).
- **Evidence**: `/tmp/probe3.py` printed:

  ```
  first build: timeout = 5.0
  after raising config to 30.0: timeout = 5.0 | same object: True
  cached clients after endpoint change: 2 (old one never freed)
  ```
- **Fix**: either key the cache on `(endpoint, timeout)`, or — simpler and consistent with the
  other guard — hang the JWKS client and the cooldown map on `app.state` beside the limiter so both
  are per-app rather than per-interpreter, and delete the two module globals and the test's reach
  into them. At minimum, correct the two comments: the timeout is fixed at first use.
  Behaviour-preserving either way (no production path mutates settings post-boot).

---

## `Retry-After` is a hand-rolled ceiling that rounds the wrong way

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:263`
- **Trigger**: a 429 whose `retry_after_seconds` lands just above an integer — e.g. `2.0005`, which
  a bucket with `service_rate_limit_per_minute=60` produces routinely (the value is
  `(1.0 - tokens) / rate`, an arbitrary float).
- **Consequence**: `max(1, int(x + 0.999))` is `ceil` only for `x` whose fractional part exceeds
  0.001; otherwise it rounds *down*, so the header tells the client to retry before a token has
  refilled and the retry earns a second 429. The magnitude is under a second, so this is minor —
  but it is a two-magic-number reimplementation of `math.ceil`, and the comment above it ("so a
  client backs off by the right amount rather than guessing") asserts precisely the property it
  does not have.
- **Evidence**: `/tmp/probe2.py` printed:

  ```
  retry_after=0.0001      shipped=1  ceil=1
  retry_after=2.0005      shipped=2  ceil=3  MISMATCH
  retry_after=3.0009      shipped=3  ceil=4  MISMATCH
  retry_after=10.0002     shipped=10  ceil=11  MISMATCH
  ```
- **Fix**: `headers={"Retry-After": str(max(1, math.ceil(exc.retry_after_seconds)))}` and
  `import math`. Behaviour-changing by design (that is the point), and by less than one second.

---

## `BudgetTracker`'s lock is justified by a thread concurrency that does not exist

- **Severity**: low
- **Location**: `src/chemclaw/api/budget.py:62-77` (class docstring), `:87` (`self._lock`),
  `:97`, `:130`
- **Trigger**: read the docstring's stated reason — "A lock guards the counters because the ASGI
  server runs turns for different sessions concurrently."
- **Consequence**: the reason is wrong, and being wrong it will be maintained wrongly. The front
  door's concurrency is asyncio on one event loop, not threads: `service_uvicorn_workers`
  (`core/config/service.py`) is a *process* count, and both call paths run on the loop —
  `routes/turns.py:140`/`:217` call `check` directly from the coroutine, and `api/runner.py:531`
  calls `record` from `run_turn`'s teardown, also on the loop. Neither `check` nor `record`
  contains an `await`, so they are already atomic with respect to every other coroutine; the
  `threading.Lock` is uncontended in production and can only ever be contended by test code
  (`tests/test_concurrency_claims.py:139` drives it from real threads). A `threading.Lock` acquired
  on the event loop is also the shape that blocks the *whole* loop if a future change ever does
  make it contended.
- **Evidence**: the only production callers, all on the loop:

  ```
  api/routes/turns.py:140       front.budget.check(session_id, principal.oid)
  api/routes/turns.py:217       front.budget.check(session_id, principal.oid)
  api/runner.py:531             budget.record(session.session_id, actor, turn_usage.total)
  ```
  No `to_thread`/`run_in_executor` path reaches `BudgetTracker` anywhere in `src/`.
- **Fix**: keep the lock (it is cheap and it is what makes the thread-driven concurrency test
  meaningful) but replace the docstring's reason with the true one — "callers may be on the loop or,
  in tests, on threads; the counters are guarded so the class is safe either way." If the intent is
  loop-only, delete the lock and say so. Behaviour-preserving in the docstring-only form.

---

### Checked and found sound (no finding)

- `core/bounded.BoundedLru` is genuinely shared by both files (`rate_limit.py:87`,
  `budget.py:81,85`) rather than re-implemented — no clone sites.
- `budget._over` (`:40`) and `_check_scope` (`:113`) each have two real callers; not
  single-caller abstractions.
- `RequestLimiter.check`'s injectable `now` (`:89`) is a legitimate seam, not test-only scaffolding
  leaking into production behaviour — the default path never passes it.
- No layering violation: all three files import only `core.*` and each other within `api/`;
  nothing in `core/` imports back into `api/`.
- `rate_limit.py` having exactly one importer (`auth.py`) is deliberate policy/mechanism
  separation, not an abstraction with a single caller — the module holds config-backed policy that
  `auth.py` should not.
