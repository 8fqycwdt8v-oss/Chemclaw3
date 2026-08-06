"""A connector requires the fleet's credential, and refuses to be an open one by accident.

The defect these pin, in the shape it was found (D-2026-08-06 authorization lane): a connector
Service authenticated nobody. Anything that could open a socket to the pod could call its whole tool
surface — including the index and write tools deliberately kept off the agent's `allowed_tools` —
and could set `X-Chemclaw-Actor` to any chemist it liked, which a bundle's tool stamps into
`bo_campaigns.opened_by` as durable GxP attribution. The manifest's `auth:` block existed and
described only how *core* authenticates outward; nothing described what a connector requires inward.

Two halves, and both are tested here because either alone is a hole: the server refuses a request
with no credential, and a deployment cannot quietly *become* one that requires none — off loopback,
with nothing to present, startup refuses rather than serving.
"""

import asyncio
import logging
import threading
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from chemclaw.connectors.identity import require_secure_channel
from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint, NoAuth
from chemclaw.connectors.server import connector_app
from tests.conftest import _free_port

_TOKEN_ENV = "CHEMCLAW_CONNECTOR_TOKEN"
_TOKEN = "fleet-token-value"


@pytest.fixture
def credentialled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A deployment that names a connector credential, with the secret present in this process."""
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", _TOKEN_ENV)
    monkeypatch.setenv(_TOKEN_ENV, _TOKEN)
    yield


class _Server:
    """A uvicorn server on a background thread, started and stopped around one test.

    A copy of `test_connector_transport`'s, deliberately: that module's is bound to its
    module-scoped composite fixture, and these tests each need a *differently configured* app —
    which is the thing under test.
    """

    def __init__(self, app: FastAPI, port: int) -> None:
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "_Server":
        """Start the server and wait until it is actually accepting connections."""
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return self
            threading.Event().wait(0.05)
        raise RuntimeError("connector test server did not start")

    def __exit__(self, *_exc: object) -> None:
        """Ask uvicorn to exit and wait for the thread, so no server outlives its test."""
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _probe_app(name: str, recorded: list[tuple[str, str]]) -> FastAPI:
    """A connector app whose one tool records the caller it was told about.

    The recording tool is what makes the attribution consequence visible rather than implied: the
    question is not only "was the request refused" but "could an unauthenticated caller get a name
    of its choosing into a record".
    """
    server = FastMCP(name)

    @server.tool()
    async def whoami() -> str:
        """Record the caller this connector would attribute a row to."""
        from chemclaw.connectors.caller import caller_provenance

        actor, session, _correlation = caller_provenance()
        recorded.append((actor, session))
        return "ok"

    return connector_app(server, name=name)


def _post_mcp(port: int, *, token: str | None) -> httpx.Response:
    """One raw MCP-shaped POST, with or without a credential — an attacker's view of the port.

    Raw rather than through `DegradingHttpConnector` on purpose: the client would attach the fleet
    credential for us, which is precisely the thing being withheld.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Chemclaw-Actor": "someone-elses-oid",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(
        f"http://127.0.0.1:{port}/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout=5,
    )


def test_a_call_without_the_credential_is_refused(credentialled: None) -> None:
    """The finding itself: an unauthenticated caller reached every tool and could name any chemist.

    Asserted at the transport, before any tool runs, because that is where the fix lives — scoring
    each record's trustworthiness afterwards would be a second, weaker answer to a question the
    channel can answer once.
    """
    recorded: list[tuple[str, str]] = []
    port = _free_port()
    with _Server(_probe_app("auth-probe", recorded), port):
        response = _post_mcp(port, token=None)
    assert response.status_code == 401
    assert not recorded, "an unauthenticated request reached a tool body"


def test_a_call_with_the_wrong_credential_is_refused(credentialled: None) -> None:
    """A token that is not this fleet's is no better than none — same refusal, same reason."""
    recorded: list[tuple[str, str]] = []
    port = _free_port()
    with _Server(_probe_app("auth-probe-wrong", recorded), port):
        response = _post_mcp(port, token="not-the-fleet-token")
    assert response.status_code == 401
    assert not recorded


def test_a_call_with_the_credential_is_served(credentialled: None) -> None:
    """The other direction, or the test above would pass on a connector that refuses everything.

    Not a 401 is the whole assertion: what the MCP layer answers a bare `tools/list` with (a session
    error, without the initialize handshake) is that layer's business and not this boundary's.
    """
    recorded: list[tuple[str, str]] = []
    port = _free_port()
    with _Server(_probe_app("auth-probe-ok", recorded), port):
        response = _post_mcp(port, token=_TOKEN)
    assert response.status_code != 401


