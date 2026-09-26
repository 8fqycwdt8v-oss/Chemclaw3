"""Front-door Entra OIDC validation (plan Phase F4-T1), proven offline with a local RSA key.

A real token is signed with a locally-generated key and validated by the module with the JWKS lookup
redirected to that key — so signature, audience, issuer, and claim extraction are all exercised
without a tenant or network. The HTTP tests prove the 401 gate and the dev-mode stand-in.
"""

import json
import logging
import os
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError

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
    """Point the validator at the test audience/issuer and the local signing key (no network).

    It used to pin `no_proxy` in both spellings as well, because `_client_for` wrote to the
    *process* environment to take the tenant host out of an ambient proxy's reach and that
    mutation outlived the test that caused it. `_HttpxJwkClient` carries `trust_env=False`
    instead, so there is nothing left to restore —
    `test_building_jwks_clients_concurrently_mutates_no_environment` is what keeps that true.
    """
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


@pytest.mark.parametrize("oid", ["  ", 42])
def test_token_with_a_blank_or_non_string_oid_is_rejected(rsa_key: Any, oid: object) -> None:
    """A malformed `oid` is an `AuthError` (a 401), not a `Principal` validation error (a 500)."""
    token = _sign(rsa_key, {"oid": oid})
    with pytest.raises(AuthError, match="no 'oid' claim"):
        validate_token(token)


def test_route_answers_401_for_a_blank_oid(monkeypatch: pytest.MonkeyPatch, rsa_key: Any) -> None:
    """End to end: the request is refused as unauthenticated rather than failing as a 500."""
    monkeypatch.setattr(settings, "entra_required", True)
    token = _sign(rsa_key, {"oid": "  "})
    with TestClient(create_app()) as client:
        response = client.post("/sessions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


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

    def get_signing_keys(self, refresh: bool = False) -> list[_FakeJwk]:
        self.cached_fetches += 1
        return [_FakeJwk("known-kid", "the-key")]

    def get_signing_key(self, kid: str) -> _FakeJwk:
        self.forced_refreshes += 1
        raise PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')


def _install_counting_client(monkeypatch: pytest.MonkeyPatch) -> _CountingJwksClient:
    """Point the real `_signing_key` at a counting client with a clean cooldown ledger."""
    monkeypatch.setattr(settings, "entra_tenant_id", "tid-1")
    monkeypatch.setattr(auth, "_HttpxJwkClient", _CountingJwksClient)
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
    monkeypatch.setattr(auth, "_HttpxJwkClient", _RecordingClient)
    monkeypatch.setattr(auth, "_jwks_clients", {})
    monkeypatch.setattr(auth, "_last_forced_refresh", {})
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _REAL_SIGNING_KEY(_token_with_kid(rsa_key, "known-kid")) == "the-key"
    assert captured["timeout"] == 7.5
    assert captured["endpoint"] == settings.entra_jwks_endpoint


@contextmanager
def _proxy_recorder() -> Iterator[tuple[str, list[str]]]:
    """A loopback HTTP server standing in for an ambient proxy, recording every request line.

    Records `CONNECT` as well as `GET` so the `https` arm is observed rather than inferred: a
    proxied `https` fetch reaches a proxy as a tunnel request naming the host, and answering it 502
    fails the fetch fast instead of leaving urllib mid-handshake against a plain HTTP server.
    """
    received: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:
            received.append(f"{self.command} {self.path}")
            body = b'{"keys": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_CONNECT(self) -> None:
            received.append(f"{self.command} {self.path}")
            self.send_error(502)

        def log_message(self, *args: Any) -> None:
            """Silence the handler's stderr logging; `received` is the record."""

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_the_jwks_fetch_does_not_follow_an_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    """The key set every bearer token is validated against must not come from a proxy.

    `PyJWKClient.fetch_data` calls `urllib.request.urlopen`, which reads the process environment
    for proxies and takes no `trust_env`. A proxy that could answer this fetch could serve a key
    set of its own choosing, so this is the one destination in the tree where following the
    environment is a trust decision rather than a routing one.

    Driven, not asserted as the shape of a keyword argument. The **control arm** builds a bare
    `PyJWKClient` for the same endpoint and requires the recorder to see the request — without it,
    a recorder that was never wired up would make the real assertion pass for the wrong reason, and
    it is also what proves the hazard is still live in the version of PyJWT installed today.

    **The third assertion is the point of the rewrite.** This used to be closed by appending the
    tenant host to `no_proxy`, which diverted the fetch by mutating the *process* environment —
    every other `urlopen` caller in the process saw it, it raced on the validation thread pool, and
    it needed a lock. `trust_env=False` is per request, so the environment the operator set must
    come back untouched: the two spellings are asserted verbatim, including the one this test sets
    and the absence of a spelling it did not.
    """
    with _proxy_recorder() as (proxy_url, received):
        for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            monkeypatch.setenv(name, proxy_url)
        monkeypatch.setenv("no_proxy", "keep.example")
        monkeypatch.delenv("NO_PROXY", raising=False)
        # Rebuilt from the environment above rather than inherited from whatever earlier test
        # first called `urlopen`: `ProxyHandler` snapshots the proxy *set* at construction.
        monkeypatch.setattr(urllib.request, "_opener", None)
        endpoint = f"{scheme}://jwks.invalid/tenant/discovery/v2.0/keys"
        monkeypatch.setattr(settings, "entra_jwks_url", endpoint)
        monkeypatch.setattr(auth, "_jwks_clients", {})

        with suppress(Exception):
            PyJWKClient(endpoint, timeout=2.0, cache_keys=False).fetch_data()
        assert received, (
            f"the control arm reached no recorder, so this test proves nothing about {scheme}"
        )
        received.clear()

        with suppress(auth.IdentityProviderUnavailable):
            auth._client_for(settings.entra_jwks_endpoint).fetch_data()
        assert received == [], (
            f"the JWKS fetch went to the ambient proxy ({received}) — a host that could answer it "
            "chooses the keys every bearer token is validated against"
        )

    assert os.environ["no_proxy"] == "keep.example", (
        "the JWKS fetch rewrote the operator's `no_proxy`; `trust_env=False` diverts this one "
        "request, and a process-global mutation is what it replaced"
    )
    assert "NO_PROXY" not in os.environ, (
        "the JWKS fetch invented an uppercase `NO_PROXY` the operator never set — which is how "
        "`getproxies_environment` comes to prefer ours and silently drop theirs"
    )


def test_building_jwks_clients_concurrently_mutates_no_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The absence test for the side effect that used to need a lock.

    `_client_for` runs on the validation thread pool — `validate_token` is dispatched through
    `asyncio.to_thread`, so two requests bearing tokens from two tenants genuinely build their
    clients at the same moment. Its body used to be an unsynchronised read-modify-write of
    `os.environ`: measured with the window widened, five concurrent writers of five distinct hosts
    left **one** of the five in `no_proxy`, and the loser's key set was then fetched through the
    ambient proxy.

    The fix is not a better lock, it is that there is nothing process-global left to write, so this
    asserts the *absence* rather than the repair — which is the shape that fails whoever re-adds
    the mutation, where a race test would only fail once it raced. The second assertion is the
    other half of deleting the lock: a check-then-insert may build a client twice, but every caller
    must leave with the one that is stored, or two threads warm two key caches and the
    unknown-`kid` refresh bound is per client rather than per endpoint.
    """
    monkeypatch.setattr(auth, "_jwks_clients", {})
    before = dict(os.environ)
    endpoints = [f"https://tenant{index}.login.example/keys" for index in range(4)]
    handed_out: list[tuple[str, object]] = []
    lock = threading.Lock()

    def build(endpoint: str) -> None:
        client = auth._client_for(endpoint)
        with lock:
            handed_out.append((endpoint, client))

    threads = [threading.Thread(target=build, args=(endpoint,)) for endpoint in endpoints * 2]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert dict(os.environ) == before, (
        "building JWKS clients changed the process environment; that mutation is what forced "
        "`_client_lock`, and it outlives the request that made it"
    )
    assert all(client is auth._jwks_clients[endpoint] for endpoint, client in handed_out), (
        "a concurrent `_client_for` handed a caller a client it did not store, so two threads "
        "would warm two key caches for one endpoint"
    )


def _closed_loopback_url() -> str:
    """A loopback URL whose port has just been closed — a connection refusal, not a hang.

    Bound and released rather than picked from thin air, so the port is one nothing else on this
    machine is serving; an arbitrary number could be somebody's development server and the refusal
    would silently become a response.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/tenant/discovery/v2.0/keys"


@contextmanager
def _jwks_server(status: int, body: str) -> Iterator[str]:
    """A real loopback HTTP server answering every GET with `status` and `body`.

    Real HTTP rather than a patched transport, because the whole subject is httpx's error taxonomy:
    a fake that raises the exception the test then asserts proves nothing about what the library
    does with a 500 or with an HTML body.
    """

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if 300 <= status < 400:
                # `httpx.Response.is_redirect` is a 3xx *and* a `Location`; a bare 302 is not a
                # redirect to it, so a server that omits this would test the wrong thing.
                self.send_header("Location", "http://elsewhere.invalid/keys")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            """Silence the handler's stderr logging."""

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/keys"
    finally:
        server.shutdown()
        server.server_close()


def test_a_fetched_key_set_is_returned_and_cached() -> None:
    """The happy path of the override, and the cache write that is not visible from outside it.

    `get_jwk_set` consults `jwk_set_cache` *before* calling `fetch_data`, so the `put` the override
    copies from upstream is what makes the second validation free. Without it every token would
    cost an outbound request to the tenant with no functional symptom at all — which is why this
    counts the server's fetches rather than trusting the code to have written the line.
    """
    key_set = '{"keys": [{"kty": "oct", "kid": "kid-a", "use": "sig", "k": "c2VjcmV0"}]}'
    with _jwks_server(200, key_set) as url:
        client = auth._HttpxJwkClient(url, timeout=5.0)
        assert client.fetch_data() == json.loads(key_set)
        assert [key.key_id for key in client.get_signing_keys()] == ["kid-a"]
        assert [key.key_id for key in client.get_signing_keys()] == ["kid-a"]
    # The server is gone by now, so a third lookup proves the cache rather than the network.
    assert [key.key_id for key in client.get_signing_keys()] == ["kid-a"]


@pytest.mark.parametrize(
    ("name", "status", "body", "expected"),
    [
        ("the tenant answered 500", 500, "{}", "answered 500"),
        ("a wrong tenant id is a 404", 404, "{}", "answered 404"),
        ("an intercepting proxy's error page", 200, "<html>502 Bad Gateway</html>", "unusable"),
        ("a redirect off the declared address", 302, "", "redirected"),
    ],
)
def test_every_way_the_fetch_can_fail_is_a_503_and_not_a_401(
    name: str, status: int, body: str, expected: str
) -> None:
    """The split, re-derived for httpx and driven over real HTTP.

    This is the assertion that had to be rewritten rather than inherited when the fetch moved off
    urllib: httpx raises a different exception family, and a mapping that let one of these fall
    through to `AuthError` would tell a chemist holding a perfectly good token that their
    credential was rejected while the actual fault was ours. Every arm of
    `_HttpxJwkClient.fetch_data` is exercised here except the transport one, which
    `test_an_unreachable_identity_provider_is_not_reported_as_a_bad_token` drives against a closed
    port.

    A 404 is in the list because it is the misconfiguration a fresh deployment makes — a wrong
    tenant id — and it is the case where "the IdP said no" is most easily mistaken for "the caller
    is wrong". It is not: no credential the caller could present would help.

    The 302 is the one case that is a *decision* rather than a translation: `urlopen` followed
    redirects and httpx does not, so this pins which way round that was settled. Refused, because a
    redirect moves the key set's origin out of the address `core/netguard.py` derived its allowlist
    from — and refused *by name* and before `raise_for_status`, which httpx raises on a 3xx too,
    so an operator reads "redirected" rather than a bare "answered 302".
    """
    with _jwks_server(status, body) as url:
        with pytest.raises(auth.IdentityProviderUnavailable, match=expected):
            auth._HttpxJwkClient(url, timeout=5.0).fetch_data()


def test_a_200_carrying_json_that_is_not_a_key_set_is_still_a_503(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any
) -> None:
    """The half of the old workaround the httpx move did **not** delete, asserted as still needed.

    Two shapes used to escape every handler in `api/auth.py` as HTTP 500s. One — an HTML error page
    — died in PyJWT's `json.load`, and now dies in `fetch_data`'s own decode, which is why that arm
    moved. The other dies in `PyJWKSet.from_dict` (`PyJWKSetError`, a `PyJWTError` that is neither
    a `PyJWKClientError` nor an `InvalidTokenError`), and `from_dict` runs in `get_jwk_set` on data
    that was fetched perfectly well — so moving the fetch did nothing for it and `_signing_key`
    still needs its last arm. Deleting that arm fails this test with a 500-shaped crash.
    """
    with _jwks_server(200, '{"error": "tenant not found"}') as url:
        monkeypatch.setattr(settings, "entra_jwks_url", url)
        monkeypatch.setattr(auth, "_jwks_clients", {})
        monkeypatch.setattr(auth, "_last_forced_refresh", {})
        with pytest.raises(auth.IdentityProviderUnavailable, match="unusable"):
            _REAL_SIGNING_KEY(_token_with_kid(rsa_key, "kid-a"))


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

    Driven against a **closed loopback port** rather than a stand-in raising the exception the
    assertion names, which would have proven only that `pytest.raises` works. The refusal is a real
    `httpx.ConnectError`, so what is under test is the mapping in `_HttpxJwkClient.fetch_data` —
    the arm that had to be re-derived when the fetch moved off urllib, because httpx raises a
    different exception family and getting it wrong makes an outage read as a rejected user.
    """
    monkeypatch.setattr(settings, "entra_jwks_url", _closed_loopback_url())
    monkeypatch.setattr(auth, "_jwks_clients", {})
    monkeypatch.setattr(auth, "_last_forced_refresh", {})
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
    # A real gateway address, so this test is about auth exposure and nothing else: the sibling
    # boot guard is satisfied by naming one, without relying on the suite's loopback opt-in.
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


# The gateway boot guard's own tests moved to `tests/test_llm_gateway_guard.py` with the guard
# (`D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard`). They were here
# because `create_app` was its only caller, which is exactly the defect that ADR closes: the
# refusal now has to hold in the background worker and the mcp face as well, and a test that can
# only reach it through `create_app` cannot say so.
