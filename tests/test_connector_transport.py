"""Each shipped connector really serves its manifest's tools over HTTP — and only those.

This replaces `test_mcp_transport.py`, which spawned each stdio MCP server and asserted it
advertised exactly its `allowed_tools`. The property is the one worth keeping: it is the check
that the agent-facing surface is what the manifest says, so the write/index tools stay off the
conversation (D-029) and a renamed tool cannot pass as present. Only the transport changed, so
the test follows it — a real uvicorn server on an ephemeral port, connected by the same MAF
client the agent uses.

It also verifies the two things the HTTP transport adds and stdio did not have: the `/healthz`
route the startup probe depends on, and that the turn's identity headers actually arrive at the
connector (the contract is only real if the bytes land).

Tool *discovery* needs no database, so this runs in the sandbox; invoking a tool needs Postgres
and is covered in CI (`test_molfp_postgres.py`, `test_rxnfp_postgres.py`) against the same code.
"""

import asyncio
import socket
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from chemclaw.agent.identity_context import reset_current_identity, set_current_identity
from chemclaw.agent.session_context import reset_current_session_id, set_current_session_id
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_ROLES,
    HEADER_SESSION,
    stamp_turn_identity,
)
from chemclaw.connectors.manifest import HttpEndpoint
from chemclaw.connectors.registry import discovered
from chemclaw.connectors.server import connector_app
from chemclaw.connectors.transport import DegradingHttpConnector

# Every discovered bundle that ships a local HTTP server, as `(name, manifest)`. Parametrizing
# over discovery rather than a hardcoded list means a new bundle is covered on the day it is
# added.
_LOCAL_HTTP = [
    (name, manifest)
    for name, (_dir, manifest) in sorted(discovered().items())
    if isinstance(manifest.endpoint, HttpEndpoint)
]


