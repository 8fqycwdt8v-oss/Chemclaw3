"""Each shipped connector really serves its manifest's tools over HTTP — and only those.

This replaces `test_mcp_transport.py`, which spawned each stdio MCP server and asserted it
advertised exactly its `allowed_tools`. The property is the one worth keeping: it is the check
that the agent-facing surface is what the manifest says, so the write/index tools stay off the
conversation (D-029) and a renamed tool cannot pass as present. Only the transport changed, so
the test follows it — a real uvicorn server on an ephemeral port, connected by the same
client the agent uses.

It also verifies the three things the HTTP transport adds and stdio did not have: the `/healthz`
route the startup probe depends on, that the turn's identity headers actually arrive at the
connector (the contract is only real if the bytes land), and that they arrive *there and nowhere
else* — a connector that answers with a redirect must not be able to walk the caller's Entra
identity to another origin (Sec-2). And it verifies that a tool call has a *deadline* at all: an
out-of-process capability that stops answering must cost its own call, never the turn.

Tool *discovery* needs no database, so this runs in the sandbox; invoking a tool needs Postgres
and is covered in CI (`test_molfp_postgres.py`, `test_rxnfp_postgres.py`) against the same code.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from langchain_core.tools import BaseTool
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError

from chemclaw.agent.audit import _served_by
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_SESSION,
)
from chemclaw.connectors.manifest import ConnectorManifest, HttpEndpoint, StdioEndpoint
from chemclaw.connectors.registry import (
    _mcp_connection,
    connector_http_client,
    discovered,
    open_connector_specs,
    request_timeout_seconds,
    server_tools_module,
)
from chemclaw.connectors.server import connector_app
from chemclaw.connectors.transport import SERVED_BY, ConnectorSpec, _stamped
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.mcp_session import cancel_on_timeout
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from tests.conftest import _free_port

# Every discovered bundle that ships a local HTTP server, as `(name, manifest)`. Parametrizing
# over discovery rather than a hardcoded list means a new bundle is covered on the day it is added.
#
# **`server_tools_module` is the second half of the predicate, and it stopped being redundant.**
# An `HttpEndpoint` used to imply we run the server, so the endpoint type alone was the whole
# filter. It no longer does: a bundle whose capability lives in `Chemclaw3-mcp` still declares an
# endpoint here — that declaration is what four validators resolve tool names through — while
# shipping no `server/` package. Without this half, these tests start a dev composite that does not
# contain the bundle and then assert against it, which is what `chem` did the day it moved: a 404
# from a route nobody serves, reported as a broken health probe.
_LOCAL_HTTP = [
    (name, manifest)
    for name, (_dir, manifest) in sorted(discovered().items())
    if isinstance(manifest.endpoint, HttpEndpoint) and server_tools_module(name) is not None
]


class _Server:
    """A uvicorn server on a background thread, started and stopped around one test."""

    def __init__(self, app: FastAPI, port: int) -> None:
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "_Server":
        """Start the server and wait until it is actually accepting connections."""
        self._thread.start()
        for _ in range(200):  # ~10s worst case; a real start is tens of milliseconds
            if self._server.started:
                return self
            threading.Event().wait(0.05)
        raise RuntimeError("connector test server did not start")

    def __exit__(self, *_exc: object) -> None:
        """Ask uvicorn to exit and wait for the thread, so no server outlives its test."""
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture(scope="module")
def composite() -> Iterator[int]:
    """Serve every local connector once, on one port, and yield it.

    Module-scoped and composite for a reason worth recording: a connector app's lifespan starts
    the MCP session manager, and `FastMCP.session_manager.run()` is single-use — a module-level
    `app` (what every bundle exports) can therefore be served exactly once per process. Serving
    each bundle in its own server per test would fail on the second one. Mounting them together
    is also what `chemclaw.cli.connectors_dev` does for the dev loop, so this exercises that shape
    too.
    """
    from chemclaw.cli.connectors_dev import build_composite, ensure_dev_tokens

    # Every bundle we host now authenticates its own `/mcp`, so the composite needs credentials to
    # exist before it serves — and the tests below need the *same* values to present. Minted the
    # way `make connectors` mints them rather than by setting a literal here, so this exercises the
    # dev path instead of a parallel one.
    ensure_dev_tokens()
    app, _urls = build_composite()
    port = _free_port()
    with _Server(app, port):
        yield port


@pytest.mark.parametrize("name", [name for name, _ in _LOCAL_HTTP])
def test_health_route_answers_for_the_startup_probe(name: str, composite: int) -> None:
    """`connectors.health` probes this route; without it a connector is unprobed, not up."""
    response = httpx.get(f"http://127.0.0.1:{composite}/{name}/healthz", timeout=5)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "connector": name}


@pytest.mark.parametrize("name", [name for name, _ in _LOCAL_HTTP])
def test_the_agent_sees_exactly_the_manifest_allow_list(name: str, composite: int) -> None:
    """The boundary that keeps write/index tools off the conversation, now over HTTP.

    Asserts the *agent's* view equals the manifest's `tools` allow-list. A server may still serve
    more than the agent may call — a job's tool is not on this list either — so this is not a
    minimality check.

    It used to say `molfp` "still serves `index_molecule` for the ingestion path", which was the
    justification for not checking the served set at all. Nothing in the tree ever called it, and
    an anonymous MCP handshake against the real app wrote a row into `molecule_fingerprints`.
    Minimality of the *served* set is now `connector-validate`'s job
    (`_served_tool_problems`): every served tool must be declared in the manifest.
    """
    manifest = dict(_LOCAL_HTTP)[name]
    assert isinstance(manifest.endpoint, HttpEndpoint)
    declared = set(manifest.endpoint.tools)
    assert declared, f"{name} declares no agent-facing tools"

    async def _discover() -> set[str]:
        # The bundle's *own* endpoint with the address swapped, not a fresh one: rebuilding it
        # dropped the manifest's `auth` declaration, so this connected anonymously and proved
        # nothing about the credential the deployment actually requires. It connected at all only
        # because no bundle we host declared one.
        assert isinstance(manifest.endpoint, HttpEndpoint)
        endpoint = manifest.endpoint.model_copy(
            update={"url": f"http://127.0.0.1:{composite}/{name}/mcp"}
        )
        spec = _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name=name)), endpoint)
        spec = replace(spec, allowed_tools=tuple(sorted(declared)))
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            assert not unreachable, f"{name} did not connect over HTTP"
            return {tool.name for tool in tools}
        raise AssertionError("unreachable")  # pragma: no cover

    assert asyncio.run(_discover()) == declared


def test_the_turn_identity_actually_arrives_at_the_connector() -> None:
    """The header contract is only real if the bytes land, so a served app records what it received.

    Uses a purpose-built app rather than a shipped bundle: the assertion is about the transport,
    and a real capability would need its database to answer a tool call. The tool being called
    is what matters — `header_provider` runs per `call_tool`, not at connect — so the app
    exposes a trivial one.
    """
    received: list[dict[str, str]] = []
    server = FastMCP("header-probe")

    @server.tool()
    async def echo() -> str:
        """A trivial tool, so that a *call* happens and the per-call headers are sent."""
        return "ok"

    app = connector_app(server, name="header-probe")

    @app.middleware("http")
    async def _capture(request: Any, call_next: Any) -> Any:
        """Record the Chemclaw headers of every request reaching the connector."""
        received.append(
            {key: value for key, value in request.headers.items() if key.startswith("x-chemclaw-")}
        )
        return await call_next(request)

    port = _free_port()

    async def _call() -> None:
        endpoint = HttpEndpoint(url=f"http://127.0.0.1:{port}/mcp")
        # Built by `_mcp_connection`, which is the one function a deployment builds a connector
        # with — so this proves the identity hook lands on the client the agent actually uses.
        spec = _mcp_connection(
            cast(ConnectorManifest, SimpleNamespace(name="header-probe")), endpoint
        )
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            assert not unreachable
            echo = next(tool for tool in tools if tool.name == "echo")
            await echo.ainvoke({})

    identity = set_current_identity("user-42", frozenset({"process-chemist"}))
    session = set_current_session_id("session-xyz")
    try:
        with _Server(app, port):
            asyncio.run(_call())
    finally:
        reset_current_session_id(session)
        reset_current_identity(identity)

    # At least one request — the tool call — carried the full identity. The headers are stamped
    # by the httpx client `connector_http_client` builds, which is also why the deployment's own
    # credential travels there rather than on a per-call hook.
    assert any(
        headers.get(HEADER_ACTOR.lower()) == "user-42"
        and headers.get(HEADER_SESSION.lower()) == "session-xyz"
        for headers in received
    ), received


def test_a_tool_body_can_read_the_caller_core_stamped() -> None:
    """The headers reach a *tool*, not only a log line — which is what they were sent for.

    `CallerLogMiddleware`'s own docstring said these exist "so a connector's own records can be
    joined to the core audit trail by actor and session", and a connector could only ever put them
    in a log. A connector that writes a durable row — a persisted BO suggestion — had no way to
    stamp it with the conversation that asked, so the row could not be traced back to a chemist or
    a turn. Advisory throughout: the tool reads them to attribute a record, never to decide
    anything.
    """
    from chemclaw.connectors.caller import caller_provenance

    seen: list[tuple[str, str, str]] = []
    server = FastMCP("caller-probe")

    @server.tool()
    async def whoami() -> str:
        """Record what the tool body can see of its caller."""
        seen.append(caller_provenance())
        return "ok"

    app = connector_app(server, name="caller-probe")
    port = _free_port()

    async def _call() -> None:
        endpoint = HttpEndpoint(url=f"http://127.0.0.1:{port}/mcp")
        spec = _mcp_connection(
            cast(ConnectorManifest, SimpleNamespace(name="caller-probe")),
            HttpEndpoint(url=endpoint.url),
        )
        async with AsyncExitStack() as stack:
            tools, _unreachable = await open_connector_specs(stack, [spec])
            await next(t for t in tools if t.name == "whoami").ainvoke({})

    identity = set_current_identity("user-77", frozenset({"process-chemist"}))
    session = set_current_session_id("session-abc")
    try:
        with _Server(app, port):
            asyncio.run(_call())
    finally:
        reset_current_session_id(session)
        reset_current_identity(identity)

    assert seen, "the tool never ran"
    actor, session_id, _correlation = seen[-1]
    assert (actor, session_id) == ("user-77", "session-abc")


def test_a_redirecting_connector_cannot_harvest_the_turn_identity() -> None:
    """The identity headers reach the configured connector and no other origin (Sec-2).

    Two real servers: the connector's own address answers `307` pointing at a second one, which
    records everything it is sent. The client is the production one
    (`registry.connector_http_client`), so what is proven is the deployment's behaviour rather than
    a flag's value.

    The leak this closes was not hypothetical arithmetic. An httpx request hook runs on *every* hop
    (`_send_handling_redirects`) and httpx builds the redirected request from the previous request's
    headers, dropping `Authorization` alone and only cross-origin — so with `follow_redirects=True`
    the second server received the caller's Entra object id and full role set once per turn, and
    every shipped manifest declares `auth: mode: none`, which makes "answer on the connector's port"
    the whole of the attack. Both halves are asserted: the real connector still gets the identity
    (a test that only checked the attacker would pass with the hook deleted), and the other origin
    gets no request at all.
    """
    from fastapi import Request as FastAPIRequest
    from fastapi.responses import RedirectResponse

    harvested: list[dict[str, str]] = []
    delivered: list[dict[str, str]] = []
    harvester_port, connector_port = _free_port(), _free_port()

    def _chemclaw_headers(request: FastAPIRequest) -> dict[str, str]:
        """The `X-Chemclaw-*` headers of one request, as the recording servers see them."""
        return {
            key: value for key, value in request.headers.items() if key.startswith("x-chemclaw-")
        }

    harvester = FastAPI()

    @harvester.post("/mcp")
    async def _harvest(request: FastAPIRequest) -> dict[str, str]:
        """Stand in for whatever the redirect points at, and record what it was handed."""
        harvested.append(_chemclaw_headers(request))
        return {"status": "ok"}

    connector = FastAPI()

    @connector.post("/mcp")
    async def _redirect(request: FastAPIRequest) -> RedirectResponse:
        """A connector that answers the MCP POST with a redirect to somewhere else entirely."""
        delivered.append(_chemclaw_headers(request))
        return RedirectResponse(f"http://127.0.0.1:{harvester_port}/mcp", status_code=307)

    endpoint = HttpEndpoint(url=f"http://127.0.0.1:{connector_port}/mcp")

    async def _post() -> int:
        async with connector_http_client("redirect-probe", endpoint) as client:
            response = await client.post(endpoint.url, json={"jsonrpc": "2.0", "method": "ping"})
            return response.status_code

    identity = set_current_identity("user-99", frozenset({"process-chemist"}))
    session = set_current_session_id("session-leak")
    try:
        with _Server(connector, connector_port), _Server(harvester, harvester_port):
            status = asyncio.run(_post())
    finally:
        reset_current_session_id(session)
        reset_current_identity(identity)

    assert delivered and delivered[0][HEADER_ACTOR.lower()] == "user-99"
    assert delivered[0][HEADER_SESSION.lower()] == "session-leak"
    # The redirect is surfaced to the caller and never walked, so nothing was ever sent onward.
    assert status == 307
    assert harvested == [], harvested


def test_oversized_body_is_rejected_before_the_mcp_handler_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector's `/mcp` refuses an oversized body with 413 before anything reads it (Sec-5).

    `connector_app` used to install only `CallerLogMiddleware` — no cap at all — so an unbounded
    body reached the MCP transport (and would reach it even with bearer auth on, since the body is
    consumed before auth is evaluated). This proves the shared `chemclaw.core.asgi.BodySizeLimit`
    now runs in front of the connector, over its own `connector_max_request_bytes` setting, exactly
    as it runs in front of the front door over `service_max_request_bytes`.
    """
    from chemclaw.core.config import settings

    # Small enough that a real MCP handshake body would trip it too — the point is that the limit
    # is enforced by the middleware, not by whatever the handler underneath would have done with a
    # body this size.
    monkeypatch.setattr(settings, "connector_max_request_bytes", 10)
    server = FastMCP("body-limit-probe")
    app = connector_app(server, name="body-limit-probe")
    port = _free_port()

    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    with _Server(app, port):
        response = httpx.post(f"http://127.0.0.1:{port}/mcp", content=b"x" * 1000, headers=headers)
    assert response.status_code == 413


