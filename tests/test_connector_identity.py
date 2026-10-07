"""What crosses the process boundary with a connector call — and what deliberately does not.

- The identity headers reflect the calling turn, so `turn_headers` reads the ambient context at
  call time; this makes a connector's request log joinable to the core audit trail.
- The auth flow reads its credential per request, so a rotated secret applies without a restart.

Negative halves: an absent actor yields no header (not an empty one), and a missing credential
raises rather than sending an empty `Authorization`. That headers arrive is a transport property,
proven against a live server in `test_connector_transport.py`.
"""

import asyncio
import shlex
from pathlib import Path
from unittest import mock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from starlette.responses import Response

from chemclaw.connectors.identity import MissingConnectorCredential, auth_for
from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint, NoAuth
from chemclaw.core.call_identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
    STAMPED_HEADERS,
    _strippable_headers,
    turn_headers,
    turn_identity_hook,
)
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.core.tracing import trace_headers
from chemclaw.core.turn_flags import reset_dry_run, set_dry_run

# A syntactically valid W3C `traceparent`, so the assertions are about a real header value
# rather than a placeholder that a stricter client would reject.
_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_no_ambient_identity_sends_no_identity_headers() -> None:
    """Off the request path there is no actor, and claiming one would corrupt an audit join."""
    headers = turn_headers()
    assert HEADER_ACTOR not in headers
    assert HEADER_SESSION not in headers
    # Dry-run is always sent: "not a dry run" is a real state, not an absence.
    assert headers[HEADER_DRY_RUN] == "false"


def test_headers_are_read_from_the_ambient_turn_at_call_time() -> None:
    """Headers are read from the ambient turn at call time.

    Anything captured at client construction or connect would attribute every call to whichever user
    came first.
    """
    identity = set_current_identity("user-1", frozenset({"process-chemist", "admin"}))
    session = set_current_session_id("session-abc")
    dry_run = set_dry_run(True)
    try:
        headers = turn_headers()
    finally:
        reset_dry_run(dry_run)
        reset_current_session_id(session)
        reset_current_identity(identity)
    assert headers[HEADER_ACTOR] == "user-1"
    assert headers[HEADER_SESSION] == "session-abc"
    assert headers[HEADER_DRY_RUN] == "true"
    # And once the turn is over, there is no identity to report again.
    assert HEADER_ACTOR not in turn_headers()


def test_the_headers_carry_only_identity_never_call_content() -> None:
    """The headers say who is calling, never what they asked for.

    `turn_headers` takes no argument, so model-authored text cannot enter the transport envelope.
    """
    import inspect

    assert inspect.signature(turn_headers).parameters == {}
    assert set(turn_headers()) == {HEADER_DRY_RUN}


def test_the_strip_list_covers_every_header_the_stamp_produces() -> None:
    """The strip list covers every header the stamp produces.

    `turn_headers()` adds `trace_headers()` (`traceparent`, `tracestate`, `baggage`) to the
    `X-Chemclaw-*` headers; on a foreign origin those carry our trace ids and arbitrary baggage. So
    the assertion covers rather than equals: whatever the stamp produces, the guard removes.
    """
    identity = set_current_identity("user-1", frozenset({"process-chemist"}))
    session = set_current_session_id("session-abc")
    correlation = set_current_correlation_id("turn-7f3a")
    try:
        stamped = {name.lower() for name in turn_headers()}
        ours = set(turn_headers()) - set(trace_headers())
    finally:
        reset_current_correlation_id(correlation)
        reset_current_session_id(session)
        reset_current_identity(identity)
    assert ours == set(STAMPED_HEADERS)
    assert stamped <= _strippable_headers()
    # And the W3C names are covered whether or not a span is live at the moment of the strip, which
    # is the case `turn_headers()` alone cannot answer for.
    with mock.patch("chemclaw.core.call_identity.trace_header_names", lambda: frozenset({"b3"})):
        assert "b3" in _strippable_headers()


