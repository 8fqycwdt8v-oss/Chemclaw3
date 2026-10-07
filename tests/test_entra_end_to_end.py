"""The enforced identity path, proven end to end against a real OIDC issuer over real HTTP.

An issuer publishes a key, the front door fetches it over HTTP, validates a token, turns it into a
`Principal`, stamps that into the turn's ambient identity, and the authorization gates decide on
it. `tests/test_auth.py` covers the validator with the JWKS lookup patched; this runs the chain.

Nothing in the module under test is patched: `_JwksIssuer` is a real HTTP server,
`settings.entra_jwks_url` points at it, and `create_app()` is the production app with
`entra_required=True`. The only fake is the model, through the `graph_factory` seam.
"""

import base64
import json
import ssl
import threading
import time
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import chemclaw.api.auth as auth
from chemclaw.agent.authz import AuthorizationError, authorize_tool
from chemclaw.api import app as front_door
from chemclaw.api.app import create_app
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_roles
from tests.fakes_turn import Piece, ScriptedTurn
from tests.test_netguard import _self_signed

_AUDIENCE = "api://chemclaw-e2e"
_ISSUER = "https://issuer.e2e.test/v2.0"
_PRIVILEGED = "process-chemist"

# Two key pairs, generated once for the module: RSA-2048 keygen is ~100 ms and every test needs at
# least one. The second exists for the two negatives that need a key the issuer never published —
# a forged token, and a rotation.
_KEY_A = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KEY_B = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64u(value: int) -> str:
    """One RSA parameter as the unpadded base64url a JWK spells it with."""
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwks(*keys: tuple[str, Any]) -> str:
    """A JWKS document publishing `(kid, private_key)` pairs, exactly as a tenant serves one."""
    return json.dumps(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": kid,
                    "n": _b64u(key.public_key().public_numbers().n),
                    "e": _b64u(key.public_key().public_numbers().e),
                }
                for kid, key in keys
            ]
        }
    )


def _sign(key: Any, kid: str, **claims: Any) -> str:
    """Sign an RS256 token carrying `kid` in its header, with tenant-shaped defaults."""
    payload: dict[str, Any] = {
        "aud": _AUDIENCE,
        "iss": _ISSUER,
        "exp": int(time.time()) + 3600,
        "oid": "u-default",
        **claims,
    }
    # `None` *removes* a claim, which is the only way to mint a token that omits one entirely —
    # the case `options={"require": [...]}` exists for, and the one a token with a bad value
    # cannot stand in for.
    payload = {key: value for key, value in payload.items() if value is not None}
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


class _JwksHandler(BaseHTTPRequestHandler):
    """Serve whatever JWKS the owning server currently holds, and count the fetch."""

    def do_GET(self) -> None:
        """Answer the keys endpoint; anything else is a 404, as a real tenant would."""
        server: Any = self.server
        if self.path != "/discovery/v2.0/keys":
            self.send_response(404)
            self.end_headers()
            return
        with server.lock:
            server.fetches += 1
            body = server.jwks.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the handler's stderr logging, which would flood the test output."""


class _JwksIssuer:
    """A real HTTP identity provider: one JWKS document, rotatable, with its fetches counted.

    The counter lets the caching tests measure fetches rather than assume them.
    """

    def __init__(self, jwks: str, tls: tuple[Path, Path] | None = None) -> None:
        """Start the server on an ephemeral port, publishing `jwks` — over https when `tls` is set.

        `tls` is a `(certificate, key)` pair; the certificate is what a tenant on a private CA
        presents, which is the shape `Chemclaw3_mock`'s https OIDC surface takes.
        """
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
        self._scheme = "http"
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(tls[0]), keyfile=str(tls[1]))
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
            self._scheme = "https"
        self._server.jwks = jwks  # type: ignore[attr-defined]
        self._server.fetches = 0  # type: ignore[attr-defined]
        self._server.lock = threading.Lock()  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def keys_url(self) -> str:
        """The endpoint `settings.entra_jwks_url` is pointed at."""
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"{self._scheme}://{host!s}:{port}/discovery/v2.0/keys"

    @property
    def fetches(self) -> int:
        """How many times the keys endpoint has been read."""
        return int(self._server.fetches)  # type: ignore[attr-defined]

    def publish(self, jwks: str) -> None:
        """Replace the published key set — a signing-key rotation, as a tenant performs one."""
        with self._server.lock:  # type: ignore[attr-defined]
            self._server.jwks = jwks  # type: ignore[attr-defined]

    def stop(self) -> None:
        """Shut the issuer down, so the next fetch fails the way an IdP outage does."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _AnswerOnlyTurn(ScriptedTurn):
    """A turn that produces one token and no tool calls — enough to reach the model."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Answer with a fixed word."""
        yield "ok"