def test_an_unexpected_tool_exception_reaches_the_caller_sanitized() -> None:
    """An unhandled exception's text must not carry a DSN/path/internal identifier to the caller.

    Measured against the real `mcp` package (installed here, unlike when this was first flagged):
    `Tool.run` folds an exception's `str()` verbatim into the tool-error text it returns, so a raw
    `RuntimeError` naming a database DSN reached the caller unredacted before `connector_app`
    patched the tool manager's `call_tool`. This pins that patch — a future `mcp` upgrade that
    changes how it composes the error text (or removes the interception point this relies on)
    should fail this test loudly rather than silently reopen the leak.
    """
    secret = "postgresql://chemclaw:s3cr3t-pw@10.0.0.7:5432/chemclaw_prod?sslmode=require"
    server = FastMCP("leak-probe")

    @server.tool()
    async def blow_up() -> str:
        """Raise an exception whose text contains a recognizable secret-shaped string."""
        raise RuntimeError(f"could not connect to database: {secret}")

    app = connector_app(server, name="leak-probe")
    port = _free_port()

    async def _call() -> str:
        spec = _mcp_connection(
            cast(ConnectorManifest, SimpleNamespace(name="leak-probe")),
            HttpEndpoint(url=f"http://127.0.0.1:{port}/mcp"),
        )
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            assert not unreachable
            # Returned rather than raised: `langchain-mcp-adapters` renders an MCP error result
            # as the tool's content, which is the shape a model is meant to read. What the test is
            # about is unchanged — what the *caller* is told.
            return str(await next(t for t in tools if t.name == "blow_up").ainvoke({}))
        raise AssertionError("unreachable")  # pragma: no cover

    with _Server(app, port):
        message = asyncio.run(_call())
    assert secret not in message
    assert "an internal error occurred" in message