def test_the_hook_strips_the_identity_when_a_request_leaves_the_connector_origin() -> None:
    """The hook strips the identity when a request leaves the connector origin (Sec-2).

    Bundle clients refuse redirects (`registry.connector_http_client`), so this is a second layer
    there and the only layer for the calc backend, whose `core.mcp_session.short_connect_client`
    follows redirects. It must strip, because httpx copies the previous request's headers (dropping
    only `Authorization`). Asserted on a redirect-following client with a trace context stamped.
    """
    seen: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer the connector's origin with a redirect elsewhere; record both requests."""
        seen[str(request.url.host)] = request.headers
        if request.url.host == "connector":
            return httpx.Response(307, headers={"Location": "http://attacker/mcp"})
        return httpx.Response(200)

    async def _post() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [turn_identity_hook("http://connector/mcp")]},
        ) as client:
            await client.post("http://connector/mcp")

    identity = set_current_identity("user-99", frozenset({"process-chemist"}))
    session = set_current_session_id("session-leak")
    # A real trace context on the wire, not a live tracer: what matters here is that the guard
    # removes the names the propagator owns, and stubbing the two producers keeps the test free of
    # an SDK, an exporter and a global provider it would otherwise have to install.
    stub = {"traceparent": _TRACEPARENT, "baggage": "tenant=acme"}
    try:
        with (
            mock.patch("chemclaw.core.call_identity.trace_headers", lambda: stub),
            mock.patch(
                "chemclaw.core.call_identity.trace_header_names",
                lambda: frozenset({"traceparent", "tracestate", "baggage"}),
            ),
        ):
            asyncio.run(_post())
    finally:
        reset_current_session_id(session)
        reset_current_identity(identity)

    assert seen["connector"][HEADER_ACTOR] == "user-99"
    assert seen["connector"][HEADER_SESSION] == "session-leak"
    assert seen["connector"]["traceparent"] == _TRACEPARENT
    # Nothing of ours survived the hop: not the identity, the flags that identify our turn, nor the
    # trace context and `baggage`.
    assert [header for header in STAMPED_HEADERS if header in seen["attacker"]] == []
    assert "traceparent" not in seen["attacker"]
    assert "baggage" not in seen["attacker"]


def test_no_auth_needs_no_credential() -> None:
    """`mode: none` is the trust-boundary case (stdio, loopback dev): nothing to attach."""
    assert auth_for(NoAuth(), "alpha") is None


def test_bearer_reads_its_token_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotated secret must take effect without a restart, so the variable is read in the flow.

    Proven by rotating it *between* two flows over the same auth object — a token captured in
    `__init__` would send the stale value the second time.
    """
    auth = auth_for(BearerAuth(token_env="CHEMCLAW_TEST_TOKEN"), "alpha")
    assert auth is not None
    monkeypatch.setenv("CHEMCLAW_TEST_TOKEN", "first")
    first = next(auth.auth_flow(httpx.Request("GET", "http://alpha/mcp")))
    assert first.headers["Authorization"] == "Bearer first"
    monkeypatch.setenv("CHEMCLAW_TEST_TOKEN", "rotated")
    second = next(auth.auth_flow(httpx.Request("GET", "http://alpha/mcp")))
    assert second.headers["Authorization"] == "Bearer rotated"


def test_a_missing_credential_raises_instead_of_sending_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named configuration error beats a 401 from a call that silently carried no credential."""
    monkeypatch.delenv("CHEMCLAW_TEST_TOKEN", raising=False)
    auth = auth_for(BearerAuth(token_env="CHEMCLAW_TEST_TOKEN"), "alpha")
    assert auth is not None
    with pytest.raises(MissingConnectorCredential, match="CHEMCLAW_TEST_TOKEN"):
        next(auth.auth_flow(httpx.Request("GET", "http://alpha/mcp")))


def test_the_correlation_id_crosses_the_connector_boundary() -> None:
    """The correlation id crosses the connector boundary (REV-11).

    `chemclaw.agent.audit` records it for every in-core tool call; carrying it lets a connector's
    records join the turn's audit trail. Advisory only: a connector must never decide access on it.
    """
    token = set_current_correlation_id("turn-7f3a")
    try:
        headers = turn_headers()
    finally:
        reset_current_correlation_id(token)
    assert headers[HEADER_CORRELATION] == "turn-7f3a"
    # Absent, not empty, once the turn is over — an empty id in a connector's log reads as one
    # that exists, which is the failure this header is meant to remove rather than reproduce.
    assert HEADER_CORRELATION not in turn_headers()


def test_a_durable_job_carries_the_turn_it_was_launched_from() -> None:
    """A durable job carries the turn it was launched from.

    A Temporal worker has no request context, so the id travels in `ConnectorJobInput` and is set as
    a workflow memo, not in `payload`, which holds only model-filled arguments.
    """
    from chemclaw.durable.connector_job import ConnectorJobInput

    job = ConnectorJobInput(
        connector="calc",
        job="compute_reaction_energy",
        workflow="CalcJobWorkflow",
        task_queue="background-jobs",
        rationale="check the barrier the reviewer asked about",
        requested_by="user-1",
        correlation_id="turn-7f3a",
    )
    assert job.correlation_id == "turn-7f3a"
    # Defaulted, so every existing caller keeps working and an off-request-path launch (the CLI, a
    # scheduled job) records the honest absence rather than a fabricated id.
    assert (
        ConnectorJobInput(
            connector="calc",
            job="compute_reaction_energy",
            workflow="CalcJobWorkflow",
            task_queue="background-jobs",
            rationale="check the barrier the reviewer asked about",
            requested_by="user-1",
        ).correlation_id
        == ""
    )


