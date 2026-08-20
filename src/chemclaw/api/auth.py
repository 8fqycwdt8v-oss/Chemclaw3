"""Front-door user authentication via Azure Entra ID (plan Phase F4-T1).

Every non-health request to the front door must carry an Entra-issued OIDC token; this module
validates it and turns it into a `Principal` — the authenticated user's object id, name, and app
roles — which then authorizes and attributes every backend action (F4-T5). Validation checks the
signature against the tenant JWKS **and the audience** (the confused-deputy guard: the front door is
both an OAuth client and a protected resource, so a token minted for a *different* resource must be
rejected), plus the issuer.

`entra_required` gates enforcement: in any real deployment it is True and a missing/invalid token is
a 401; only local dev sets it False, where a fixed stand-in principal lets the app run with no
tenant. The signing-key lookup is a single indirection (`_signing_key`) so tests validate real
tokens against a local key without network. The raw-inference-credential exception (LLM) does not
apply here — this is a user-scoped resource access, so it is fully Entra-scoped.
"""

import asyncio
import logging
import time
from typing import Any

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
from pydantic import BaseModel, Field

from chemclaw.api.rate_limit import RateLimited, enforce_request_budget
from chemclaw.core.config import settings
from chemclaw.core.identity_context import GROUP_ROLE_PREFIX
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# Re-exported, not defined here: the prefix is part of the role vocabulary `core.identity_context`
# owns, and the manifests and refusal messages that teach an operator how to write a group-gated
# entitlement have to name the same string. Imported under its own name so `auth.GROUP_ROLE_PREFIX`
# keeps resolving for anything that already reads it from this module.
__all__ = ["GROUP_ROLE_PREFIX", "AuthError", "Principal", "require_principal", "validate_token"]

# The dev stand-in used only when `entra_required` is False (local, no tenant). Never reached in a
# real deployment, where every request is a validated Entra token.
_DEV_PRINCIPAL_OID = "dev-user"


class Principal(BaseModel):
    """An authenticated Entra user: the identity every backend action is attributed to."""

    oid: str = Field(min_length=1)
    upn: str = ""
    roles: frozenset[str] = frozenset()


class AuthError(Exception):
    """A token could not be validated (bad signature, audience, issuer, or missing identity)."""


class IdentityProviderUnavailable(Exception):
    """The tenant JWKS could not be reached, so *no* token can be validated right now.

    Deliberately not an `AuthError`. An unreachable IdP is our outage, not the caller's bad
    credential: answering 401 would tell a user with a perfectly good token that it was rejected,
    and would hide a dependency failure inside a metric operators read as "someone is probing us".
    """


# One JWKS client per endpoint, cached: `PyJWKClient` keeps its own key cache, so rebuilding it per
# request would re-fetch the tenant JWKS on the hot path and amplify under a token flood (review
# finding). Keyed by endpoint so a config change is still picked up.
_jwks_clients: dict[str, PyJWKClient] = {}

# When an unknown `kid` was last allowed to force a JWKS re-fetch, per endpoint. Caching the client
# — the earlier fix above — bounds the *warm* path but not this one: `PyJWKClient.get_signing_key`
# retries with `refresh=True` whenever the `kid` is absent from the cached set, and the `kid` comes
# from an unauthenticated caller's token header. That made one credential-less request cost one
# outbound fetch to the tenant IdP, and stalled the shared validation thread pool while it ran.
_last_forced_refresh: dict[str, float] = {}


def _client_for(endpoint: str) -> PyJWKClient:
    """The cached `PyJWKClient` for `endpoint`, built on first use with our configured timeout."""
    client = _jwks_clients.get(endpoint)
    if client is None:
        client = PyJWKClient(endpoint, timeout=settings.entra_http_timeout_seconds)
        _jwks_clients[endpoint] = client
    return client


def _match_kid(signing_keys: list[Any], kid: str) -> Any | None:
    """The key in `signing_keys` whose id is `kid`, or `None`.

    Written here rather than borrowed from `PyJWKClient.match_kid` so key resolution does not
    depend on a class attribute — the lookup is three lines of pure data matching, and reaching
    into the client for it couples this module to a surface it does not otherwise use.
    """
    return next((key for key in signing_keys if key.key_id == kid), None)