class _IdentityProbeTurn(ScriptedTurn):
    """A turn that records the ambient identity and asks the real gate for a real decision.

    It calls `authorize_tool`, the function the tool middleware calls, from inside the model call,
    closing the chain from HTTP edge to the contextvar the gate reads.
    """

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.actors: list[str | None] = []
        self.roles: list[frozenset[str]] = []
        self.refusals: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Record the turn's identity, put a gated tool to the gate, and answer."""
        self.actors.append(get_current_actor())
        self.roles.append(get_current_roles())
        try:
            authorize_tool("record_knowledge_note")
            self.refusals.append("")
        except AuthorizationError as exc:
            self.refusals.append(str(exc))
        yield "ok"


def _no_connectors(_profile: str | None) -> list[Any]:
    """No connector opens for a turn here: the chain under test stops at the tool gate."""
    return []


@pytest.fixture
def issuer() -> Iterator[_JwksIssuer]:
    """A running issuer publishing key A under `kid-a`, torn down after the test."""
    running = _JwksIssuer(_jwks(("kid-a", _KEY_A)))
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture(autouse=True)
def _enforced(monkeypatch: pytest.MonkeyPatch, issuer: _JwksIssuer) -> None:
    """Put the process in the posture a real deployment ships: identity required, tenant reachable.

    The module-level caches in `chemclaw.api.auth` are cleared so tests measuring fetch counts and
    cooldowns do not depend on port uniqueness.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_audience", _AUDIENCE)
    monkeypatch.setattr(settings, "entra_issuer", _ISSUER)
    monkeypatch.setattr(settings, "entra_jwks_url", issuer.keys_url)
    monkeypatch.setattr(settings, "entra_privileged_roles", _PRIVILEGED)
    monkeypatch.setattr(auth, "_jwks_clients", {})
    monkeypatch.setattr(auth, "_last_forced_refresh", {})


def _client(turn: ScriptedTurn | None = None) -> TestClient:
    """The production app, built the way the service builds it, with only the model faked."""
    scripted = turn if turn is not None else _AnswerOnlyTurn()
    return TestClient(
        create_app(graph_factory=scripted.graph_factory, connector_factory=_no_connectors)
    )


def _bearer(token: str) -> dict[str, str]:
    """The header a browser sends."""
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------------------------
# The chain, as one thing
# --------------------------------------------------------------------------------------------


def test_a_token_the_issuer_vouches_for_opens_a_session(issuer: _JwksIssuer) -> None:
    """The whole edge: unauthenticated is refused, and a real issuer's token is admitted.

    The key is never handed to the validator — it is fetched from `issuer.keys_url` by PyJWT's own
    urllib, which is the half `tests/test_auth.py` patches out.
    """
    with _client() as client:
        assert client.post("/sessions").status_code == 401
        opened = client.post("/sessions", headers=_bearer(_sign(_KEY_A, "kid-a", oid="u-alice")))
        assert opened.status_code == 200
        assert opened.json()["session_id"]
    assert issuer.fetches >= 1, "the front door never asked the issuer for a key"


def test_the_keys_are_fetched_once_and_then_cached(issuer: _JwksIssuer) -> None:
    """Twenty requests cost one JWKS fetch, not twenty.

    The fetch is blocking network I/O on every request's path; a per-request fetch would stall the
    validation pool and amplify traffic against the tenant.
    """
    token = _sign(_KEY_A, "kid-a", oid="u-alice")
    with _client() as client:
        for _ in range(20):
            assert client.post("/sessions", headers=_bearer(token)).status_code == 200
    assert issuer.fetches == 1


def test_the_roles_in_the_token_reach_the_tool_authorization_gate() -> None:
    """A role claim, carried over HTTP, decides a tool call several layers down.

    Two turns differing only in the token's `roles` claim: one is refused `record_knowledge_note` by
    `DEFAULT_WRITE_TOOL_GATES`, the other is not. That proves the chain, not just the gate.
    """
    probe = _IdentityProbeTurn()
    with _client(probe) as client:
        for oid, roles in (("u-bench", []), ("u-lead", [_PRIVILEGED])):
            token = _sign(_KEY_A, "kid-a", oid=oid, roles=roles)
            session = client.post("/sessions", headers=_bearer(token)).json()["session_id"]
            with client.stream(
                "POST",
                f"/sessions/{session}/messages",
                json={"message": "hi"},
                headers=_bearer(token),
            ) as res:
                assert res.status_code == 200
                for _ in res.iter_lines():
                    pass

    assert probe.actors == ["u-bench", "u-lead"], "the validated oid did not reach the turn"
    assert probe.roles == [frozenset(), frozenset({_PRIVILEGED})]
    refused, allowed = probe.refusals
    assert "not authorized to use record_knowledge_note" in refused
    assert allowed == ""


def test_a_role_gated_route_refuses_the_same_caller_the_token_does_not_entitle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DELETE /jobs/{id}` is 403 without the role and reaches the handler with it.

    The 404 with the role held shows the request got past the gate to the job registry.
    """
    monkeypatch.setattr(front_door, "cancel_job", _no_such_job)
    with _client() as client:
        bench = _sign(_KEY_A, "kid-a", oid="u-bench")
        lead = _sign(_KEY_A, "kid-a", oid="u-lead", roles=[_PRIVILEGED])
        assert client.delete("/jobs/qm-1", headers=_bearer(bench)).status_code == 403
        assert client.delete("/jobs/qm-1", headers=_bearer(lead)).status_code == 404