def test_the_connector_server_entrypoint_configures_the_process_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector server process must get the setup every other process role already has.

    `deploy/entrypoint.sh` used to exec `uvicorn <bundle>.server.app:app` straight at the app
    object, so no module owned this role's startup and nothing called `configure_logging()` or
    `configure_telemetry()`. The one process family that holds per-connector bearer tokens ran
    with no secret redaction, no correlation id and no actor on any line — and with no meter
    provider, which is the configuration `_install_noop_meter_provider` records as leaking.

    Order matters as much as the call: the app is handed to uvicorn as an import *string*, so it
    is built after logging is configured. Importing it here would put a bundle's import-time
    logging on an unconfigured, unredacted root logger.
    """
    from chemclaw.connectors import server_entry

    calls: list[str] = []
    monkeypatch.setattr(server_entry, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(server_entry, "configure_telemetry", lambda: calls.append("telemetry"))
    monkeypatch.setattr(
        "chemclaw.connectors.server_entry.uvicorn.run",
        lambda target, **_kw: calls.append(f"serve:{target}"),
    )
    server_entry.main("safety")
    assert calls == ["logging", "telemetry", "serve:chemclaw.connectors.safety.server.app:app"]


def test_building_a_connector_app_does_not_reconfigure_process_logging() -> None:
    """The other half: `connector_app` must *not* do process setup, and this pins why.

    Putting `configure_logging()` here was tried first and is wrong. It is
    `logging.basicConfig(force=True)`, which removes every existing root handler — and
    `connector_app` runs at import time in seven bundle modules that tests, the dev composite and
    anything else import freely. It tore out pytest's own capture handler and broke two
    audit-trail tests that have nothing to do with logging.
    """
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        connector_app(FastMCP("no-stomp-probe"), name="no-stomp-probe")
        assert sentinel in root.handlers, "connector_app replaced the host's logging handlers"
    finally:
        root.removeHandler(sentinel)


def test_a_deliberate_domain_error_still_reaches_the_caller_unchanged() -> None:
    """The other half of the same fix: a `ValueError`-family message must not be swallowed too.

    Every connector tool that refuses a bad SMILES or a bad argument raises a `ValueError` (or
    `ChemclawError`/`ConnectorError`, both subclasses) precisely so the model reads a sentence it
    can act on. Sanitizing indiscriminately would silently break every one of those.
    """
    from chemclaw.core.errors import ChemclawError

    server = FastMCP("domain-error-probe")

    @server.tool()
    async def bad_smiles() -> str:
        """Raise the deliberately-worded domain error a real connector tool would raise."""
        raise ChemclawError("could not parse SMILES 'not-a-molecule'")

    app = connector_app(server, name="domain-error-probe")
    port = _free_port()

    async def _call() -> str:
        spec = _mcp_connection(
            cast(ConnectorManifest, SimpleNamespace(name="domain-error-probe")),
            HttpEndpoint(url=f"http://127.0.0.1:{port}/mcp"),
        )
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            assert not unreachable
            # Returned rather than raised: `langchain-mcp-adapters` renders an MCP error result
            # as the tool's content, which is the shape a model is meant to read. What the test is
            # about is unchanged — what the *caller* is told.
            return str(await next(t for t in tools if t.name == "bad_smiles").ainvoke({}))
        raise AssertionError("unreachable")  # pragma: no cover

    with _Server(app, port):
        message = asyncio.run(_call())
    assert "could not parse SMILES 'not-a-molecule'" in message


def test_a_bundles_startup_report_cannot_delay_it_becoming_ready() -> None:
    """The `on_start` hook is started, never awaited — readiness must not depend on it.

    `molfp`/`rxnfp` use the hook to log how many fingerprints their index holds, which is a
    database round trip. Awaiting it made the connector's startup wait out the pool timeout when
    Postgres was unreachable, which is the wrong trade twice over: the pod is slower to become
    ready exactly when the operator most needs it up to read the log line. Proven with a hook that
    never returns at all — if the lifespan awaited it, this test would time out instead of pass.
    """
    running = asyncio.Event()

    async def _never_finishes() -> None:
        running.set()
        await asyncio.sleep(3600)

    app = connector_app(FastMCP("hook-probe"), name="hook-probe", on_start=_never_finishes)

    async def _serve_and_stop() -> None:
        async with app.router.lifespan_context(app):
            # The hook really was launched (not silently skipped), yet startup already completed.
            await asyncio.wait_for(running.wait(), timeout=5)

    asyncio.run(asyncio.wait_for(_serve_and_stop(), timeout=10))


def _session_read_bound(spec: ConnectorSpec) -> float:
    """The deadline the MCP session will actually enforce, read off the built connection.

    Read from the connection mapping rather than recomputed, because the property under test is
    that the number a deployment *ships* is finite — a test that derived its own would pass with
    `session_kwargs` deleted.
    """
    kwargs = spec.connection.get("session_kwargs") or {}
    bound = kwargs["read_timeout_seconds"]
    assert isinstance(bound, timedelta), bound
    return bound.total_seconds()


def test_a_slow_tool_call_is_abandoned_at_the_declared_request_timeout() -> None:
    """A tool that will not answer must fail the call, not hold the turn until the deadline.

    The defect this pins was unbounded in the literal sense. Nothing set `session_kwargs`, so the
    MCP `ClientSession` got `read_timeout_seconds=None` and `mcp.shared.session.send_request`
    reached `anyio.fail_after(None)` — a wait with no end. The httpx read timeout that *did* fire
    was swallowed by `mcp.client.streamable_http` (caught as `Exception` at debug level, with a
    reconnect that needs an SSE event id FastMCP never sends), so the answer was discarded and the
    caller went on waiting: measured, a 4 s tool behind `request_timeout: 2` was still blocked at
    25 s. Only the front door's 600 s turn deadline ended it, with an admission permit and the
    session's connectors held the whole time.

    Wrapped in `asyncio.wait_for` so the *unfixed* code fails this test in 15 s instead of hanging
    the suite, and the elapsed time is asserted rather than merely "it raised" — raising at 25 s
    would be the bug with a nicer ending.
    """
    release = threading.Event()
    server = FastMCP("slow-probe")

    @server.tool()
    async def crawl() -> str:
        """Answer only when the test lets go — far past any bound under test.

        Blocks on a `threading.Event` in a worker thread rather than `asyncio.sleep`: the server
        runs its own loop on its own thread, and this is the one way to release it from the test's
        thread without a cross-loop race. The 30 s ceiling is the backstop if the release is missed.
        """
        await asyncio.to_thread(release.wait, 30)
        return "too late to matter"

    app = connector_app(server, name="slow-probe")
    port = _free_port()

    async def _call() -> float:
        spec = _mcp_connection(
            cast(ConnectorManifest, SimpleNamespace(name="slow-probe")),
            HttpEndpoint(url=f"http://127.0.0.1:{port}/mcp", request_timeout=2),
        )
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            assert not unreachable
            slow = next(tool for tool in tools if tool.name == "crawl")
            started = time.monotonic()
            # An `McpError`, not a tool-error *result*: a transport/session failure is not something
            # the connector said, so `langchain-mcp-adapters` raises it rather than rendering it as
            # content for the model.
            with pytest.raises(McpError):
                await slow.ainvoke({})
            return time.monotonic() - started
        raise AssertionError("unreachable")  # pragma: no cover

    with _Server(app, port):
        try:
            elapsed = asyncio.run(asyncio.wait_for(_call(), timeout=15))
        finally:
            release.set()  # let the server's in-flight request finish so uvicorn can exit
    assert elapsed < 10, f"the call was abandoned only after {elapsed:.1f}s, not near its 2s bound"


def test_a_timed_out_call_tells_the_connector_to_stop_working() -> None:
    """Abandoning a call must stop the *server*, not only this side's wait.

    The sibling of the test above, and the half that was missing. `request_timeout` bounded the
    caller and nothing else: `mcp.shared.session.send_request` raises on expiry and sends the
    server nothing, and the session stays open for the rest of the turn — so the tool ran to
    completion and its answer was discarded. Measured before the fix, against a running server: a
    20 s tool behind a 4 s bound printed "RAN TO COMPLETION with nobody waiting".

    Affordable for a dictionary lookup, not for what the fleet actually hosts. `Chemclaw3-mcp`'s
    `calc` server is documented as "minutes or hours, deliberately", and `cached_compute` retries a
    miss — so an abandoned CREST search held a pod's CPU while the next attempt started a second
    identical one beside it.

    Asserts the *tool body* observed cancellation rather than that the client raised, because the
    client raised before the fix too. The flag is written on the server's own loop and read here
    only after that loop has had its chance, which is why the wait below is a poll rather than an
    assertion on the spot.
    """
    cancelled = threading.Event()
    server = FastMCP("cancel-probe")

    @server.tool()
    async def crawl() -> str:
        """Sleep past any bound under test, and record if the request is cancelled under us."""
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "too late to matter"

    app = connector_app(server, name="cancel-probe")
    port = _free_port()

    async def _call() -> None:
        spec = _mcp_connection(
            cast(ConnectorManifest, SimpleNamespace(name="cancel-probe")),
            HttpEndpoint(url=f"http://127.0.0.1:{port}/mcp", request_timeout=2),
        )
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            assert not unreachable
            slow = next(tool for tool in tools if tool.name == "crawl")
            with pytest.raises(McpError):
                await slow.ainvoke({})
            # Held open deliberately: this is the turn's own shape, and it is what made the
            # abandoned work outlive the caller. The cancellation has to arrive *here*, while the
            # session is still up, rather than as a side effect of tearing it down.
            assert cancelled.wait(10), (
                "the connector was never told to stop; it is still computing an answer nobody "
                "is waiting for"
            )

    with _Server(app, port):
        asyncio.run(asyncio.wait_for(_call(), timeout=20))


def test_a_session_that_cannot_be_wrapped_is_left_alone_rather_than_refused() -> None:
    """Installing the cancellation must never be able to fail a working session.

    `cancel_on_timeout` reads two upstream privates, and `open_session` calls it *before* it marks
    the connection established — so anything raised there is classified as `McpConnectFailed`, "the
    calculation service is not answering". An SDK rename would therefore have turned a lost
    *cancellation* into a total *outage*: every calc job failing, for a courtesy.

    The right failure mode for an enhancement to an otherwise working session is to degrade to the
    behaviour it improves on. This pins that, against a session exposing neither attribute — which
    is both the upstream-rename case and the shape of the minimal fake in
    `tests/test_calc_remote.py` that found it.
    """

    class _Bare:
        """A session object with none of what the wrapper wants."""

    bare = _Bare()
    cancel_on_timeout(cast(Any, bare))  # must not raise
    assert not hasattr(bare, "send_request"), "an unwrappable session was wrapped anyway"


def test_the_http_read_bound_is_looser_than_the_session_bound() -> None:
    """The bound that raises must fire before the bound that is swallowed — this pins that order.

    Both come from `request_timeout_seconds`, so they cannot drift apart; what they must not do is
    become *equal*, because then the invisible one can win. `mcp.client.streamable_http` discards an
    httpx read timeout at debug level, so if it tripped first a merely slow tool would become a lost
    answer with no error anywhere — which is exactly the failure measured before the fix. The grace
    is what keeps the httpx timeout a backstop for a stream that stops producing bytes at all.
    """
    endpoint = HttpEndpoint(url="http://127.0.0.1:8899/mcp", request_timeout=2)
    spec = _mcp_connection(
        cast(ConnectorManifest, SimpleNamespace(name="ordering-probe")), endpoint
    )
    session_bound = _session_read_bound(spec)
    assert session_bound == request_timeout_seconds(endpoint) == 2.0

    async def _read_bound() -> float | None:
        async with connector_http_client("ordering-probe", endpoint) as client:
            assert isinstance(client.timeout.read, float)
            return client.timeout.read

    read_bound = asyncio.run(_read_bound())
    assert read_bound is not None and read_bound > session_bound


def test_an_endpoint_declaring_no_timeout_is_still_bounded() -> None:
    """`request_timeout` is optional, and its absence must not mean "wait forever".

    The case a third-party bundle ships: `HttpEndpoint.request_timeout` defaults to `None`, and
    `StdioEndpoint` has no such field at all. Both used to reach `anyio.fail_after(None)`. Both
    branches of `_mcp_connection` are checked, because the one that was forgotten is the one that
    hangs a turn.
    """
    http = HttpEndpoint(url="http://127.0.0.1:8899/mcp")
    assert http.request_timeout is None, "this test is only meaningful for an undeclared timeout"
    stdio = StdioEndpoint(command="python", args=["-c", "pass"])

    for endpoint in (http, stdio):
        spec = _mcp_connection(
            cast(ConnectorManifest, SimpleNamespace(name="default-probe")), endpoint
        )
        bound = _session_read_bound(spec)
        assert bound == request_timeout_seconds(endpoint)
        assert 0 < bound < 600, f"{type(endpoint).__name__} bound {bound}s is not a usable deadline"


def test_a_tool_carries_the_build_of_the_server_that_answers_it() -> None:
    """The handshake's `serverInfo.version` reaches the tool, which is what the trail records.

    **The provenance the capability migration broke.** `audit_events.revision` names this process's
    commit, and while the chemistry ran here that reproduced a result. It no longer does: a
    `Chemclaw3-mcp` server computes the number and releases on its own cadence, so the build that
    actually produced it was recorded nowhere. `initialize()` already answers the question — every
    session reads `serverInfo` — so nothing new is opened, sent or awaited to close it.

    Driven through `open_connector_specs` against a real served app rather than a stubbed session,
    because the stamp is applied inside `HeldConnectorSession._hold` and every claim here is about
    what survives the real path: the allow-list filter, the holder task, and the adapter's own
    metadata. A double could only show the double agrees with itself.

    The version is set the way the fleet sets it (`mcp_server_kit.app._stamp_revision` assigns
    `FastMCP._mcp_server.version`), which is why this doubles as the cross-repository contract test:
    the private attribute those five servers reach through is the one this reads back out.
    """
    server = FastMCP("revision-probe")
    server._mcp_server.version = "sha-9f3c1d"

    @server.tool()
    async def echo() -> str:
        """A trivial tool, so the session has something to advertise."""
        return "ok"

    port = _free_port()

    async def _discover() -> list[BaseTool]:
        endpoint = HttpEndpoint(url=f"http://127.0.0.1:{port}/mcp")
        spec = _mcp_connection(
            cast(ConnectorManifest, SimpleNamespace(name="revision-probe")), endpoint
        )
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            assert not unreachable, "the probe server did not connect"
            return tools
        raise AssertionError("unreachable")  # pragma: no cover

    with _Server(connector_app(server, name="revision-probe"), port):
        tools = asyncio.run(_discover())

    assert [tool.name for tool in tools] == ["echo"]
    assert (tools[0].metadata or {})[SERVED_BY] == {
        "connector": "revision-probe",
        "revision": "sha-9f3c1d",
    }
    assert _served_by(SimpleNamespace(tool=tools[0])) == "revision-probe@sha-9f3c1d"


def test_a_server_that_cannot_name_its_build_says_so_rather_than_reporting_the_sdk() -> None:
    """An unstamped server must be distinguishable from an in-process tool, not blend into it.

    Two failures are being kept apart, and neither is the other's severity. An in-process tool has
    no server revision because there is no server — `revision` already covers its build, and an
    empty stamp is a complete answer. An image built without `--build-arg CHEMCLAW_REVISION` is a
    deployment mistake someone can fix, and it must read as one.

    What makes this non-obvious is the value in between: left alone, `FastMCP` reports the **MCP
    SDK's** release, so the column would fill with a real-looking version string that names the
    client library rather than the build. The fleet's `server_revision` defaults to `"unknown"`
    precisely to avoid that, and this asserts the reading end agrees — a tool stamped `"unknown"`
    is recorded as `<connector>@unknown`, which is neither empty nor a plausible-looking lie.
    """
    stamped = _stamped([_probe_tool()], connector="calc", revision="unknown")
    assert _served_by(SimpleNamespace(tool=stamped[0])) == "calc@unknown"

    # The in-process case, which is what every LangGraph request outside a connector looks like:
    # `ToolNode` also passes `tool=None` for a name the graph does not hold, and both must be the
    # same empty string rather than a fabricated `@unknown`.
    assert _served_by(SimpleNamespace(tool=_probe_tool())) == ""
    assert _served_by(SimpleNamespace(tool=None)) == ""


def _probe_tool() -> BaseTool:
    """An unstamped `BaseTool`, standing in for an in-process capability."""
    from langchain_core.tools import tool as make_tool

    @make_tool
    def probe() -> str:
        """A trivial in-process tool."""
        return "ok"

    return probe