def _forced_refresh_allowed(endpoint: str, now: float) -> bool:
    """Whether an unknown `kid` may pay for a JWKS re-fetch — at most once per cooldown.

    Records the attempt when it grants one, so the *first* caller to hit a genuinely rotated key
    pays the fetch and every later caller reads the refreshed cache.
    """
    last = _last_forced_refresh.get(endpoint)
    if last is not None and now - last < settings.entra_jwks_refresh_cooldown_seconds:
        return False
    _last_forced_refresh[endpoint] = now
    return True


def _signing_key(token: str) -> Any:
    """Resolve the RSA signing key for `token` from the tenant JWKS (indirected for tests).

    The JWKS fetch is synchronous network I/O (PyJWT's urllib), so callers on the event loop must
    run validation in a worker thread (`require_principal` does); the client is built with the
    configured `entra_http_timeout_seconds` so a slow/blackholed IdP is bounded by our config, not
    PyJWT's 30s default.

    A `kid` that matches the cached key set costs no network at all. A `kid` that does not is
    rate-limited by `entra_jwks_refresh_cooldown_seconds` rather than refetching per request, and
    is otherwise refused as an `AuthError` — an unknown signing key is a caller problem, and the
    caller must not be able to choose how much work we do about it.
    """
    endpoint = settings.entra_jwks_endpoint
    client = _client_for(endpoint)
    # Raises `DecodeError` (an `InvalidTokenError`) on a malformed token, which `validate_token`
    # already turns into a 401 — so garbage never reaches the network at all.
    kid = jwt.get_unverified_header(token).get("kid")
    if not kid:
        raise AuthError("token header carries no 'kid'")
    try:
        cached = _match_kid(client.get_signing_keys(), kid)
        if cached is not None:
            return cached.key
        if not _forced_refresh_allowed(endpoint, time.monotonic()):
            raise AuthError(f"no signing key matches kid {kid!r} (refresh on cooldown)")
        return client.get_signing_key(kid).key
    # Order matters: the connection error is a subclass of the general client error.
    except PyJWKClientConnectionError as exc:
        raise IdentityProviderUnavailable(f"tenant JWKS unreachable: {exc}") from exc
    except PyJWKClientError as exc:
        # Not an `InvalidTokenError` — this is the class that used to escape every handler here
        # and surface as a 500.
        raise AuthError(f"no signing key matches kid {kid!r}: {exc}") from exc