# --- The other half of the credential: something that checks it -------------------------------


def test_a_bearer_connector_refuses_an_unauthenticated_mcp_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bearer connector refuses an unauthenticated MCP request.

    Enforced as middleware, because `/mcp` is `app.mount`ed and a mount bypasses the enclosing app's
    dependencies. `/healthz` stays open (a kubelet probe has no identity); both halves are asserted.
    """
    from fastapi.testclient import TestClient

    from chemclaw.connectors.server import connector_app

    monkeypatch.setenv("CHEMCLAW_PROBE_CONNECTOR_TOKEN", "s3cret-token-value")
    monkeypatch.setattr(
        "chemclaw.connectors.server._declared_bearer_env",
        lambda name: "CHEMCLAW_PROBE_CONNECTOR_TOKEN",
    )
    # A context manager so the app's lifespan runs: `/mcp` is the mounted MCP transport and its
    # session manager is started there, so a bare `TestClient` would fail on the accepted request
    # for a reason unrelated to authorization.
    with TestClient(connector_app(FastMCP("probe"), name="probe")) as client:
        assert client.get("/healthz").status_code == 200, "probes must stay open"
        assert client.post("/mcp", json={}).status_code == 401
        assert (
            client.post(
                "/mcp", json={}, headers={"Authorization": "Bearer wrong-token"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/mcp", json={}, headers={"Authorization": "Bearer s3cret-token-value"}
            ).status_code
            != 401
        ), "the configured token must reach the MCP transport"


def test_a_bearer_connector_with_no_token_configured_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bearer connector with no token configured fails closed.

    An unset variable must not make `expected` empty and accept an empty `Authorization`.
    """
    from fastapi.testclient import TestClient

    from chemclaw.connectors.server import connector_app

    monkeypatch.delenv("CHEMCLAW_PROBE_CONNECTOR_TOKEN", raising=False)
    monkeypatch.setattr(
        "chemclaw.connectors.server._declared_bearer_env",
        lambda name: "CHEMCLAW_PROBE_CONNECTOR_TOKEN",
    )
    client = TestClient(connector_app(FastMCP("probe"), name="probe"))
    assert client.post("/mcp", json={}).status_code == 401
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer "}).status_code == 401


def test_a_mode_none_connector_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `mode: none` connector gets no middleware.

    Its boundary is the NetworkPolicy, a deployment decision; adding a check nobody declared would
    break `make connectors` and the transport tests.
    """
    from fastapi.testclient import TestClient

    from chemclaw.connectors.server import connector_app

    monkeypatch.setattr("chemclaw.connectors.server._declared_bearer_env", lambda name: None)
    with TestClient(connector_app(FastMCP("probe"), name="probe")) as client:
        assert client.post("/mcp", json={}).status_code != 401


def test_an_unreadable_manifest_makes_the_connector_refuse_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable manifest makes the connector refuse everything.

    Exercises the real `_declared_bearer_env`. `discovered()` raises on one bad YAML (e.g. a typo in
    a prepended override directory), and that must not leave bearer-mode connectors anonymous; the
    connector refuses until the manifest is fixed.
    """
    from fastapi.testclient import TestClient

    from chemclaw.connectors.registry import ConnectorError
    from chemclaw.connectors.server import _declared_bearer_env, connector_app

    def _unreadable() -> dict[str, object]:
        raise ConnectorError("/etc/connectors/other/connector.yaml: invalid manifest")

    monkeypatch.setattr("chemclaw.connectors.registry.discovered", _unreadable)
    assert _declared_bearer_env("probe") is not None, (
        "an unresolved auth mode must not read as none"
    )

    client = TestClient(connector_app(FastMCP("probe"), name="probe"))
    assert client.get("/healthz").status_code == 200, "probes stay open so the pod can be drained"
    assert client.post("/mcp", json={}).status_code == 401
    assert (
        client.post("/mcp", json={}, headers={"Authorization": "Bearer anything"}).status_code
        == 401
    )


def test_a_shipped_bundle_resolves_to_the_variable_its_manifest_names() -> None:
    """A shipped bundle resolves to the variable its manifest names.

    Rules out "fail closed always", which would look like a working gate while refusing every call.
    The `None` case is `test_an_app_no_bundle_backs_is_not_refused`.
    """
    from chemclaw.connectors.registry import enabled
    from chemclaw.connectors.server import _declared_bearer_env

    manifest = next(m for m in enabled() if m.name == "molfp")
    endpoint = manifest.endpoint
    assert isinstance(endpoint, HttpEndpoint) and isinstance(endpoint.auth, BearerAuth)
    assert _declared_bearer_env("molfp") == endpoint.auth.token_env


