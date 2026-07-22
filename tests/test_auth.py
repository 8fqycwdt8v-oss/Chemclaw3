"""Front-door Entra OIDC validation (plan Phase F4-T1), proven offline with a local RSA key.

A real token is signed with a locally-generated key and validated by the module with the JWKS lookup
redirected to that key — so signature, audience, issuer, and claim extraction are all exercised
without a tenant or network. The HTTP tests prove the 401 gate and the dev-mode stand-in.
"""

import time
from typing import Any

import jwt
import pytest
from agent_framework import AgentSession
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import service.auth as auth
from chemclaw.config import settings
from service.app import create_app
from service.auth import AuthError, validate_token

_AUDIENCE = "api://chemclaw"
_ISSUER = "https://issuer.test/v2.0"


class _FakeAgent:
    """A minimal agent whose only used method is `create_session` (no model)."""

    def create_session(self, *, session_id: str) -> AgentSession:
        return AgentSession(session_id=session_id)


@pytest.fixture
def rsa_key() -> Any:
    """A fresh RSA private key for signing test tokens."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _sign(key: Any, claims: dict[str, Any]) -> str:
    """Sign a token (RS256) with sensible defaults for aud/iss/exp, overridable via `claims`."""
    payload = {"aud": _AUDIENCE, "iss": _ISSUER, "exp": int(time.time()) + 3600, **claims}
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(payload, pem, algorithm="RS256")


@pytest.fixture(autouse=True)
def _entra_env(monkeypatch: pytest.MonkeyPatch, rsa_key: Any) -> None:
    """Point the validator at the test audience/issuer and the local signing key (no network)."""
    monkeypatch.setattr(settings, "entra_audience", _AUDIENCE)
    monkeypatch.setattr(settings, "entra_issuer", _ISSUER)
    monkeypatch.setattr(auth, "_signing_key", lambda _token: rsa_key.public_key())


def test_valid_token_yields_principal(rsa_key: Any) -> None:
    """A well-formed token validates and its identity/roles are extracted."""
    token = _sign(
        rsa_key, {"oid": "u-123", "preferred_username": "chemist@corp", "roles": ["bench"]}
    )
    principal = validate_token(token)
    assert principal.oid == "u-123"
    assert principal.upn == "chemist@corp"
    assert principal.roles == frozenset({"bench"})


def test_wrong_audience_is_rejected(rsa_key: Any) -> None:
    """A token minted for a different resource is rejected (the confused-deputy guard)."""
    token = _sign(rsa_key, {"oid": "u-1", "aud": "api://someone-else"})
    with pytest.raises(AuthError):
        validate_token(token)


def test_token_without_oid_is_rejected(rsa_key: Any) -> None:
    """A validly-signed token with no identity claim is rejected — no anonymous principal."""
    token = _sign(rsa_key, {"preferred_username": "nobody@corp"})
    with pytest.raises(AuthError):
        validate_token(token)


def test_expired_token_is_rejected(rsa_key: Any) -> None:
    """An expired token is rejected."""
    token = _sign(rsa_key, {"oid": "u-1", "exp": int(time.time()) - 10})
    with pytest.raises(AuthError):
        validate_token(token)


def test_route_requires_token_when_entra_required(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any
) -> None:
    """With enforcement on, a session route is 401 without a token and 200 with a valid one."""
    monkeypatch.setattr(settings, "entra_required", True)
    with TestClient(create_app(agent_factory=_FakeAgent)) as client:
        assert client.post("/sessions").status_code == 401
        token = _sign(rsa_key, {"oid": "u-9"})
        ok = client.post("/sessions", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert ok.json()["session_id"]
        # SEC-7: a rejected token returns a generic 401 detail, not the validation reason.
        bad = client.post("/sessions", headers={"Authorization": "Bearer not.a.jwt"})
        assert bad.status_code == 401
        assert bad.json()["detail"] == "invalid or expired token"


def test_dev_mode_allows_no_token() -> None:
    """With enforcement off (local dev), a session route works without a token (dev principal)."""
    with TestClient(create_app(agent_factory=_FakeAgent)) as client:
        assert client.post("/sessions").status_code == 200


def test_healthz_never_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness must not be gated, even with enforcement on (probes carry no token)."""
    monkeypatch.setattr(settings, "entra_required", True)
    with TestClient(create_app(agent_factory=_FakeAgent)) as client:
        assert client.get("/healthz").status_code == 200
