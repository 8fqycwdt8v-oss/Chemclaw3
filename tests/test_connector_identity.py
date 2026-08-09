"""What crosses the process boundary with a connector call — and what deliberately does not.

Two mechanisms, tested for the two different reasons they exist:

- The **identity headers** must reflect the turn that is calling, so `turn_headers` is tested for
  reading the *ambient* context rather than anything captured earlier — the property that makes
  a connector's own request log joinable to the core audit trail.
- The **auth flow** must read its credential per request, so a rotated secret takes effect without a
  restart rather than pinning whatever was mounted when the client was built.

Both have a negative half worth pinning: an absent actor must yield an absent header (not an empty
one, which would let a connector's log claim an anonymous user made the call), and a missing
credential must raise rather than send an empty `Authorization`.

That the headers actually *arrive* is a transport property, proven against a live server in
`test_connector_transport.py` — it cannot be shown here, and assuming it is exactly the mistake
that
made MAF's own `header_provider` look usable (see `chemclaw.connectors.identity`).
"""

import asyncio

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from starlette.responses import Response

from chemclaw.agent.turn_flags import reset_dry_run, set_dry_run
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_ROLES,
    HEADER_SESSION,
    STAMPED_HEADERS,
    MissingConnectorCredential,
    auth_for,
    turn_headers,
    turn_identity_hook,
)
from chemclaw.connectors.manifest import BearerAuth, NoAuth
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.core.tracing import trace_headers


def test_no_ambient_identity_sends_no_identity_headers() -> None:
    """Off the request path there is no actor, and claiming one would corrupt an audit join."""
    headers = turn_headers()
    assert HEADER_ACTOR not in headers
    assert HEADER_ROLES not in headers
    assert HEADER_SESSION not in headers
    # Dry-run is always sent: "not a dry run" is a real state, not an absence.
    assert headers[HEADER_DRY_RUN] == "false"


def test_headers_are_read_from_the_ambient_turn_at_call_time() -> None:
    """The property the whole design rests on: the headers describe the turn in flight.

    Anything captured earlier — at client construction, at connect — would make every call in
    the process report whichever user happened to be first, which is precisely the
    misattribution the per-turn connector lifetime exists to prevent.
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
    # Sorted and space-delimited (the OAuth `scope` convention), so two calls by one user match.
    assert headers[HEADER_ROLES] == "admin process-chemist"
    assert headers[HEADER_SESSION] == "session-abc"
    assert headers[HEADER_DRY_RUN] == "true"
    # And once the turn is over, there is no identity to report again.
    assert HEADER_ACTOR not in turn_headers()


def test_the_headers_carry_only_identity_never_call_content() -> None:
    """The headers say *who* is calling, never *what* they asked for.

    Nothing from the tool call reaches them by construction — `turn_headers` takes no argument
    at all — which is the point: model-authored text in the transport envelope would be read as
    our own metadata by a connector's request log and by any intermediary.
    """
    import inspect

    assert inspect.signature(turn_headers).parameters == {}
    assert set(turn_headers()) == {HEADER_DRY_RUN}


def test_stamped_headers_lists_every_header_this_module_writes() -> None:
    """The strip list and the stamp list must not drift, or one header outlives the guard.

    `turn_identity_hook` removes exactly `STAMPED_HEADERS` when a request leaves the connector's
    origin, so a new `X-Chemclaw-*` header that is stamped and not listed would be the single one
    that still walks a redirect. Standard headers are excluded deliberately: `traceparent` grants
    nothing, and pruning it would only orphan a span.
    """
    identity = set_current_identity("user-1", frozenset({"process-chemist"}))
    session = set_current_session_id("session-abc")
    correlation = set_current_correlation_id("turn-7f3a")
    try:
        ours = set(turn_headers()) - set(trace_headers())
    finally:
        reset_current_correlation_id(correlation)
        reset_current_session_id(session)
        reset_current_identity(identity)
    assert ours == set(STAMPED_HEADERS)


def test_the_hook_strips_the_identity_when_a_request_leaves_the_connector_origin() -> None:
    """Defence in depth for Sec-2: the hook is bound to one origin and prunes on any other.

    The client refuses redirects (`registry.connector_http_client`), so this is the second layer —
    what protects the header set if that flag is ever restored to the MCP SDK's own default. It has
    to *strip* rather than decline to re-add: httpx builds a redirected request from the previous
    request's headers and drops only `Authorization`, so a hook that merely skipped a foreign origin
    would let the originals travel untouched. Asserted on a client that *does* follow redirects,
    because that is the configuration the guard exists for.
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
    try:
        asyncio.run(_post())
    finally:
        reset_current_session_id(session)
        reset_current_identity(identity)

    assert seen["connector"][HEADER_ACTOR] == "user-99"
    assert seen["connector"][HEADER_ROLES] == "process-chemist"
    assert seen["connector"][HEADER_SESSION] == "session-leak"
    # Nothing of ours survived the hop, including the flags that are not identity themselves but
    # would still tell an eavesdropper which of our turns it is looking at.
    assert [header for header in STAMPED_HEADERS if header in seen["attacker"]] == []


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
    """The audit trail joins across processes, on the key core already stamps (REV-11).

    `chemclaw.agent.audit` records a correlation id for every in-core tool call, and the connector
    serving
    that call logged under an id of its own with nothing tying the two together. "Show me everything
    that happened in this turn" was therefore answerable in core and unanswerable across the four
    runtimes a turn actually spans — which is most of what an audit trail is for.

    Advisory like the rest of these headers: a connector may join its records to ours on it and must
    never make an access decision on it.
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
    """The other half of the same gap: a durable run must not be an island in the trail.

    `ConnectorJobInput` reaches a Temporal worker that has no request context, so the id has to
    travel in the input — the same argument that puts `requested_by` there. It is then set as a
    workflow *memo* rather than folded into `payload`, because `payload` is exactly the arguments
    the model filled in, and metadata the LLM can write is not metadata.
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
    """`mode: bearer` was send-only — this file tested the sending half and nothing tested a check.

    `_EnvBearerAuth` above puts an `Authorization` header on every call, and no connector ever read
    one: `connector_app` took no manifest, the string "Authorization" did not appear in
    `connectors/server.py`, and `connector-validate` raised no objection. A deployment following
    the manifest's own advice ("bearer for everything in-cluster") mounted a secret, recorded the
    control as enabled, and served every tool to anything that could reach the pod. Proved before
    the fix by completing an unauthenticated MCP handshake against the real app.

    Enforced as middleware rather than a route dependency, and that is why the gap was easy to
    miss: `/mcp` is `app.mount`ed, and a mount bypasses the enclosing app's dependencies — anything
    written as `Depends(...)` would have guarded the two routes that need it least.

    `/healthz` stays open deliberately (a kubelet probe carries no identity), so both halves are
    asserted here or the fix would be a liveness outage rather than a control.
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
    """A missing secret must refuse, not compare against `""` and accept every caller.

    The failure mode this rules out is the one that looks like success: an unset variable making
    `expected` empty, an empty `Authorization` header matching it, and the connector serving
    everything while the deployment believes the credential is in force.
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
    """Every shipped bundle declares `auth: mode: none`; the middleware must not appear for them.

    The boundary for those is the NetworkPolicy, which is a deployment decision, not a code one —
    so adding a check where none is declared would break `make connectors` and the transport tests
    without any manifest asking for it.
    """
    from fastapi.testclient import TestClient

    from chemclaw.connectors.server import connector_app

    monkeypatch.setattr("chemclaw.connectors.server._declared_bearer_env", lambda name: None)
    with TestClient(connector_app(FastMCP("probe"), name="probe")) as client:
        assert client.post("/mcp", json={}).status_code != 401