def test_a_shipped_bundle_that_discovery_missed_is_unresolved_not_unguarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A shipped bundle that discovery missed is unresolved, not unguarded.

    A `connectors_dir` pointing elsewhere parses fine and omits the bundle; that must refuse rather
    than serve `/mcp` anonymously. Driven by pointing `connectors_dir` at an empty directory.
    """
    from chemclaw.connectors.registry import forget_discovered
    from chemclaw.connectors.server import _UNRESOLVED_AUTH, _declared_bearer_env

    monkeypatch.setattr(settings, "connectors_dir", str(tmp_path))
    forget_discovered()
    try:
        assert _declared_bearer_env("molfp") == _UNRESOLVED_AUTH
    finally:
        forget_discovered()


def test_an_app_no_bundle_backs_is_not_refused() -> None:
    """An app no bundle backs is not refused.

    Synthetic apps (built by transport and identity tests) declare nothing, so there is no token to
    present; refusing them would be "fail closed always".
    """
    from chemclaw.connectors.server import _declared_bearer_env

    assert _declared_bearer_env("not-a-bundle") is None


def test_an_unresolved_connector_recovers_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolved connector recovers without a restart.

    `_declared` caches a resolved answer, never the fail-closed sentinel, so fixing the manifest is
    enough.
    """
    from chemclaw.connectors.server import _UNRESOLVED_AUTH, BearerAuthMiddleware

    answers = iter([_UNRESOLVED_AUTH, None])
    monkeypatch.setattr(
        "chemclaw.connectors.server._declared_bearer_env", lambda name: next(answers)
    )
    middleware = BearerAuthMiddleware(app=None, connector="probe")

    assert middleware._declared() == _UNRESOLVED_AUTH, "the first read is unresolved"
    assert middleware._declared() is None, "the fix is picked up without a restart"
    assert middleware._declared() is None, "and the resolved answer is then kept"