@pytest.mark.parametrize("route", ["/healthz", "/metrics"])
def test_the_probe_and_the_scrape_stay_open(credentialled: None, route: str) -> None:
    """The kubelet and Prometheus hold no credential, and neither route serves a tool.

    Requiring one here would trade a real liveness signal for no property at all — the pod network
    could already reach both, and neither reads an identity header or writes a row.
    """
    recorded: list[tuple[str, str]] = []
    port = _free_port()
    with _Server(_probe_app("auth-probe-open", recorded), port):
        response = httpx.get(f"http://127.0.0.1:{port}{route}", timeout=5)
    assert response.status_code == 200


def test_a_server_missing_the_secret_refuses_rather_than_serving_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment asked for a closed connector and this process cannot be one: refuse, loudly.

    The failure mode being excluded is the one that reads as working: serving every request
    unauthenticated because the variable the chart named is unset on *this* pod, while the client
    keeps sending a credential nobody checks.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", _TOKEN_ENV)
    monkeypatch.delenv(_TOKEN_ENV, raising=False)
    recorded: list[tuple[str, str]] = []
    port = _free_port()
    with _Server(_probe_app("auth-probe-nosecret", recorded), port):
        response = _post_mcp(port, token=_TOKEN)
    assert response.status_code == 503
    assert not recorded


def test_no_credential_configured_serves_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loopback dev fleet is unchanged, which is what keeps `connector_token_env` adoptable.

    The boundary there is the machine, not a token, and `require_secure_channel` is what stops that
    reading from being borrowed by a deployment where it is false.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", "")
    recorded: list[tuple[str, str]] = []
    port = _free_port()
    with _Server(_probe_app("auth-probe-none", recorded), port):
        response = _post_mcp(port, token=None)
    assert response.status_code != 401


def test_an_off_loopback_connector_with_no_credential_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: a deployment cannot become an open connector fleet by omission.

    `mode: none` says "inside our own trust boundary". Off loopback with nothing to present, that
    sentence is false, and the deployment finds out at startup rather than through a forged
    attribution nobody notices.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", "")
    monkeypatch.setattr("chemclaw.core.config.settings.service_allow_insecure", False)
    with pytest.raises(RuntimeError, match="no credential"):
        require_secure_channel("alpha", "http://alpha.svc:8080/mcp", NoAuth())


def test_loopback_needs_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev path stays a path: reaching a connector on this machine is its own boundary."""
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", "")
    require_secure_channel("alpha", "http://127.0.0.1:8815/mcp", NoAuth())


def test_the_fleet_credential_satisfies_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential is the other way to be legitimate, which is the shipped cluster shape."""
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", _TOKEN_ENV)
    monkeypatch.setenv(_TOKEN_ENV, _TOKEN)
    require_secure_channel("alpha", "http://alpha.svc:8080/mcp", NoAuth())


def test_a_connectors_own_bearer_satisfies_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A third-party connector with its own credential is untouched by the fleet token."""
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", "")
    require_secure_channel("vendor", "https://vendor.example/mcp", BearerAuth(token_env="VENDOR"))


def test_the_explicit_opt_out_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """One loud env var, as the front door offers for its own unauthenticated mode (SEC-2).

    A deployment may have a boundary this process cannot see. It has to say so, and it is warned
    about per connector rather than trusted silently.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", "")
    monkeypatch.setattr("chemclaw.core.config.settings.service_allow_insecure", True)
    require_secure_channel("alpha", "http://alpha.svc:8080/mcp", NoAuth())


