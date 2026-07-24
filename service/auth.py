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
from typing import Any

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from pydantic import BaseModel, Field

from chemclaw.config import settings

logger = logging.getLogger(__name__)

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


# One JWKS client per endpoint, cached: `PyJWKClient` keeps its own key cache, so rebuilding it per
# request would re-fetch the tenant JWKS on the hot path and amplify under a token flood (review
# finding). Keyed by endpoint so a config change is still picked up.
_jwks_clients: dict[str, PyJWKClient] = {}


def _signing_key(token: str) -> Any:
    """Resolve the RSA signing key for `token` from the tenant JWKS (indirected for tests).

    The JWKS fetch is synchronous network I/O (PyJWT's urllib), so callers on the event loop must
    run validation in a worker thread (`require_principal` does); the client is built with the
    configured `entra_http_timeout_seconds` so a slow/blackholed IdP is bounded by our config, not
    PyJWT's 30s default.
    """
    endpoint = settings.entra_jwks_endpoint
    client = _jwks_clients.get(endpoint)
    if client is None:
        client = PyJWKClient(endpoint, timeout=settings.entra_http_timeout_seconds)
        _jwks_clients[endpoint] = client
    return client.get_signing_key_from_jwt(token).key


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
    """Build a `Principal` from validated claims (`oid` is mandatory — no anonymous identity)."""
    oid = claims.get("oid")
    if not oid:
        raise AuthError("token has no 'oid' claim")
    upn = claims.get("preferred_username") or claims.get("upn") or ""
    roles = frozenset(claims.get("roles", []))
    return Principal(oid=oid, upn=upn, roles=roles)


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
        return Principal(oid=_DEV_PRINCIPAL_OID, upn="dev@localhost")
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return await asyncio.to_thread(validate_token, header[len("Bearer ") :])
    except AuthError as exc:
        # The specific failure reason (audience/issuer/expiry mismatch) is useful to an operator
        # but is not disclosed to the caller — log it server-side, return a generic 401 (SEC-7).
        logger.info("token validation failed: %s", exc)
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc
