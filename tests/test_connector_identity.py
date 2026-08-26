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
import shlex
from pathlib import Path

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from starlette.responses import Response

from chemclaw.agent.turn_flags import reset_dry_run, set_dry_run
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
    STAMPED_HEADERS,
    MissingConnectorCredential,
    auth_for,
    turn_headers,
    turn_identity_hook,
)
from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint, NoAuth
from chemclaw.core.config import settings
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


def test_a_shipped_bundle_resolves_to_the_variable_its_manifest_names() -> None:
    """The other real-resolution case: a discovered bundle resolves to a *name*, not a sentinel.

    Without this, "fail closed on an unreadable manifest" could be satisfied by failing closed
    always — which would refuse every MCP call in the dev composite and the transport tests, and
    look exactly like a working credential gate while gating nothing that could ever open.

    This used to assert `is None` for `molfp`, on the premise that every shipped bundle declared
    `auth: mode: none`. That premise was the finding rather than the fixture: the four bundles this
    repository hosts served their whole tool surface to anything that could reach the pod. The
    assertion is now the positive one, and `test_an_app_no_bundle_backs_is_not_refused` carries the
    `None` case it used to stand in for.
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
    """A bundle that ships a manifest this process did not discover must refuse, not open.

    The *likelier* half of what the unreadable-manifest test above closes. A raise failed closed;
    a `discovered()` that simply comes back without this bundle in it fell through to "no
    credential required" and served the whole `/mcp` surface anonymously. Nothing has to be corrupt
    for that — a `connectors_dir` pointing elsewhere, or an operator's prepended override directory
    shadowing the tree, both parse perfectly well — and the deployment goes on recording the pod as
    credential-gated, which is what makes the open answer worse than an outage.

    Driven by pointing `connectors_dir` at an empty directory, which is the misconfiguration
    itself rather than a stand-in for it.
    """
    from chemclaw.connectors.registry import discovered
    from chemclaw.connectors.server import _UNRESOLVED_AUTH, _declared_bearer_env

    monkeypatch.setattr(settings, "connectors_dir", str(tmp_path))
    discovered.cache_clear()
    try:
        assert _declared_bearer_env("molfp") == _UNRESOLVED_AUTH
    finally:
        discovered.cache_clear()


def test_an_app_no_bundle_backs_is_not_refused() -> None:
    """Undiscovered is not undeclared — a synthetic app stays open, and that is the boundary.

    `connector_app` serves apps no bundle backs at all: every transport and identity test builds
    one. Nothing was declared for those names, so there is no promise to betray and no token
    anybody could present — failing closed there would only mean "fail closed always", which the
    `mode: none` test above exists to prevent.
    """
    from chemclaw.connectors.server import _declared_bearer_env

    assert _declared_bearer_env("not-a-bundle") is None


def test_an_unresolved_connector_recovers_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_declared` caches a resolved answer, never the fail-closed sentinel.

    `_declared_bearer_env` promises the connector "answers 401 until an operator fixes the
    manifest". Latching `_resolved` unconditionally made that "…and restarts the pod", because the
    middleware never asked again — so the remedy the log line names did not work.
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


def test_every_bundle_this_repository_hosts_authenticates_its_own_mcp() -> None:
    """A bundle we serve declares a credential — asserted over the manifests, not remembered.

    Four of the six endpoint-serving bundles shipped `auth: mode: none` (`bo`, `calc`, `molfp`,
    `rxnfp`). The NetworkPolicy was the only thing between any pod in the namespace and a tool that
    starts durable HPC work, and a NetworkPolicy selects peers rather than paths — so a compromised
    or merely curious neighbour in the same namespace could launch one.

    Written as a sweep over the enabled set rather than a list of four names, because the failure
    this guards against is the *fifth* bundle: a list would still pass the day someone adds one
    with `mode: none`, which is exactly how the first four came to be that way.

    `chem` and `safety` are covered by the same sweep and were already correct — their credential
    belongs to `Chemclaw3-mcp`, which enforces it on its own `/mcp`.
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
    """The declaration reaches the middleware: no token is a 401, the declared value is not.

    The other bearer tests here patch `_declared_bearer_env`, which proves the *gate* and says
    nothing about whether a real `connector.yaml` reaches it. This one names a shipped bundle and
    lets resolution run for real, so a manifest that stopped declaring a credential — or declared
    one under a variable nothing sets — fails here rather than in a cluster.
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
    """`/healthz` stays open when the app is mounted at `/<name>`, which is how dev serves it.

    Starlette does not strip a mount prefix from `scope["path"]` — it records it in `root_path` and
    leaves the path whole — so an allowlist written against `/healthz` matches at the root and
    silently stops matching under a mount. Each connector is its own Deployment in the cluster, so
    the bug was invisible there; `chemclaw.cli.connectors_dev` mounts every bundle under its name,
    which is what `make connectors`, the live lane and `tests/test_connector_transport.py` run.

    It was invisible everywhere until the four bundles we host declared a credential, because
    nothing was refused. With one declared, the readiness probe `connectors.health` makes against
    `health_url` would have come back 401 and reported the whole fleet unreachable.

    Driven at the scope level, mount prefix and all, because the shape is the whole point.
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
    # The exemption is the probe routes, not the prefix: everything else still needs the token.
    assert asyncio.run(_status("/molfp/mcp", "/molfp")) == 401, "mounted MCP surface"


def test_the_dev_runner_mints_a_credential_only_where_both_ends_are_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token is invented for the bundles we serve, and never for someone else's server.

    `chem` and `safety` declare a bearer too, and that credential belongs to `Chemclaw3-mcp`.
    Minting a random value for one would replace a clear `MissingConnectorCredential` naming the
    unset variable with a 401 from a server that has never heard of the token — a worse failure,
    and a slower one to diagnose. A secret is only ours to invent when both ends of the call are.
    """
    from chemclaw.cli.connectors_dev import bearer_token_envs, ensure_dev_tokens

    monkeypatch.delenv("CHEMCLAW_CHEM_TOKEN", raising=False)
    monkeypatch.delenv("CHEMCLAW_SAFETY_TOKEN", raising=False)
    for env_var in bearer_token_envs().values():
        monkeypatch.delenv(env_var, raising=False)

    minted = ensure_dev_tokens()

    assert set(minted) == set(bearer_token_envs().values())
    assert "CHEMCLAW_CHEM_TOKEN" not in minted
    assert "CHEMCLAW_SAFETY_TOKEN" not in minted
    assert all(len(token) >= 24 for token in minted.values()), "a short token is not a credential"


def test_an_operator_supplied_credential_is_kept_and_shell_quoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing value survives untouched, and the printed export cannot break out of its quotes.

    Both halves matter. Keeping the value is what lets a caller decide the secret and have both
    processes agree on it. Quoting it is what stops a value carrying a `'` from ending the
    assignment early and turning the rest of a *credential* into shell words — the output of
    `--export-env` is `eval`ed by `infra/live/processes.sh`.
    """
    from chemclaw.cli.connectors_dev import _export_lines, bearer_token_envs, ensure_dev_tokens

    env_var = bearer_token_envs()["molfp"]
    monkeypatch.setenv(env_var, "it's a token; echo pwned")

    minted = ensure_dev_tokens()
    assert minted[env_var] == "it's a token; echo pwned"

    line = next(line for line in _export_lines({}, minted) if line.startswith(f"export {env_var}="))
    # Round-trip through the shell's own parser rather than asserting on the escaping: what matters
    # is the value a caller ends up with, not which of the several correct spellings we emit.
    assert shlex.split(line) == ["export", f"{env_var}=it's a token; echo pwned"]


def test_the_callers_entitlements_are_not_sent_to_a_connector() -> None:
    """`X-Chemclaw-Roles` is gone and must not come back without a reader.

    `D-2026-08-26-an-entitlement-set-is-not-provenance`. An *absence* test, the shape
    D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution established for a claim
    with no producer, applied to the mirror case: a value with no consumer. It had one writer here
    and, measured across this repository and `Chemclaw3-mcp`, no reader anywhere — while being the
    one identity header with no bound on its size, carrying every AD group a user is in under
    `entra_group_claims_as_roles` to every connector, including servers this family does not host.

    Re-adding it is a decision, not a line: it needs a connector that reads it and an argument for
    why an entitlement set is the thing that reader needs, given that a connector may never decide
    on one.
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
