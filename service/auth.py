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

from typing import Any

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from pydantic import BaseModel, Field

from chemclaw.config import settings

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


def _signing_key(token: str) -> Any:
    """Resolve the RSA signing key for `token` from the tenant JWKS (indirected for tests)."""
    return PyJWKClient(settings.entra_jwks_endpoint).get_signing_key_from_jwt(token).key


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
    """
    if not settings.entra_required:
        return Principal(oid=_DEV_PRINCIPAL_OID, upn="dev@localhost")
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return validate_token(header[len("Bearer ") :])
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