def test_a_non_ascii_authorization_header_is_refused_not_a_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-ASCII `Authorization` header is refused, not a server error.

    `compare_digest` on `str` raises `TypeError` unless both sides are ASCII, and Starlette decodes
    headers as latin-1. Driven through `dispatch` with a uvicorn-shaped scope, because httpx refuses
    to send such bytes.
    """
    from starlette.requests import Request

    from chemclaw.connectors.server import BearerAuthMiddleware

    monkeypatch.setenv("CHEMCLAW_PROBE_CONNECTOR_TOKEN", "s3cret-token-value")
    monkeypatch.setattr(
        "chemclaw.connectors.server._declared_bearer_env",
        lambda name: "CHEMCLAW_PROBE_CONNECTOR_TOKEN",
    )
    middleware = BearerAuthMiddleware(app=None, connector="probe")

    async def _never_called(_request: Request) -> Response:
        raise AssertionError("the request must not reach the application")

    async def _status_for(raw_header: bytes) -> int:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": [(b"authorization", raw_header)],
                "query_string": b"",
            }
        )
        response = await middleware.dispatch(request, _never_called)
        return int(response.status_code)

    for raw in (b"Bearer \xff", b"Bearer s3cret-token-valu\xe9", b"Bearer \xc3\xa9"):
        assert asyncio.run(_status_for(raw)) == 401, f"{raw!r} did not produce a clean refusal"


def test_a_tool_reads_the_caller_of_the_call_it_serves_not_of_the_handshake() -> None:
    """A tool reads the caller of the call it serves, not of the handshake.

    `CallerLogMiddleware` binds in the ASGI task, but tool bodies run in the MCP session-manager
    task created at `initialize`, so the caller must be re-bound per call. Driven over the real
    transport: handshake as alice, call as bob on the same session; the tool must read bob. This
    pins attribution, which `caller_provenance` provides.
    """
    from fastapi.testclient import TestClient

    from chemclaw.connectors.caller import caller_provenance
    from chemclaw.connectors.server import connector_app

    seen: list[tuple[str, str, str]] = []
    server = FastMCP("probe")

    @server.tool()
    def whoami() -> str:
        """Record the caller the tool body sees."""
        seen.append(caller_provenance())
        return "ok"

    def who(name: str) -> dict[str, str]:
        return {
            HEADER_ACTOR: f"{name}-oid",
            HEADER_SESSION: f"sess-{name}",
            HEADER_CORRELATION: f"corr-{name}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    # `base_url` on a loopback host because `FastMCP`'s transport ships its own DNS-rebinding
    # guard (`allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`), and TestClient's default
    # `testserver` host is refused with 421 before any of this is reached.
    with TestClient(
        connector_app(server, name="probe"), base_url="http://127.0.0.1:8000"
    ) as client:
        opened = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            },
            headers=who("alice"),
        )
        session_id = opened.headers["mcp-session-id"]
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**who("alice"), "mcp-session-id": session_id},
        )
        client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
            headers={**who("bob"), "mcp-session-id": session_id},
        )

    assert seen == [("bob-oid", "sess-bob", "corr-bob")], (
        "a tool called by bob on a session alice opened must be attributed to bob; "
        f"got {seen} — the caller is frozen at the MCP handshake again"
    )


def test_every_bundle_this_repository_hosts_authenticates_its_own_mcp() -> None:
    """Every bundle this repository hosts authenticates its own MCP.

    A NetworkPolicy selects peers, not paths, so without a credential any pod in the namespace could
    start durable work. A sweep over the enabled set, so a new bundle is covered. `chem` and
    `safety` are enforced by `Chemclaw3-mcp` on its own `/mcp`.
    """
    from chemclaw.connectors.registry import enabled

    open_endpoints = [
        manifest.name
        for manifest in enabled()
        if isinstance(manifest.endpoint, HttpEndpoint)
        and not isinstance(manifest.endpoint.auth, BearerAuth)
    ]
    assert not open_endpoints, (
        f"connector(s) {open_endpoints} serve an MCP endpoint with no credential. A NetworkPolicy "
        "selects peers, not paths, so nothing else stands between a pod in the namespace and these "
        "tools. Declare `auth: mode: bearer` with a `token_env`, add the key to "
        "`deploy/helm/chemclaw/values.yaml`'s `secrets.optionalKeys`, and let "
        "`chemclaw.cli.connectors_dev` mint it for local work."
    )


def test_a_shipped_manifests_declaration_is_what_the_gate_actually_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shipped manifest's declaration is what the gate actually reads.

    Resolution runs for real on a shipped bundle: no token is a 401, the declared value is not.
    """
    from starlette.requests import Request

    from chemclaw.connectors.registry import enabled
    from chemclaw.connectors.server import BearerAuthMiddleware

    manifest = next(m for m in enabled() if m.name == "molfp")
    endpoint = manifest.endpoint
    assert isinstance(endpoint, HttpEndpoint) and isinstance(endpoint.auth, BearerAuth)
    token_env = endpoint.auth.token_env
    monkeypatch.setenv(token_env, "a-real-looking-token")
    middleware = BearerAuthMiddleware(app=None, connector="molfp")

    reached: list[bool] = []

    async def _application(_request: Request) -> Response:
        reached.append(True)
        return Response(status_code=200)

    async def _status(headers: list[tuple[bytes, bytes]]) -> int:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": headers,
                "query_string": b"",
            }
        )
        response = await middleware.dispatch(request, _application)
        return int(response.status_code)

    assert asyncio.run(_status([])) == 401
    assert asyncio.run(_status([(b"authorization", b"Bearer wrong")])) == 401
    assert reached == [], "an unauthenticated request reached the MCP application"
    assert asyncio.run(_status([(b"authorization", b"Bearer a-real-looking-token")])) == 200
    assert reached == [True]


def test_the_probe_allowlist_survives_being_mounted_under_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe allowlist survives being mounted under a name.

    Starlette keeps the mount prefix in `scope["path"]` (recording it in `root_path`), so an
    allowlist written for `/healthz` must also match `/<name>/healthz`, as
    `chemclaw.cli.connectors_dev` mounts it. Otherwise the readiness probe gets 401 and reports the
    fleet unreachable. Driven at scope level.
    """
    from starlette.requests import Request

    from chemclaw.connectors.server import BearerAuthMiddleware

    monkeypatch.setattr(
        "chemclaw.connectors.server._declared_bearer_env", lambda name: "CHEMCLAW_PROBE_TOKEN"
    )
    monkeypatch.setenv("CHEMCLAW_PROBE_TOKEN", "s3cret")
    middleware = BearerAuthMiddleware(app=None, connector="probe")

    async def _application(_request: Request) -> Response:
        return Response(status_code=200)

    async def _status(path: str, root: str) -> int:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "root_path": root,
                "headers": [],
                "query_string": b"",
            }
        )
        response = await middleware.dispatch(request, _application)
        return int(response.status_code)

    assert asyncio.run(_status("/healthz", "")) == 200, "unmounted probe"
    assert asyncio.run(_status("/molfp/healthz", "/molfp")) == 200, "mounted probe"
    assert asyncio.run(_status("/molfp/metrics", "/molfp")) == 200, "mounted scrape"
    assert asyncio.run(_status("/molfp/livez", "/molfp")) == 200, "mounted liveness probe"
    # The exemption is the probe routes, not the prefix: everything else still needs the token.
    assert asyncio.run(_status("/molfp/mcp", "/molfp")) == 401, "mounted MCP surface"


def test_liveness_is_its_own_route_and_consults_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/livez` answers whenever the process serves HTTP, whatever else is wrong with it.

    Liveness on its own route, so a readiness check never becomes a kill. Asserted: open without a
    credential even when auth could not be resolved, and answering with the lifespan never run.
    """
    from fastapi.testclient import TestClient

    from chemclaw.connectors.registry import ConnectorError
    from chemclaw.connectors.server import connector_app

    def _unreadable() -> dict[str, object]:
        raise ConnectorError("/etc/connectors/other/connector.yaml: invalid manifest")

    monkeypatch.setattr("chemclaw.connectors.registry.discovered", _unreadable)
    # No `with`: the lifespan does not run, so nothing the app depends on has been started.
    client = TestClient(connector_app(FastMCP("probe"), name="probe"))
    response = client.get("/livez")
    assert response.status_code == 200, "liveness must answer with no credential and no lifespan"
    assert response.json() == {"status": "alive", "connector": "probe"}
    assert client.post("/mcp", json={}).status_code == 401, "the MCP surface stays refused"


