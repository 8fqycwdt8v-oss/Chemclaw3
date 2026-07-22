"""Entra ID token validation → `Principal` (Phase 6, plan step 6.1).

Validating the JWT a caller presents is what turns an anonymous request into a `Principal`
(identity + roles) that the rest of Phase 6 already consumes — skill scoping (D-039) and tool
authorization (D-040). The validation *logic* — RS256 signature, audience, issuer, expiry, and
claim extraction — is standard and fully testable offline against a synthetic keypair; only the
*signing-key source* is live: Entra publishes rotating public keys at the tenant JWKS endpoint.

So the validator takes a pluggable key resolver: `TokenValidator.for_entra(...)` wires the live
`PyJWKClient` (the infra-gated edge — it reaches the network at validate time), while tests
inject a static public key. Everything else is deterministic and covered offline.
"""

from collections.abc import Callable
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient
from jwt.exceptions import PyJWKClientError

from chemclaw.identity import Principal


class TokenValidationError(Exception):
    """A presented token was missing, malformed, untrusted, expired, or carried no identity."""


# Resolves a raw token to the public key that should verify it (Entra picks the key by `kid`).
KeyResolver = Callable[[str], Any]


def _entra_issuer(tenant_id: str) -> str:
    """The expected `iss` for tokens from an Entra v2.0 tenant."""
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


def _entra_jwks_url(tenant_id: str) -> str:
    """The tenant's JWKS (signing-key) discovery endpoint."""
    return f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"


class TokenValidator:
    """Validate an Entra JWT and produce the `Principal` it represents."""

    def __init__(self, *, audience: str, issuer: str, key_resolver: KeyResolver) -> None:
        """Bind the checks a token must pass: its `aud`, its `iss`, and how its key is resolved."""
        self._audience = audience
        self._issuer = issuer
        self._key_resolver = key_resolver

    @classmethod
    def for_entra(
        cls, *, tenant_id: str, audience: str, jwks_url: str | None = None
    ) -> "TokenValidator":
        """Wire a validator against a live Entra tenant (rotating keys fetched from JWKS).

        `jwks_url` defaults to the tenant's v2.0 discovery endpoint. This is the infra-gated
        constructor: `PyJWKClient` reaches the network when a token is validated. The core
        `validate` stays offline — this only supplies the key resolver.
        """
        client = PyJWKClient(jwks_url or _entra_jwks_url(tenant_id))

        def resolve(token: str) -> Any:
            return client.get_signing_key_from_jwt(token).key

        return cls(audience=audience, issuer=_entra_issuer(tenant_id), key_resolver=resolve)

    def validate(self, token: str) -> Principal:
        """Return the `Principal` for a valid token, else raise `TokenValidationError`.

        Verifies the RS256 signature against the resolved key and requires a matching audience,
        issuer, and unexpired `exp`; then maps the Entra claims (`oid`, `preferred_username`/
        `upn`, `roles`, `groups`) to a `Principal`. A token failing any check — or carrying no
        `oid` (so the caller cannot be identified) — is rejected.
        """
        try:
            key = self._key_resolver(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "aud", "iss"]},
            )
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise TokenValidationError(str(exc)) from exc

        oid = claims.get("oid")
        if not oid:
            raise TokenValidationError("token has no 'oid' claim — cannot identify the caller")
        return Principal(
            oid=oid,
            upn=claims.get("preferred_username") or claims.get("upn"),
            roles=frozenset(claims.get("roles", [])),
            groups=frozenset(claims.get("groups", [])),
        )
