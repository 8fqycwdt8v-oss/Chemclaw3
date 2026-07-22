"""Entra token validation → `Principal` (Phase 6, plan step 6.1).

Exercises the full validation decision offline with a synthetic RSA keypair: a well-formed,
correctly-signed, unexpired token for the right audience/issuer yields the expected `Principal`;
a token that fails any check (bad signature, wrong audience, expired, no `oid`) is rejected.
Only the live signing-key *source* (JWKS over the network) is out of scope here — it is injected.
"""

import datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from chemclaw.auth import TokenValidationError, TokenValidator, _entra_issuer, _entra_jwks_url

_AUD = "api://chemclaw"
_ISS = "https://login.microsoftonline.com/test-tenant/v2.0"


def _keypair() -> rsa.RSAPrivateKey:
    """A throwaway RSA private key for signing test tokens."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(signing_key: rsa.RSAPrivateKey, **overrides: Any) -> str:
    """Encode a signed Entra-shaped token, with defaults any test can override."""
    now = datetime.datetime.now(datetime.UTC)
    claims: dict[str, Any] = {
        "oid": "00000000-0000-0000-0000-000000000001",
        "preferred_username": "chemist@example.com",
        "roles": ["compute", "lab"],
        "groups": ["g-projectX"],
        "aud": _AUD,
        "iss": _ISS,
        "exp": now + datetime.timedelta(hours=1),
        **overrides,
    }
    return jwt.encode(claims, signing_key, algorithm="RS256")


def _validator(verifying_key: Any) -> TokenValidator:
    """A validator that resolves every token to `verifying_key` (stands in for JWKS)."""
    return TokenValidator(audience=_AUD, issuer=_ISS, key_resolver=lambda _token: verifying_key)


def test_valid_token_yields_the_principal() -> None:
    """A correctly-signed, in-audience, unexpired token maps its claims to a `Principal`."""
    key = _keypair()
    principal = _validator(key.public_key()).validate(_token(key))
    assert principal.oid == "00000000-0000-0000-0000-000000000001"
    assert principal.upn == "chemist@example.com"
    assert principal.roles == frozenset({"compute", "lab"})
    assert principal.groups == frozenset({"g-projectX"})


def test_bad_signature_is_rejected() -> None:
    """A token signed by a different key than the resolver returns is rejected."""
    signer, other = _keypair(), _keypair()
    with pytest.raises(TokenValidationError):
        _validator(other.public_key()).validate(_token(signer))


def test_wrong_audience_is_rejected() -> None:
    """A token minted for a different audience is rejected."""
    key = _keypair()
    with pytest.raises(TokenValidationError):
        _validator(key.public_key()).validate(_token(key, aud="api://someone-else"))


def test_expired_token_is_rejected() -> None:
    """A token whose `exp` is in the past is rejected."""
    key = _keypair()
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    with pytest.raises(TokenValidationError):
        _validator(key.public_key()).validate(_token(key, exp=past))


def test_token_without_oid_is_rejected() -> None:
    """No `oid` means the caller cannot be identified — reject rather than guess."""
    key = _keypair()
    with pytest.raises(TokenValidationError, match="oid"):
        _validator(key.public_key()).validate(_token(key, oid=None))


def test_entra_issuer_and_jwks_url_derivation() -> None:
    """The live constructor derives the standard v2.0 issuer + JWKS endpoint from the tenant."""
    assert _entra_issuer("t") == "https://login.microsoftonline.com/t/v2.0"
    assert _entra_jwks_url("t") == "https://login.microsoftonline.com/t/discovery/v2.0/keys"
    # Constructing the live validator does not touch the network (PyJWKClient fetches lazily).
    assert isinstance(TokenValidator.for_entra(tenant_id="t", audience=_AUD), TokenValidator)