def test_the_dev_runner_mints_a_credential_only_where_both_ends_are_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev runner mints a credential only where both ends are ours.

    `chem` and `safety` credentials belong to `Chemclaw3-mcp`; minting one would replace a clear
    `MissingConnectorCredential` with a confusing 401.
    """
    from chemclaw.cli.connectors_dev import bearer_token_envs, ensure_dev_tokens

    monkeypatch.delenv("CHEMCLAW_CHEM_TOKEN", raising=False)
    monkeypatch.delenv("CHEMCLAW_SAFETY_TOKEN", raising=False)
    for env_var in bearer_token_envs().values():
        monkeypatch.delenv(env_var, raising=False)

    minted, preexisting = ensure_dev_tokens()

    assert preexisting == frozenset()  # every one was unset above, so every one was minted
    assert set(minted) == set(bearer_token_envs().values())
    assert "CHEMCLAW_CHEM_TOKEN" not in minted
    assert "CHEMCLAW_SAFETY_TOKEN" not in minted
    assert all(len(token) >= 24 for token in minted.values()), "a short token is not a credential"


def test_an_operator_supplied_credential_is_kept_and_shell_quoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-supplied credential is kept and shell-quoted.

    Keeping it lets both processes agree; quoting stops a `'` breaking out of the assignment, since
    `--export-env` output is `eval`ed by `infra/live/processes.sh`.
    """
    from chemclaw.cli.connectors_dev import _export_lines, bearer_token_envs, ensure_dev_tokens

    env_var = bearer_token_envs()["molfp"]
    monkeypatch.setenv(env_var, "it's a token; echo pwned")

    minted, preexisting = ensure_dev_tokens()
    assert minted[env_var] == "it's a token; echo pwned"
    assert env_var in preexisting

    line = next(line for line in _export_lines({}, minted) if line.startswith(f"export {env_var}="))
    # Round-trip through the shell's own parser rather than asserting on the escaping: what matters
    # is the value a caller ends up with, not which of the several correct spellings we emit.
    assert shlex.split(line) == ["export", f"{env_var}=it's a token; echo pwned"]


def test_the_callers_entitlements_are_not_sent_to_a_connector() -> None:
    """The caller's entitlements are not sent to a connector.

    `D-2026-08-26-an-entitlement-set-is-not-provenance`. `X-Chemclaw-Roles` had no reader anywhere,
    was unbounded in size, and carried AD groups to servers this family does not host. Re-adding it
    needs a reader and an argument, since a connector may never decide access on it.
    """
    identity = set_current_identity("user-1", frozenset({"process-chemist", "admin"}))
    session = set_current_session_id("session-abc")
    try:
        headers = turn_headers()
    finally:
        reset_current_session_id(session)
        reset_current_identity(identity)
    assert not [name for name in headers if "role" in name.lower()], (
        f"a connector request carries the caller's entitlements again: {sorted(headers)}"
    )


def test_two_concurrent_calls_on_one_session_each_read_their_own_caller() -> None:
    """Two concurrent calls on one MCP session each read their own caller.

    `ToolNode` runs a batch in parallel on one session; if calls shared a task, the bind/reset pairs
    would interleave and mis-attribute or fail.
    """
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from chemclaw.connectors.caller import caller_provenance
    from chemclaw.connectors.server import connector_app

    seen: list[tuple[str, str, str]] = []
    server = FastMCP("probe2")

    @server.tool()
    async def slow_whoami() -> str:
        """Dawdle long enough that the two calls overlap, then record the caller."""
        await asyncio.sleep(0.25)
        seen.append(caller_provenance())
        return "ok"

    def who(name: str) -> dict[str, str]:
        return {
            HEADER_ACTOR: f"{name}-oid",
            HEADER_SESSION: f"sess-{name}",
            HEADER_CORRELATION: f"corr-{name}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    with TestClient(
        connector_app(server, name="probe2"), base_url="http://127.0.0.1:8000"
    ) as client:
        opened = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "probe2", "version": "1"},
                },
            },
            headers=who("opener"),
        )
        session_id = opened.headers["mcp-session-id"]
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**who("opener"), "mcp-session-id": session_id},
        )

        def call(name: str, request_id: int) -> None:
            client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "slow_whoami", "arguments": {}},
                },
                headers={**who(name), "mcp-session-id": session_id},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(call, "alice", 2)
            second = pool.submit(call, "bob", 3)
            first.result()
            second.result()

    assert sorted(seen) == [
        ("alice-oid", "sess-alice", "corr-alice"),
        ("bob-oid", "sess-bob", "corr-bob"),
    ], (
        f"two concurrent calls on one session read {seen}; the per-call binding interleaved "
        "and a durable row would be stamped with the other caller's identity"
    )


def test_the_dev_banner_does_not_echo_a_credential_the_operator_already_exported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev banner does not echo a credential the operator already exported.

    `--export-env` prints credentials because a caller `eval`s it; the serving banner prints only
    tokens it minted, since stdout is often a log.
    """
    from chemclaw.cli.connectors_dev import _export_lines, bearer_token_envs, ensure_dev_tokens

    envs = sorted(set(bearer_token_envs().values()))
    assert envs, "no local bundle declares a bearer credential; this test would prove nothing"
    supplied, *rest = envs
    monkeypatch.setenv(supplied, "SUPER-SECRET-PROD-TOKEN")
    for env_var in rest:
        monkeypatch.delenv(env_var, raising=False)

    tokens, preexisting = ensure_dev_tokens()
    assert preexisting == frozenset({supplied})

    banner = "\n".join(_export_lines({}, tokens, preexisting=preexisting))
    assert "SUPER-SECRET-PROD-TOKEN" not in banner, banner
    assert supplied in banner  # the operator still learns which variable is in play
    # `--export-env` is `eval`ed by a caller that needs the real value, so it still carries it.
    exported = "\n".join(_export_lines({}, tokens))
    assert "SUPER-SECRET-PROD-TOKEN" in exported
    for env_var in rest:  # a minted token is printed either way
        assert tokens[env_var] in banner


def test_a_connector_answers_the_address_it_is_configured_at_and_refuses_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector answers the address it is configured at and refuses others.

    `FastMCP(name)`'s DNS-rebinding guard admits loopback only, so a Service-name `Host` got 421.
    The connector also admits its `connector_urls` address and still refuses an unconfigured `Host`.
    """
    from fastapi.testclient import TestClient

    from chemclaw.connectors.server import connector_app

    monkeypatch.setattr("chemclaw.connectors.server._declared_bearer_env", lambda name: None)
    monkeypatch.setattr(
        settings, "connector_urls", {"probe": "http://chemclaw-connector-probe:8080/mcp"}
    )
    with TestClient(connector_app(FastMCP("probe"), name="probe")) as client:
        for host in ("chemclaw-connector-probe:8080", "127.0.0.1:8080", "localhost:8811"):
            assert client.post("/mcp", json={}, headers={"Host": host}).status_code != 421, host
        assert (
            client.post("/mcp", json={}, headers={"Host": "attacker.example:8080"}).status_code
            == 421
        )