def _free_port() -> int:
    """An unused localhost port, so concurrent test runs cannot collide on a fixed one."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    from chemclaw.cli.connectors_dev import build_composite

    app, _urls = build_composite()
    port = _free_port()
    with _Server(app, port):
        yield port


@pytest.mark.parametrize("name", [name for name, _ in _LOCAL_HTTP])
def test_health_route_answers_for_the_startup_probe(name: str, composite: int) -> None:
    """`connectors.health` probes this route; without it a connector is unprobed, not up."""
    import httpx

    response = httpx.get(f"http://127.0.0.1:{composite}/{name}/healthz", timeout=5)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "connector": name}


@pytest.mark.parametrize("name", [name for name, _ in _LOCAL_HTTP])
def test_the_agent_sees_exactly_the_manifest_allow_list(name: str, composite: int) -> None:
    """The boundary that keeps write/index tools off the conversation, now over HTTP.

    A server may legitimately expose more than the agent may call — `molfp` still serves
    `index_molecule` for the ingestion path — so this asserts the *agent's* view equals the
    manifest, not that the server is minimal.
    """
    manifest = dict(_LOCAL_HTTP)[name]
    assert isinstance(manifest.endpoint, HttpEndpoint)
    declared = set(manifest.endpoint.tools)
    assert declared, f"{name} declares no agent-facing tools"

    async def _discover() -> set[str]:
        tool = DegradingHttpConnector(
            name=name,
            url=f"http://127.0.0.1:{composite}/{name}/mcp",
            allowed_tools=sorted(declared),
            load_prompts=False,
        )
        async with tool:
            assert tool.is_connected, f"{name} did not connect over HTTP"
            return {function.name for function in tool.functions}

    assert asyncio.run(_discover()) == declared


def test_the_turn_identity_actually_arrives_at_the_connector() -> None:
    """The header contract is only real if the bytes land, so a served app records what it received.

    Uses a purpose-built app rather than a shipped bundle: the assertion is about the transport,
    and a real capability would need its database to answer a tool call. The tool being called
    is what matters — `header_provider` runs per `call_tool`, not at connect — so the app
    exposes a trivial one.
    """
    from mcp.server.fastmcp import FastMCP

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
        tool = DegradingHttpConnector(
            name="header-probe",
            url=f"http://127.0.0.1:{port}/mcp",
            load_prompts=False,
            # The identity hook, exactly as `connectors.registry` installs it on a connector's
            # client.
            http_client=httpx.AsyncClient(
                follow_redirects=True, event_hooks={"request": [stamp_turn_identity]}
            ),
        )
        async with tool:
            assert tool.is_connected
            await tool.call_tool("echo")

    identity = set_current_identity("user-42", frozenset({"process-chemist"}))
    session = set_current_session_id("session-xyz")
    try:
        with _Server(app, port):
            asyncio.run(_call())
    finally:
        reset_current_session_id(session)
        reset_current_identity(identity)

    # At least one request — the tool call — carried the full identity. The handshake
    # deliberately does not (MAF only invokes `header_provider` for `call_tool`), which is
    # exactly why our own credential travels on the httpx client instead.
    assert any(
        headers.get(HEADER_ACTOR.lower()) == "user-42"
        and headers.get(HEADER_ROLES.lower()) == "process-chemist"
        and headers.get(HEADER_SESSION.lower()) == "session-xyz"
        for headers in received
    ), received


def test_an_unreachable_connector_costs_its_tools_not_the_turn() -> None:
    """The degrade posture, at the layer it has to live in (`chemclaw.connectors.transport`).

    Nothing is listening on this port. The connector must come back *not connected* and
    contribute no tools, rather than raising — because `Agent.run` re-enters an unconnected MCP
    tool, so a failure that escapes here would surface mid-turn no matter what the caller did.
    """
    port = _free_port()

    async def _attempt() -> tuple[bool, int]:
        tool = DegradingHttpConnector(
            name="absent", url=f"http://127.0.0.1:{port}/mcp", load_prompts=False
        )
        async with tool:
            return tool.is_connected, len(tool.functions)

    connected, tool_count = asyncio.run(_attempt())
    assert connected is False
    assert tool_count == 0


def test_concurrent_turns_get_their_own_connections_and_their_own_identity() -> None:
    """Why connectors are built per turn: sharing one tool object across turns is doubly wrong.

    Two turns run at once with different actors, each with its own connector instance — the
    shape `chemclaw.agent.chemclaw_agent.connector_tools` produces. Both must complete, and every
    request must carry the identity of the turn that made it.

    Measured, not assumed: with a *shared* tool object instead, the same two turns deadlock
    (each turn's `async with` entering and leaving one connection's lifecycle), and any request
    that did get through would travel over a connection opened in the other turn's context —
    misattributing it in the connector's own log. That is the whole reason connectors are not
    attached to the process-lived agent, and this test is what would fail if they were put back.
    """
    from mcp.server.fastmcp import FastMCP

    seen: list[str] = []
    server = FastMCP("concurrency-probe")

    @server.tool()
    async def slow() -> str:
        """Slow enough that the two turns genuinely overlap rather than serialize by luck."""
        await asyncio.sleep(0.3)
        return "ok"

    app = connector_app(server, name="concurrency-probe")

    @app.middleware("http")
    async def _capture(request: Any, call_next: Any) -> Any:
        """Record the actor of every request that carries one."""
        actor = request.headers.get(HEADER_ACTOR.lower())
        if actor:
            seen.append(actor)
        return await call_next(request)

    port = _free_port()

    def _tool() -> DegradingHttpConnector:
        return DegradingHttpConnector(
            name="concurrency-probe",
            url=f"http://127.0.0.1:{port}/mcp",
            load_prompts=False,
            http_client=httpx.AsyncClient(
                follow_redirects=True, event_hooks={"request": [stamp_turn_identity]}
            ),
        )

    async def _turn(actor: str) -> None:
        """One turn: stamp its identity, open its own connector, call a tool, tear down."""
        token = set_current_identity(actor, frozenset())
        try:
            tool = _tool()
            async with tool:
                assert tool.is_connected
                await tool.call_tool("slow")
        finally:
            reset_current_identity(token)

    async def _both() -> None:
        await asyncio.gather(
            asyncio.create_task(_turn("user-A")), asyncio.create_task(_turn("user-B"))
        )

    with _Server(app, port):
        asyncio.run(_both())

    # Both turns got through (a shared connection deadlocks here), and neither borrowed the
    # other's identity: every request is attributable to the turn that made it.
    assert "user-A" in seen and "user-B" in seen
    assert set(seen) == {"user-A", "user-B"}
