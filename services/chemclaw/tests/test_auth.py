"""Front-door Entra OIDC validation (plan Phase F4-T1), proven offline with a local RSA key.

A real token is signed with a locally-generated key and validated by the module with the JWKS lookup
redirected to that key — so signature, audience, issuer, and claim extraction are all exercised
without a tenant or network. The HTTP tests prove the 401 gate and the dev-mode stand-in.
"""

import logging
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
# Captured at import time, before the autouse fixture swaps `_signing_key` out — so the JWKS-client
# construction test can exercise the real implementation.
_REAL_SIGNING_KEY = auth._signing_key


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


def test_token_validation_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`require_principal` validates in a worker thread, never on the event loop.

    The JWKS fetch inside validation is synchronous network I/O; run on the loop, a slow IdP
    would freeze every in-flight SSE stream and health probe of this single-process service.
    """
    import asyncio

    from service.auth import Principal

    monkeypatch.setattr(settings, "entra_required", True)
    on_loop: list[bool] = []

    def _probe(token: str) -> Principal:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return Principal(oid="u-thread")

    monkeypatch.setattr(auth, "validate_token", _probe)
    with TestClient(create_app(agent_factory=_FakeAgent)) as client:
        res = client.post("/sessions", headers={"Authorization": "Bearer x.y.z"})
    assert res.status_code == 200
    assert on_loop == [False]  # validation ran in a thread, not on the serving loop


def test_jwks_client_uses_the_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JWKS client is bounded by `entra_http_timeout_seconds`, not PyJWT's 30s default."""
    from types import SimpleNamespace

    captured: dict[str, object] = {}

    class _FakeJwksClient:
        def __init__(self, endpoint: str, *, timeout: float) -> None:
            captured["endpoint"] = endpoint
            captured["timeout"] = timeout

        def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
            return SimpleNamespace(key="the-key")

    monkeypatch.setattr(settings, "entra_tenant_id", "tid-1")
    monkeypatch.setattr(settings, "entra_http_timeout_seconds", 7.5)
    monkeypatch.setattr(auth, "PyJWKClient", _FakeJwksClient)
    monkeypatch.setattr(auth, "_jwks_clients", {})
    assert _REAL_SIGNING_KEY("tok") == "the-key"
    assert captured["timeout"] == 7.5
    assert captured["endpoint"] == settings.entra_jwks_endpoint


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_unauthenticated_loopback_boots(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """The local dev flow is untouched: no auth on a loopback bind boots without complaint."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "service_host", host)
    with TestClient(create_app(agent_factory=_FakeAgent)) as client:
        assert client.get("/healthz").status_code == 200


def test_unauthenticated_exposed_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """No auth on a non-loopback bind fails closed at startup with an actionable message (SEC-2).

    The earlier warn-and-boot left a network-exposed, authorization-gates-open deployment one
    missed log line away; refusing to start makes the insecure combination impossible by default.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "service_host", "0.0.0.0")
    monkeypatch.setattr(settings, "service_allow_insecure", False)
    with pytest.raises(RuntimeError, match="CHEMCLAW_ENTRA_REQUIRED"):
        create_app(agent_factory=_FakeAgent)


def test_unauthenticated_exposed_boots_only_with_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`service_allow_insecure=true` is the conscious opt-out: it boots, but warns loudly."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "service_host", "0.0.0.0")
    monkeypatch.setattr(settings, "service_allow_insecure", True)
    with caplog.at_level(logging.WARNING, logger="service.app"):
        app = create_app(agent_factory=_FakeAgent)
    assert any("authorization gates OPEN" in r.message for r in caplog.records)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_entra_required_exposed_boots_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The production posture (enforcement on, exposed bind) boots cleanly — nothing to warn."""
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "service_host", "0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="service.app"):
        create_app(agent_factory=_FakeAgent)
    assert not any("authorization gates OPEN" in r.message for r in caplog.records)