def test_a_connector_pod_logs_the_caller_it_is_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector pod logs the caller it is serving.

    Every record carries the caller's correlation id, session and actor via
    `core.logging.ContextFilter`. Driven over the real transport, because tool bodies run in the MCP
    session-manager task. The handler pair is the one `configure_logging` installs, so credentials
    in header values are redacted in both renderings.
    """
    import logging

    from fastapi.testclient import TestClient

    from chemclaw.connectors.server import connector_app
    from chemclaw.core import logging as core_logging
    from chemclaw.core.logging import ContextFilter, JsonFormatter, SecretRedactingFilter

    secret = "s3cr3t-value-that-must-not-print"
    monkeypatch.setenv("CHEMCLAW_TEST_CLAIMED_SECRET", secret)
    monkeypatch.setattr(core_logging, "_RUNTIME_SECRET_ENVS", {"CHEMCLAW_TEST_CLAIMED_SECRET"})

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture()
    capture.addFilter(ContextFilter())
    capture.addFilter(SecretRedactingFilter())
    tool_logger = logging.getLogger("tests.connector_probe_tool")
    # Earlier tests may have quietened these loggers, so they are opened here and restored after.
    # `setLevel`, not assigning `level`: only the method clears the `isEnabledFor` cache.
    root = logging.getLogger()
    opened_loggers = [
        logging.getLogger(name)
        for name in ("chemclaw", "chemclaw.connectors", "chemclaw.connectors.server")
    ]
    saved = [(lg, lg.level, lg.disabled) for lg in (root, *opened_loggers, tool_logger)]
    for lg in (*opened_loggers, tool_logger):
        lg.disabled = False
        lg.setLevel(logging.NOTSET)
    root.setLevel(logging.INFO)
    root.addHandler(capture)
    server = FastMCP("logprobe")

    @server.tool()
    def ping() -> str:
        """Log one line from inside the tool body."""
        tool_logger.info("inside the tool")
        return "ok"

    mcp_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def who(correlation: str) -> dict[str, str]:
        return {
            **mcp_headers,
            HEADER_ACTOR: "oid-alice",
            HEADER_SESSION: "sess-alice",
            HEADER_CORRELATION: correlation,
        }

    def call(client: TestClient, session_id: str, headers: dict[str, str], rid: int) -> None:
        client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {}},
            },
            headers={**headers, "mcp-session-id": session_id},
        )

    try:
        with TestClient(
            connector_app(server, name="logprobe"), base_url="http://127.0.0.1:8000"
        ) as client:
            opened = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "logprobe", "version": "1"},
                    },
                },
                headers=who("corr-handshake"),
            )
            session_id = opened.headers["mcp-session-id"]
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={**who("corr-handshake"), "mcp-session-id": session_id},
            )
            records.clear()
            call(client, session_id, who("corr-call"), 2)
            identified = list(records)
            records.clear()
            call(client, session_id, mcp_headers, 3)
            anonymous = list(records)
            records.clear()
            call(client, session_id, who(f"corr-{secret}"), 4)
            leaky = list(records)
    finally:
        root.removeHandler(capture)
        for lg, level, disabled in saved:
            lg.disabled = disabled
            lg.setLevel(level)

    def ours(batch: list[logging.LogRecord]) -> list[logging.LogRecord]:
        names = {"chemclaw.connectors.server", tool_logger.name}
        return [record for record in batch if record.name in names]

    def ids(record: logging.LogRecord) -> tuple[str, str, str]:
        return (record.correlation_id, record.session_id, record.actor)  # type: ignore[attr-defined]

    served = ours(identified)
    assert {record.name for record in served} == {"chemclaw.connectors.server", tool_logger.name}
    # The tool call's own id, not the handshake's: the per-tool-call binding is what the tool
    # body's line reads, and that is the hop where the earlier defect also froze attribution.
    assert {ids(record) for record in served} == {("corr-call", "sess-alice", "oid-alice")}
    request_line = next(r for r in served if r.name == "chemclaw.connectors.server")
    assert "correlation=corr-call" in request_line.getMessage()

    unattributed = ours(anonymous)
    assert unattributed, "the header-less call logged nothing to check"
    assert {ids(record) for record in unattributed} == {("-", "-", "-")}
    assert (
        "correlation=-"
        in next(r for r in unattributed if r.name == "chemclaw.connectors.server").getMessage()
    )

    scrubbed = ours(leaky)
    assert scrubbed
    text = logging.Formatter(settings.log_format)
    json_formatter = JsonFormatter()
    for record in scrubbed:
        assert secret not in record.correlation_id  # type: ignore[attr-defined]
        assert secret not in text.format(record)
        assert secret not in json_formatter.format(record)


def test_a_claimed_caller_is_log_attribution_and_never_an_identity() -> None:
    """A claimed caller is log attribution, never an identity.

    The read-only MCP face runs core tools behind this transport, where `get_current_actor` feeds
    every authorization gate, so the claimed caller has its own variable and an identity this
    process bound wins the record's fields.
    """
    import logging

    from chemclaw.connectors.caller import bind_caller, reset_caller
    from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
    from chemclaw.core.logging import ContextFilter
    from chemclaw.core.session_context import get_current_session_id

    tokens = bind_caller("oid-claimed", "sess-claimed", "corr-claimed")
    try:
        assert (get_current_actor(), get_current_session_id(), get_current_correlation_id()) == (
            None,
            None,
            None,
        )
        bound = set_current_correlation_id("corr-own")
        try:
            record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
            ContextFilter().filter(record)
        finally:
            reset_current_correlation_id(bound)
    finally:
        reset_caller(tokens)
    assert (record.correlation_id, record.session_id, record.actor) == (  # type: ignore[attr-defined]
        "corr-own",
        "sess-claimed",
        "oid-claimed",
    )
