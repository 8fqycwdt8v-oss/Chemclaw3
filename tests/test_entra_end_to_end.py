"""The enforced identity path, proven end to end against a real OIDC issuer over real HTTP.

**Why this file exists.** `tests/test_auth.py` proves the *validator*: it signs a token with a
local key and swaps `auth._signing_key` for a lambda that returns that key, so signature, audience,
issuer and claim extraction are all exercised — and the JWKS lookup, the one part of validation that
talks to a network, never runs. Every other authorization test sets the ambient identity by hand.
So the chain that a deployment actually depends on — *an issuer publishes a key, the front door
fetches it over HTTP, validates a token against it, turns the token into a `Principal`, stamps that
principal into the turn's ambient identity, and the authorization gates decide on it* — had no test
that ran it as one thing. `docs/planning/DEFERRED.md` recorded this as gated on "a real Entra
tenant", which was never true: an issuer is a JWKS document served over HTTP, and this file serves
one.

Nothing here is patched inside the module under test. `_JwksIssuer` is a real HTTP server on a real
port; `settings.entra_jwks_url` points at it; `PyJWKClient` fetches from it with its own urllib;
`create_app()` is the production app with `entra_required=True`. The only fake is the model, through
the `graph_factory` seam every other front-door test uses.

The companion piece is `Chemclaw3_mock`'s `/entra` surface, which is this issuer as a long-running
service so the live lane and the four-repo e2e can run enforced too. This file is what makes the
claim checkable in CI, where no lane runs.
"""

import base64
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    The fetch counter is what makes `test_the_keys_are_fetched_once_and_then_cached` a measurement
    rather than a belief — the JWKS fetch is blocking network I/O on the path that serves every
    request, so "cached" is a property worth proving rather than asserting in a docstring.
    """

    def __init__(self, jwks: str) -> None:
        """Start the server on an ephemeral port, publishing `jwks`."""
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
        self._server.jwks = jwks  # type: ignore[attr-defined]
        self._server.fetches = 0  # type: ignore[attr-defined]
        self._server.lock = threading.Lock()  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def keys_url(self) -> str:
        """The endpoint `settings.entra_jwks_url` is pointed at."""
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host!s}:{port}/discovery/v2.0/keys"

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

    This is the last link of the chain and the one nothing else covered: a token is validated at the
    HTTP edge, and what the *authorization* gate reads is a contextvar several layers below. The
    probe closes it by calling `authorize_tool` — the same function the tool middleware calls — from
    inside the model call, and recording both what it saw and what it was told.
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

    The two module-level caches in `chemclaw.api.auth` are cleared rather than left alone. They are
    keyed by endpoint and each test gets a fresh port, so nothing would leak — but a test that
    *measures* fetch counts and refresh cooldowns must not depend on that reasoning holding for the
    next person who adds one.
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

    Measured rather than argued: the fetch is blocking network I/O on the path that serves every
    request, and a per-request fetch would both stall the shared validation thread pool and turn
    ordinary traffic into an amplifier against the tenant.
    """
    token = _sign(_KEY_A, "kid-a", oid="u-alice")
    with _client() as client:
        for _ in range(20):
            assert client.post("/sessions", headers=_bearer(token)).status_code == 200
    assert issuer.fetches == 1