def validate_token(token: str) -> Principal:
    """Validate an Entra OIDC token and return its `Principal`, or raise `AuthError`.

    Verifies the RS256 signature against the tenant JWKS, the audience (`entra_audience` — the
    confused-deputy guard), and the issuer, then extracts the identity claims.
    """
    try:
        key = _signing_key(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.entra_audience,
            issuer=settings.entra_issuer_url,
            # Require an expiry: PyJWT only checks `exp` when present, so reject a token that omits
            # it (Entra always issues one; this closes the no-exp edge). (review finding)
            options={"require": ["exp"]},
        )
    except jwt.InvalidTokenError as exc:  # signature/audience/issuer/expiry all funnel here
        raise AuthError(f"invalid token: {exc}") from exc
    return _principal_from_claims(claims)


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    """Build a `Principal` from validated claims (`oid` is mandatory — no anonymous identity).

    Under `entra_group_claims_as_roles` the token's `groups` claim joins the same set. An AD
    security group is an entitlement, and `authz`, `skill_access` and every manifest gate already
    match entitlements against exactly one set — so a group belongs *in* it rather than beside it.
    Carrying a second collection would mean every gate deciding, separately, whether it also
    consults groups, which is the shape a rule takes just before it stops being enforced in one of
    the places it was written.
    """
    oid = claims.get("oid")
    if not oid:
        raise AuthError("token has no 'oid' claim")
    upn = claims.get("preferred_username") or claims.get("upn") or ""
    entitlements = list(claims.get("roles", []))
    if settings.entra_group_claims_as_roles:
        # Entra emits `_claim_names`/`_claim_sources` instead of `groups` for a user in more
        # groups than the token can carry (~150+). That is an *overage*, not an empty membership,
        # and silently treating it as one would quietly deny the users with the most access. There
        # is no fix here — resolving it needs a Graph call, which D-089 does not permit — so it is
        # named in the log rather than hidden behind a shorter role list.
        if "groups" not in claims and "_claim_names" in claims:
            # Counted as well as logged. The log line names *who*, which an operator needs once
            # they are looking; the counter is what makes them look, because a warning on a pod's
            # stdout is not something anyone watches and this failure is silent from the chemist's
            # side too — a gated share simply returns nothing.
            record_metric(lambda m: m.increment("chemclaw_group_claim_overage_total"))
            logger.warning(
                "token for %s carries a group-claim overage rather than 'groups'; "
                "group-derived entitlements are unavailable for this user",
                oid,
            )
        # **Namespaced, not merged flat.** This same role set gates privileged tools
        # (`entra_privileged_roles`, `tool_role_gates`) and skills
        # (`agent/skill_access.py`), so an unprefixed group value is a role value. The comment that
        # used to sit here asserted these are group *object-ids* — but that is a tenant setting,
        # not a guarantee: `groupMembershipClaims` can emit `sam_account_name` or
        # `cloud_displayname` instead, at which point a group named like a privileged app role
        # silently grants it. One flag meant to hand a file share its read entitlement must not be
        # able to widen the write-tool gates.
        entitlements += [f"{GROUP_ROLE_PREFIX}{group}" for group in claims.get("groups", [])]
    return Principal(oid=oid, upn=upn, roles=frozenset(entitlements))


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: the validated Entra user for this request (401 if required and absent).

    With `entra_required` False (local dev) a fixed dev principal is returned so the app runs
    without a tenant; otherwise a missing/invalid `Authorization: Bearer` token is a 401.

    Validation runs in a worker thread: on a JWKS cache miss (cold start, lifespan expiry, key
    rotation) `_signing_key` performs a blocking HTTP fetch, and this single-process service serves
    every SSE stream and health probe on one event loop — a fetch stall on the loop would freeze
    them all.
    """
    if not settings.entra_required:
        return _within_budget(Principal(oid=_DEV_PRINCIPAL_OID, upn="dev@localhost"))
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        principal = await asyncio.to_thread(validate_token, header[len("Bearer ") :])
    except IdentityProviderUnavailable as exc:
        # 503, not 401: we could not reach the tenant to decide. `warning` rather than `info`
        # because this one is actionable — the token is fine and the dependency is not.
        logger.warning("identity provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="identity provider unavailable") from exc
    except AuthError as exc:
        # The specific failure reason (audience/issuer/expiry mismatch) is useful to an operator
        # but is not disclosed to the caller — log it server-side, return a generic 401 (SEC-7).
        logger.info("token validation failed: %s", exc)
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc
    return _within_budget(principal)


def _within_budget(principal: Principal) -> Principal:
    """Spend one request against this principal's rate budget, or 429.

    The one thing in this module that is not authentication, and it is here for the reason the
    PR-gate's proposal record is inside `propose_note` (D-2026-07-31): every authenticated route
    already funnels through `require_principal`, so one call here is a gate a new route cannot
    forget, while a decorator on twenty routes is a gate the twenty-first silently skips. The
    policy itself lives in `api/rate_limit.py`; this is only where the funnel is.

    *After* validation, never before. Limiting on the raw bearer token would also throttle the
    JWKS-backed validation path, which sounds like a bonus and is not: two tokens for one user are
    two buckets, so the limit would be per-credential rather than per-person, and rotating a token
    would reset it. `/healthz`, `/readyz` and `/metrics` do not depend on this function and are
    therefore never limited — a throttled probe reads as a down pod.
    """
    try:
        enforce_request_budget(principal.oid)
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail="too many requests",
            # Seconds until one token refills, so a client backs off by the right amount rather
            # than guessing — the same courtesy the budget guard's 429 already extends.
            headers={"Retry-After": str(max(1, int(exc.retry_after_seconds + 0.999)))},
        ) from exc
    return principal