async def _no_such_job(_job_id: str) -> bool:
    """A job registry that holds nothing — so the only thing left to prove is the gate."""
    return False


def test_one_chemists_session_is_invisible_to_another() -> None:
    """Ownership is enforced against the validated `oid`, and a non-owner learns nothing.

    404 rather than 403 on purpose: a 403 would confirm the id exists. Both callers are fully
    authenticated, so this is the authorization half rather than the authentication one.
    """
    with _client() as client:
        alice = _sign(_KEY_A, "kid-a", oid="u-alice")
        bob = _sign(_KEY_A, "kid-a", oid="u-bob")
        session = client.post("/sessions", headers=_bearer(alice)).json()["session_id"]
        transcript = f"/sessions/{session}/messages"
        assert client.get(transcript, headers=_bearer(alice)).status_code == 200
        refused = client.get(transcript, headers=_bearer(bob))
        assert refused.status_code == 404
        assert refused.json()["detail"] == "unknown session"


# --------------------------------------------------------------------------------------------
# The refusals, each against the real issuer
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "claims"),
    [
        ("wrong audience", {"aud": "api://someone-else"}),
        ("wrong issuer", {"iss": "https://attacker.test/v2.0"}),
        ("expired", {"exp": int(time.time()) - 30}),
        ("no expiry at all", {"exp": None}),
        ("no identity", {"oid": ""}),
    ],
)
def test_a_token_that_fails_one_check_is_refused(name: str, claims: dict[str, Any]) -> None:
    """Audience, issuer, expiry, a missing expiry, and identity each refuse a token.

    PyJWT checks `exp` only when present, so a token omitting it must be refused by demanding it.
    The audience case is the confused-deputy guard: a token from our own tenant issued for another
    resource must be refused.
    """
    token = _sign(_KEY_A, "kid-a", **claims)
    with _client() as client:
        res = client.post("/sessions", headers=_bearer(token))
    assert res.status_code == 401, name
    # SEC-7: which check failed is an operator's business, never the caller's.
    assert res.json()["detail"] == "invalid or expired token"


def test_a_token_signed_by_a_key_the_issuer_does_not_publish_is_refused() -> None:
    """A forged token whose `kid` names a real key is rejected on the signature.

    Key resolution succeeds and verification fails, which a test patching `_signing_key` cannot
    express.
    """
    with _client() as client:
        forged = _sign(_KEY_B, "kid-a", oid="u-attacker")
        assert client.post("/sessions", headers=_bearer(forged)).status_code == 401


def test_a_token_naming_an_unpublished_kid_is_refused_without_a_second_fetch(
    issuer: _JwksIssuer,
) -> None:
    """An unknown `kid` costs one refresh, and every later one nothing until the cooldown.

    The `kid` comes from an unauthenticated caller; without the cooldown each such request would be
    an outbound request to the tenant.
    """
    with _client() as client:
        # Warm the cache with a good token, so the count below is about the unknown kid alone.
        assert client.post("/sessions", headers=_bearer(_sign(_KEY_A, "kid-a"))).status_code == 200
        assert issuer.fetches == 1
        stranger = _sign(_KEY_B, "kid-unknown", oid="u-attacker")
        for _ in range(50):
            assert client.post("/sessions", headers=_bearer(stranger)).status_code == 401
    assert issuer.fetches == 2