def test_the_roles_in_the_token_reach_the_tool_authorization_gate() -> None:
    """A role claim, carried over HTTP, decides a tool call several layers down.

    Two turns, identical but for the `roles` claim in the token: the role-less one is refused
    `record_knowledge_note` by `DEFAULT_WRITE_TOOL_GATES`, the entitled one is not. That the same
    request differs only by the token is what makes this a proof of the *chain* rather than of the
    gate — which `tests/test_authz.py` already covers by setting the contextvar directly.
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

    The two responses are 403 and 404, and the 404 is the point: with the role held, the request
    got past the gate and asked the job registry a question it answered "no such job". The role
    claim in the token is the only difference between the two calls.
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
    """Audience, issuer, expiry, a *missing* expiry, and identity are each load-bearing.

    The absent-`exp` case is not a duplicate of the expired one and does not overlap it: PyJWT
    checks `exp` only when the claim is present, so a token that simply omits it validates forever
    unless the decoder demands it. Entra always issues one; this closes the edge anyway.

    The audience case is the confused-deputy guard specifically: the front door is both an OAuth
    client and a protected resource, so a token minted by *our own tenant*, correctly signed by the
    key the issuer publishes, must still be refused when it was issued for a different resource.
    """
    token = _sign(_KEY_A, "kid-a", **claims)
    with _client() as client:
        res = client.post("/sessions", headers=_bearer(token))
    assert res.status_code == 401, name
    # SEC-7: which check failed is an operator's business, never the caller's.
    assert res.json()["detail"] == "invalid or expired token"


def test_a_token_signed_by_a_key_the_issuer_does_not_publish_is_refused() -> None:
    """A forged token whose `kid` names a real key is rejected on the signature.

    `kid-a` *is* published, so key resolution succeeds and the token is verified against the right
    public key — and fails, because it was signed with key B. This is the case a test that patches
    `_signing_key` to "return the key that signed it" cannot express.
    """
    with _client() as client:
        forged = _sign(_KEY_B, "kid-a", oid="u-attacker")
        assert client.post("/sessions", headers=_bearer(forged)).status_code == 401


def test_a_token_naming_an_unpublished_kid_is_refused_without_a_second_fetch(
    issuer: _JwksIssuer,
) -> None:
    """An unknown `kid` costs one refresh, and every later one costs nothing until the cooldown.

    The `kid` comes from an unauthenticated caller, so without the cooldown each credential-less
    request would become one outbound request to the tenant. Fifty requests, two fetches: the warm
    one and the single refresh the first unknown `kid` is allowed.
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
    """A tenant rotates its key and the front door follows, without a restart.

    The cost of the cooldown above is rotation latency, and this is the other side of that trade:
    once the window passes, the first token carrying the new `kid` pays one refresh and every later
    caller reads the refreshed cache.
    """
    monkeypatch.setattr(settings, "entra_jwks_refresh_cooldown_seconds", 0.0)
    with _client() as client:
        assert client.post("/sessions", headers=_bearer(_sign(_KEY_A, "kid-a"))).status_code == 200
        rotated = _sign(_KEY_B, "kid-b", oid="u-alice")
        assert client.post("/sessions", headers=_bearer(rotated)).status_code == 401
        issuer.publish(_jwks(("kid-a", _KEY_A), ("kid-b", _KEY_B)))
        assert client.post("/sessions", headers=_bearer(rotated)).status_code == 200


def test_an_unreachable_issuer_answers_503_and_not_401(issuer: _JwksIssuer) -> None:
    """An IdP outage is our failure, not the caller's bad credential.

    401 would tell a chemist holding a perfectly good token that it was rejected, and would hide a
    dependency failure inside the metric operators read as "someone is probing us". The token here
    is valid; only the issuer is gone.
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
    """A 200 carrying anything but a JWKS is still "we could not reach the tenant to decide".

    `IdentityProviderUnavailable` exists so that failure is a 503 rather than a 401, and its
    reasoning — "an IdP failure is our outage, not the caller's bad credential" — covers a
    *successful* HTTP response carrying junk exactly as it covers a refused connection. That is the
    common shape of an intercepting proxy, a captive portal or a tenant misconfiguration, and it
    used to be a bare HTTP 500: the wrong contract for the client ("this request is broken, do not
    retry"), a page for the on-call as an application bug, and a 5xx spike naming nothing.

    Two shapes because they fail in two different libraries: the HTML page dies in `json.load`
    (`json.JSONDecodeError`, a `ValueError`, which PyJWT's client does not convert), and the JSON
    one dies in `PyJWKSet.from_dict` (`PyJWKSetError` — a `PyJWTError` that is neither a
    `PyJWKClientError` nor an `InvalidTokenError`, so every handler in `api/auth.py` missed it).
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
            "/proposals",
            "/profiles",
            "/schedules",
            "/plans/pending",
        ):
            assert client.get(path).status_code == 401, path