def test_the_client_sends_the_fleet_token_to_a_none_mode_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mode: none` means "inside our boundary", so it is reached with our own credential.

    Proven by the bytes rather than by the auth object's type: what matters is that the header
    arrives, since the server's refusal is keyed on it.
    """
    from chemclaw.connectors.registry import connector_http_client

    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", _TOKEN_ENV)
    monkeypatch.setenv(_TOKEN_ENV, _TOKEN)
    endpoint = HttpEndpoint(url="http://127.0.0.1:8815/mcp")
    client = connector_http_client("alpha", endpoint)
    request = client.build_request("POST", "http://127.0.0.1:8815/mcp")
    flow = client.auth.auth_flow(request)  # type: ignore[union-attr]
    assert next(flow).headers["Authorization"] == f"Bearer {_TOKEN}"


def test_the_fleet_token_does_not_displace_a_third_party_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector with its own `bearer` keeps it — sending ours would hand out the fleet key."""
    from chemclaw.connectors.registry import connector_http_client

    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", _TOKEN_ENV)
    monkeypatch.setenv(_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv("VENDOR_TOKEN", "vendor-secret")
    endpoint = HttpEndpoint(
        url="https://vendor.example/mcp", auth=BearerAuth(token_env="VENDOR_TOKEN")
    )
    client = connector_http_client("vendor", endpoint)
    request = client.build_request("POST", "https://vendor.example/mcp")
    flow = client.auth.auth_flow(request)  # type: ignore[union-attr]
    assert next(flow).headers["Authorization"] == "Bearer vendor-secret"


def test_the_startup_check_covers_every_enabled_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot-time, not first-turn: a misconfigured fleet is a failed rollout, not a bad answer.

    `connector_http_client` enforces the same rule on every call, so this is about *when* — and the
    difference matters, because the first-turn version fails a chemist's question instead of a
    deploy.
    """
    from chemclaw.connectors.health import check_connectors_at_startup
    from chemclaw.connectors.registry import discovered

    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", "")
    monkeypatch.setattr("chemclaw.core.config.settings.service_allow_insecure", False)
    monkeypatch.setattr(
        "chemclaw.core.config.settings.connector_urls",
        {name: f"http://{name}.svc:8080/mcp" for name in discovered()},
    )
    with pytest.raises(RuntimeError, match="no credential"):
        asyncio.run(check_connectors_at_startup())


def test_the_fleet_credential_is_registered_for_log_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential this process holds must be scrubbed from its logs, and this one is held by name.

    `logging._SECRET_SETTINGS` cannot cover it: that inventory lists settings whose *value* is the
    secret, and `connector_token_env` holds the variable's **name** — the same shape as a warehouse
    binding's credentials, which is why `register_secret_env` exists. Without this, the fleet token
    would be readable in any line that echoed the client's own configuration.
    """
    from chemclaw.connectors.identity import auth_for
    from chemclaw.core.logging import redact_secrets

    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", _TOKEN_ENV)
    monkeypatch.setenv(_TOKEN_ENV, _TOKEN)
    auth_for(NoAuth(), "alpha")
    assert _TOKEN not in redact_secrets(f"connect failed with Authorization: Bearer {_TOKEN}")


def test_the_probe_is_exempt_under_the_dev_composite_mounting(credentialled: None) -> None:
    """The exemption follows the app, not one spelling of its path.

    Found reviewing the fix rather than writing it. A connector app serves at the root in a cluster
    (`server_entry`) and is mounted under `/<name>` by the dev composite, so an exact-path exemption
    passes every production test and 401s every kubelet probe the day someone runs the composite
    with a credential configured — which reads as "the connectors are down", not as "the check is
    wrong".
    """
    from chemclaw.connectors.server import is_unauthenticated_route

    for path in ("/healthz", "/metrics", "/calc/healthz", "/calc/metrics", "/healthz/"):
        assert is_unauthenticated_route(path), path
    # Nothing that serves a tool can borrow the exemption.
    for path in ("/mcp", "/calc/mcp", "/", "/healthzz"):
        assert not is_unauthenticated_route(path), path


def test_a_refused_request_is_logged(credentialled: None, caplog: pytest.LogCaptureFixture) -> None:
    """An unauthenticated probe of the port must leave a trace, and nearly did not.

    `CallerLogMiddleware` sits *inside* the credential check, so it never runs for a refused
    request: without a line here, someone sweeping a connector's port would appear nowhere in its
    log — on the one process family whose whole surface is capability. The claimed actor is included
    because "someone unauthenticated claimed to be X" is what an operator needs; it is logged and
    never believed.
    """
    recorded: list[tuple[str, str]] = []
    port = _free_port()
    with caplog.at_level(logging.WARNING, logger="chemclaw.connectors.server"):
        with _Server(_probe_app("auth-probe-logged", recorded), port):
            _post_mcp(port, token=None)
    assert "refused an unauthenticated request" in caplog.text
    assert "someone-elses-oid" in caplog.text, "the claimed actor is not in the refusal line"


def test_a_pod_without_its_secret_reports_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector that refuses every call must not answer "ok" to the probe.

    The third review finding. The middleware already 503s every tool call when the variable the
    chart named is unset in this process — but `/healthz` answered `{"status": "ok"}` regardless, so
    the pod stayed in rotation and the front door's `/readyz`, which reads exactly this route,
    agreed that it was healthy. A connector that can serve nothing is not healthy, and this is the
    one place that can say so.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connector_token_env", _TOKEN_ENV)
    monkeypatch.delenv(_TOKEN_ENV, raising=False)
    recorded: list[tuple[str, str]] = []
    port = _free_port()
    with _Server(_probe_app("auth-probe-unhealthy", recorded), port):
        response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5)
    assert response.status_code == 503
    assert response.json()["status"] == "credential-unavailable"
    assert _TOKEN_ENV in response.json()["detail"]
