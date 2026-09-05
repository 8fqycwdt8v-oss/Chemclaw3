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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

import chemclaw.api.auth as auth
from chemclaw.agent.session import TurnSession
from chemclaw.api.app import create_app
from chemclaw.api.auth import AuthError, validate_token
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS

_AUDIENCE = "api://chemclaw"
_ISSUER = "https://issuer.test/v2.0"
# Captured at import time, before the autouse fixture swaps `_signing_key` out — so the JWKS-client
# construction test can exercise the real implementation.
_REAL_SIGNING_KEY = auth._signing_key


class _FakeAgent:
    """A minimal agent whose only used method is `create_session` (no model)."""

    def create_session(self, *, session_id: str) -> TurnSession:
        return TurnSession(session_id=session_id)


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


def test_group_claims_join_the_role_set_only_when_configured(
    rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An AD security group is an entitlement, so it belongs in the one entitlement set.

    Off by default: a deployment whose tenant assigns the AD group to an app role already receives
    it as a `roles` value and must not also start matching raw group object-ids.

    **Namespaced when it is on.** A tenant may emit `groups` as names rather than object-ids
    (`groupMembershipClaims` accepts `sam_account_name`, `cloud_displayname`, …), so an unprefixed
    group value *is* an app-role value — and this same set gates privileged tools and skills.
    """
    claims = {"oid": "u-9", "roles": ["bench"], "groups": ["7f1c-group-oid"]}
    assert validate_token(_sign(rsa_key, claims)).roles == frozenset({"bench"})

    monkeypatch.setattr(settings, "entra_group_claims_as_roles", True)
    principal = validate_token(_sign(rsa_key, claims))
    assert principal.roles == frozenset({"bench", "group:7f1c-group-oid"})


def test_a_group_named_like_a_privileged_role_does_not_become_one(
    rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escalation the prefix exists to stop, stated as a test rather than as a comment.

    Turning on group claims to give a file share its read entitlement must not let anyone who can
    get a directory group created — or who is already in one that happens to be named for an app
    role — pass the write-tool and expensive-action gates.
    """
    monkeypatch.setattr(settings, "entra_group_claims_as_roles", True)
    claims = {"oid": "u-9", "roles": [], "groups": ["process-chemist"]}
    roles = validate_token(_sign(rsa_key, claims)).roles
    assert "process-chemist" not in roles
    assert roles == frozenset({"group:process-chemist"})


def test_a_group_claim_overage_is_reported_rather_than_read_as_no_groups(
    rsa_key: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Entra replaces `groups` with `_claim_names` past ~150 memberships.

    Treating that as an empty membership would quietly deny exactly the users with the most
    access, and the denial would look identical to a correct one. It is logged instead.
    """
    monkeypatch.setattr(settings, "entra_group_claims_as_roles", True)
    token = _sign(rsa_key, {"oid": "u-10", "roles": [], "_claim_names": {"groups": "src1"}})
    before = METRICS.value("chemclaw_group_claim_overage_total")
    with caplog.at_level("WARNING"):
        assert validate_token(token).roles == frozenset()
    assert "overage" in caplog.text
    # **And counted.** The log line names who; the counter is what makes anyone look. This failure
    # is silent from both sides — the chemist sees a gated share return nothing, the operator sees
    # a WARNING on a pod's stdout — so the only thing that turns it into an event is a series an
    # alert can read (`ChemclawGroupClaimOverage`).
    assert METRICS.value("chemclaw_group_claim_overage_total") == before + 1


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
    with TestClient(create_app()) as client:
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
    with TestClient(create_app()) as client:
        assert client.post("/sessions").status_code == 200


def test_healthz_never_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness must not be gated, even with enforcement on (probes carry no token)."""
    monkeypatch.setattr(settings, "entra_required", True)
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200


def test_token_validation_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`require_principal` validates in a worker thread, never on the event loop.

    The JWKS fetch inside validation is synchronous network I/O; run on the loop, a slow IdP
    would freeze every in-flight SSE stream and health probe of this single-process service.
    """
    import asyncio

    from chemclaw.api.auth import Principal

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
    with TestClient(create_app()) as client:
        res = client.post("/sessions", headers={"Authorization": "Bearer x.y.z"})
    assert res.status_code == 200
    assert on_loop == [False]  # validation ran in a thread, not on the serving loop


class _FakeJwk:
    """The two attributes of a `PyJWK` that key resolution actually reads."""

    def __init__(self, key_id: str, key: str) -> None:
        self.key_id = key_id
        self.key = key


class _CountingJwksClient:
    """A `PyJWKClient` stand-in that counts fetches, so amplification is measurable, not argued.

    Mirrors the real contract this module depends on: `get_signing_keys()` serves a cached set,
    and `get_signing_key(kid)` is the call that re-fetches when the `kid` is absent. Each is
    counted separately because the whole finding is about which one an anonymous caller can drive.
    """

    def __init__(self, endpoint: str, *, timeout: float) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.cached_fetches = 0
        self.forced_refreshes = 0
        self.connection_error = False

    def get_signing_keys(self, refresh: bool = False) -> list[_FakeJwk]:
        if self.connection_error:
            raise PyJWKClientConnectionError("cannot reach the tenant")
        self.cached_fetches += 1
        return [_FakeJwk("known-kid", "the-key")]

    def get_signing_key(self, kid: str) -> _FakeJwk:
        self.forced_refreshes += 1
        raise PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')


def _install_counting_client(monkeypatch: pytest.MonkeyPatch) -> _CountingJwksClient:
    """Point the real `_signing_key` at a counting client with a clean cooldown ledger."""
    monkeypatch.setattr(settings, "entra_tenant_id", "tid-1")
    monkeypatch.setattr(auth, "PyJWKClient", _CountingJwksClient)
    monkeypatch.setattr(auth, "_jwks_clients", {})
    monkeypatch.setattr(auth, "_last_forced_refresh", {})
    client = _CountingJwksClient(settings.entra_jwks_endpoint, timeout=1.0)
    monkeypatch.setitem(auth._jwks_clients, settings.entra_jwks_endpoint, client)
    return client


def _token_with_kid(rsa_key: Any, kid: str) -> str:
    """A well-formed RS256 token carrying `kid` in its header — the attacker-controlled field."""
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(
        {"oid": "u-1", "exp": int(time.time()) + 3600}, pem, algorithm="RS256", headers={"kid": kid}
    )


def test_jwks_client_uses_the_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JWKS client is bounded by `entra_http_timeout_seconds`, not PyJWT's 30s default."""
    captured: dict[str, object] = {}

    class _RecordingClient(_CountingJwksClient):
        def __init__(self, endpoint: str, *, timeout: float) -> None:
            super().__init__(endpoint, timeout=timeout)
            captured["endpoint"] = endpoint
            captured["timeout"] = timeout

    monkeypatch.setattr(settings, "entra_tenant_id", "tid-1")
    monkeypatch.setattr(settings, "entra_http_timeout_seconds", 7.5)
    monkeypatch.setattr(auth, "PyJWKClient", _RecordingClient)
    monkeypatch.setattr(auth, "_jwks_clients", {})
    monkeypatch.setattr(auth, "_last_forced_refresh", {})
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _REAL_SIGNING_KEY(_token_with_kid(rsa_key, "known-kid")) == "the-key"
    assert captured["timeout"] == 7.5
    assert captured["endpoint"] == settings.entra_jwks_endpoint


def test_an_unknown_kid_is_an_auth_error_not_an_unhandled_crash(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any
) -> None:
    """A `kid` absent from the JWKS must raise `AuthError` (a 401), never escape as a 500.

    `PyJWKClientError` is not a subclass of `jwt.InvalidTokenError`, so it used to slip past both
    `validate_token`'s handler and `require_principal`'s — turning an anonymous, malformed-token
    request into an unhandled exception. Remove the `PyJWKClientError` handler in
    `auth._signing_key` and this fails with that error instead.
    """
    _install_counting_client(monkeypatch)
    with pytest.raises(AuthError, match="no signing key matches"):
        _REAL_SIGNING_KEY(_token_with_kid(rsa_key, "attacker-chosen-kid"))


def test_an_unreachable_identity_provider_is_not_reported_as_a_bad_token(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any
) -> None:
    """A JWKS outage raises `IdentityProviderUnavailable`, which the route turns into 503, not 401.

    Answering 401 would tell a user with a valid token that their credential was rejected, and
    would bury a dependency outage in a metric that reads as "someone is probing us".
    """
    client = _install_counting_client(monkeypatch)
    client.connection_error = True
    with pytest.raises(auth.IdentityProviderUnavailable, match="unreachable"):
        _REAL_SIGNING_KEY(_token_with_kid(rsa_key, "known-kid"))


def test_a_known_kid_costs_no_forced_refresh(monkeypatch: pytest.MonkeyPatch, rsa_key: Any) -> None:
    """The warm path is untouched: a `kid` in the cached set never triggers a re-fetch."""
    client = _install_counting_client(monkeypatch)
    for _ in range(5):
        assert _REAL_SIGNING_KEY(_token_with_kid(rsa_key, "known-kid")) == "the-key"
    assert client.forced_refreshes == 0


def test_an_unknown_kid_flood_forces_at_most_one_refresh_per_cooldown(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any
) -> None:
    """The amplification bound, measured: 50 anonymous unknown-`kid` tokens buy one refresh.

    This is the defect's real shape. PyJWT re-fetches the tenant JWKS on *every* `kid` miss, and
    the `kid` is chosen by an unauthenticated caller, so before the cooldown 50 credential-less
    requests meant 50 outbound requests to the IdP — each one occupying a validation worker
    thread. Set `entra_jwks_refresh_cooldown_seconds` to 0 and this fails with 50.
    """
    client = _install_counting_client(monkeypatch)
    monkeypatch.setattr(settings, "entra_jwks_refresh_cooldown_seconds", 300.0)
    for i in range(50):
        with pytest.raises(AuthError):
            _REAL_SIGNING_KEY(_token_with_kid(rsa_key, f"bogus-{i}"))
    assert client.forced_refreshes == 1


def test_the_cooldown_still_lets_a_rotated_key_be_picked_up(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any
) -> None:
    """The cooldown delays key rotation; it must not prevent it.

    A zero cooldown is the degenerate case that proves the gate is a *rate* limit and not a
    permanent refusal — every miss is allowed to refresh, exactly as PyJWT does unaided.
    """
    client = _install_counting_client(monkeypatch)
    monkeypatch.setattr(settings, "entra_jwks_refresh_cooldown_seconds", 0.0)
    for i in range(3):
        with pytest.raises(AuthError):
            _REAL_SIGNING_KEY(_token_with_kid(rsa_key, f"rotated-{i}"))
    assert client.forced_refreshes == 3


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_unauthenticated_loopback_boots(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """The local dev flow is untouched: no auth on a loopback bind boots without complaint."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "service_host", host)
    with TestClient(create_app()) as client:
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
        create_app()


def test_unauthenticated_exposed_boots_only_with_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`service_allow_insecure=true` is the conscious opt-out: it boots, but warns loudly."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "service_host", "0.0.0.0")
    monkeypatch.setattr(settings, "service_allow_insecure", True)
    # A non-loopback bind must name a real gateway or `_refuse_unconfigured_llm_gateway` fires
    # (the shipped default is the loopback mock); this test is about auth exposure.
    monkeypatch.setattr(settings, "llm_base_url", "http://internal-llm:8000/v1")
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.app"):
        app = create_app()
    assert any("authorization gates OPEN" in r.message for r in caplog.records)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_entra_required_exposed_boots_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The production posture (enforcement on, exposed bind) boots cleanly — nothing to warn."""
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "service_host", "0.0.0.0")
    monkeypatch.setattr(settings, "llm_base_url", "http://internal-llm:8000/v1")
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.app"):
        create_app()
    assert not any("authorization gates OPEN" in r.message for r in caplog.records)


def test_exposed_process_still_on_the_dev_gateway_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network-exposed process still pointed at the loopback mock fails closed at boot.

    **This replaces a guard that was false in the direction that mattered.**
    `_refuse_public_llm_exposure` refused `llm_provider="anthropic"` with no `llm_base_url`, and
    returned early whenever `llm_base_url` was *truthy* — while on that provider the base URL was
    never passed to the client at all. So the one shape it existed to catch (a gateway configured,
    the provider left at its shipped default) was precisely the one it waved through, and
    `core/netguard.derive_allowed` opened `api.anthropic.com` for the same reason.

    With one destination there is no public default left to refuse
    (`D-2026-09-04-a-gateway-is-the-only-provider`); what is new is that `llm_base_url` ships with a
    value, the local mock. That default cannot leave the pod — but a deployment that forgot to
    override it would meet it as a refused connection on a chemist's first question. This says so
    at boot instead, on the same non-loopback-bind signal the auth guard uses.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "service_host", "0.0.0.0")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:8820/v1")
    with pytest.raises(RuntimeError, match="loopback address"):
        create_app()


def test_a_loopback_bind_may_keep_the_dev_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: local dev against the mock is untouched.

    A guard that also broke `make chat` would be a worse defect than the one it closes, and
    `pytest.raises` on the test above cannot show that it does not.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:8820/v1")
    create_app()