def test_a_rotated_signing_key_is_picked_up_after_the_cooldown(
    issuer: _JwksIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotated signing key is picked up after the cooldown, without a restart.

    The first token carrying the new `kid` after the window pays one refresh.
    """
    monkeypatch.setattr(settings, "entra_jwks_refresh_cooldown_seconds", 0.0)
    with _client() as client:
        assert client.post("/sessions", headers=_bearer(_sign(_KEY_A, "kid-a"))).status_code == 200
        rotated = _sign(_KEY_B, "kid-b", oid="u-alice")
        assert client.post("/sessions", headers=_bearer(rotated)).status_code == 401
        issuer.publish(_jwks(("kid-a", _KEY_A), ("kid-b", _KEY_B)))
        assert client.post("/sessions", headers=_bearer(rotated)).status_code == 200


def test_an_unreachable_issuer_answers_503_and_not_401(issuer: _JwksIssuer) -> None:
    """An unreachable issuer answers 503, not 401.

    An IdP outage is our failure, not the caller's bad credential; the token here is valid.
    """
    token = _sign(_KEY_A, "kid-a", oid="u-alice")
    issuer.stop()
    with _client() as client:
        res = client.post("/sessions", headers=_bearer(token))
    assert res.status_code == 503
    assert res.json()["detail"] == "identity provider unavailable"


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("an HTML error page from a proxy", "<html><body>502 Bad Gateway</body></html>"),
        ("valid JSON that is not a key set", '{"error": "tenant not found"}'),
    ],
)
def test_an_issuer_answering_with_something_that_is_not_a_key_set_answers_503(
    issuer: _JwksIssuer, name: str, body: str
) -> None:
    """An issuer answering 200 with something that is not a key set answers 503.

    That is the shape of an intercepting proxy, captive portal or tenant misconfiguration. Two
    shapes fail in two places: an HTML page fails the decode in `_HttpxJwkClient.fetch_data`, and
    JSON that is not a key set fails in `PyJWKSet.from_dict` and is caught by `_signing_key`'s last
    arm. The test drives the app so both stay 503 whichever frame refuses them.
    """
    token = _sign(_KEY_A, "kid-a", oid="u-alice")
    issuer.publish(body)
    with _client() as client:
        res = client.post("/sessions", headers=_bearer(token))
    assert res.status_code == 503, name
    assert res.json()["detail"] == "identity provider unavailable"


def test_the_probes_stay_open_while_everything_else_is_closed() -> None:
    """Enforcement does not reach the kubelet or the scrape, and reaches everything else.

    The route-coverage sweep asserts this over the dependency tree; this asserts the same thing
    over the wire, in the enforced posture, which is where it matters.
    """
    with _client() as client:
        for path in ("/healthz", "/readyz", "/metrics"):
            assert client.get(path).status_code in (200, 503), path
        for path in (
            "/sessions",
            "/jobs",
            "/profiles",
            "/schedules",
            "/plans/pending",
        ):
            assert client.get(path).status_code == 401, path


# --------------------------------------------------------------------------------------------
# Key rotation against PyJWT's own refresh cooldown
# --------------------------------------------------------------------------------------------


def test_a_key_rotated_just_after_the_warm_fetch_is_accepted_on_its_first_token(
    issuer: _JwksIssuer,
) -> None:
    """The first token under a new `kid` is admitted at once, at the shipped cooldown.

    A warm fetch does not start the limiter, so the first forced refresh is granted. This test does
    not catch a second limiter (PyJWT's own cooldown); the test below does.
    """
    with _client() as client:
        assert client.post("/sessions", headers=_bearer(_sign(_KEY_A, "kid-a"))).status_code == 200
        issuer.publish(_jwks(("kid-a", _KEY_A), ("kid-b", _KEY_B)))
        rotated = _sign(_KEY_B, "kid-b", oid="u-alice")
        assert client.post("/sessions", headers=_bearer(rotated)).status_code == 200
    assert issuer.fetches == 2, "the new kid should cost exactly one refresh"


def test_rotation_latency_is_the_configured_cooldown_and_no_longer(
    issuer: _JwksIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotated key refused once is admitted as soon as our cooldown has passed.

    Worst case: a token under the new `kid` arrives before the tenant publishes it and spends the
    one forced refresh. With a 1 s cooldown, a second limiter composing with ours (PyJWT's 30 s,
    restarted by that fetch) would show up as a 401 after our window; `_client_for` disables it.
    """
    cooldown = 1.0
    monkeypatch.setattr(settings, "entra_jwks_refresh_cooldown_seconds", cooldown)
    with _client() as client:
        assert client.post("/sessions", headers=_bearer(_sign(_KEY_A, "kid-a"))).status_code == 200
        rotated = _sign(_KEY_B, "kid-b", oid="u-alice")
        assert client.post("/sessions", headers=_bearer(rotated)).status_code == 401
        # Read after the response: the limiter stamps its refresh inside that request, so a stamp
        # taken before it would let a slow runner wake inside the window and see a 401.
        refreshed_at = time.monotonic()
        issuer.publish(_jwks(("kid-a", _KEY_A), ("kid-b", _KEY_B)))
        # Inside the window the limiter holds — that is the cost it is configured to charge.
        assert client.post("/sessions", headers=_bearer(rotated)).status_code == 401
        time.sleep(max(0.0, cooldown - (time.monotonic() - refreshed_at)) + 0.1)
        assert client.post("/sessions", headers=_bearer(rotated)).status_code == 200
        admitted_after = time.monotonic() - refreshed_at
    assert admitted_after < cooldown + 1.0, f"admitted {admitted_after:.2f}s after the refresh"
    assert issuer.fetches == 3


# --------------------------------------------------------------------------------------------
# A tenant on a private CA (`entra_ca_bundle`)
# --------------------------------------------------------------------------------------------


@pytest.fixture
def private_ca_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[_JwksIssuer, Path]]:
    """An https issuer whose certificate no public root vouches for, and its CA as a PEM file.

    Self-signed for `127.0.0.1`, the shape a mock browser-facing tenant or a TLS-inspecting proxy's
    private root takes. `settings.entra_jwks_url` is pointed at it.
    """
    cert, key = _self_signed("private-tenant-ca", tmp_path)
    running = _JwksIssuer(_jwks(("kid-a", _KEY_A)), tls=(cert, key))
    monkeypatch.setattr(settings, "entra_jwks_url", running.keys_url)
    try:
        yield running, cert
    finally:
        running.stop()


def test_a_tenant_on_a_private_ca_is_refused_by_default(
    private_ca_issuer: tuple[_JwksIssuer, Path],
) -> None:
    """Unset, the key set is verified against certifi, and a private CA does not pass.

    503 rather than 401 — the token is fine, the tenant could not be verified — and the issuer
    never served a key, because the handshake failed before any request was made.
    """
    issuer, _ = private_ca_issuer
    assert settings.entra_ca_bundle == ""
    with _client() as client:
        res = client.post("/sessions", headers=_bearer(_sign(_KEY_A, "kid-a", oid="u-alice")))
    assert res.status_code == 503
    assert issuer.fetches == 0


def test_a_tenant_on_a_private_ca_is_trusted_through_the_configured_bundle(
    private_ca_issuer: tuple[_JwksIssuer, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming the CA in `entra_ca_bundle` is the whole change, and the chain then holds.

    Same issuer, same token, one setting: the fetch verifies against the bundle and the token is
    admitted.
    """
    issuer, ca = private_ca_issuer
    monkeypatch.setattr(settings, "entra_ca_bundle", str(ca))
    with _client() as client:
        res = client.post("/sessions", headers=_bearer(_sign(_KEY_A, "kid-a", oid="u-alice")))
    assert res.status_code == 200
    assert issuer.fetches == 1


def test_an_unset_bundle_is_the_process_trust_store_and_a_set_one_replaces_it(
    tmp_path: Path,
) -> None:
    """Unset, the trust store is `default_ssl_context` (certifi); set, the bundle replaces it.

    The mounted file is the complete statement of whom the tenant is trusted from. Verification
    stays on in both.
    """
    from chemclaw.core.http import default_ssl_context

    assert auth._tenant_ssl_context("") is default_ssl_context()
    cert, _ = _self_signed("only-ca", tmp_path)
    context = auth._tenant_ssl_context(str(cert))
    assert len(context.get_ca_certs()) == 1
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize("shape", ["missing", "directory", "empty", "not a certificate"])
def test_an_unusable_bundle_refuses_to_boot(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle that cannot be used stops the front door at boot, naming the setting.

    Left to the first sign-in it would be an `OSError` inside the fetch, outside every arm that
    maps a fetch failure to a 503 — a 500 per request, found by chemists before operators.
    """
    path = tmp_path / "ca.pem"
    if shape == "directory":
        path = tmp_path
    elif shape == "empty":
        path.write_text("")
    elif shape == "not a certificate":
        _, key = _self_signed("key-only", tmp_path)
        path = key
    monkeypatch.setattr(settings, "entra_ca_bundle", str(path))
    with pytest.raises(RuntimeError, match="CHEMCLAW_ENTRA_CA_BUNDLE"):
        _client()