def test_an_unreadable_manifest_makes_the_connector_refuse_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real `_declared_bearer_env`, on the path where it decides whether a control exists.

    The three tests above monkeypatch that function away, so the function that decides whether the
    connector is guarded at all was executed by none of them — which is how its first version came
    to fail *open*. `discovered()` parses every bundle in `connectors_dirs` and raises on one bad
    YAML, so a typo in an operator's prepended directory (the documented PATH-like override), or a
    mount briefly unreadable at pod start, took every bearer-mode connector in the process
    anonymous while logging only that it "could not read manifests to resolve its auth mode".

    A control whose absence is decided by a file being unreadable is not a control. The connector
    now refuses until an operator fixes the manifest.
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


def test_a_mode_none_bundle_resolves_to_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other real-resolution case: every shipped bundle declares `mode: none`.

    Without this, "fail closed on an unreadable manifest" could be satisfied by failing closed
    always, which would break `make connectors`, the dev composite and the transport tests.
    """
    from chemclaw.connectors.server import _declared_bearer_env

    assert _declared_bearer_env("molfp") is None
    assert _declared_bearer_env("not-a-bundle") is None


def test_a_non_ascii_authorization_header_is_refused_not_a_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`compare_digest` on `str` raises `TypeError` unless both sides are ASCII.

    Starlette decodes header bytes as latin-1, so one non-ASCII byte turned the auth boundary into
    a 500 with a traceback that any remote party could produce at will — and made "fail closed" a
    property of an exception handler upstream rather than of the branch written for it.

    Driven through the middleware's own `dispatch` rather than `TestClient`, because httpx encodes
    outgoing headers as ASCII and refuses to send the bytes a real server accepts. The scope is the
    shape uvicorn builds: raw bytes, decoded latin-1 by Starlette.
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
    """The identity a connector stamps on a durable row must be the one that asked for the row.

    `CallerLogMiddleware` binds the caller in `dispatch`, an ASGI task — but a tool body runs in
    the MCP session-manager task created by `initialize`, so the contextvar it read was whatever
    the *handshake* set, for the whole life of the MCP session. Measured over the real
    streamable-HTTP transport, handshaking with alice's headers and then calling the tool with
    bob's on the same `mcp-session-id`: the tool body read
    `('alice-oid', 'sess-alice', 'corr-alice')`. The middleware's own log line for that same call
    printed bob, because it reads the headers directly — so the log and the durable row this
    feature exists to reconcile disagreed with each other.

    Two docstrings asserted the opposite ("each request runs in its own task context, so a
    ContextVar set here is already invisible to the next one"; "so one request's identity cannot
    leak into the next"). Both are corrected, and this is the test that keeps the corrected
    version true.

    Not a cross-user leak today — two independent MCP sessions showed no bleed — so what this
    pins is attribution, which is exactly what `caller_provenance` exists to provide.
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
