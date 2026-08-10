"""Connectors on the LangGraph engine: real sessions, real degradation, real task affinity (M7).

Every test here drives a live `uvicorn` server over the same streamable-HTTP transport a
deployment uses, for the reason `tests/test_connector_transport.py` gives for the MAF half: the
things that broke in this seam — a deadlock between concurrent turns, an identity header that
never landed, an `anyio` scope exited from the wrong task — are all properties of a real
connection, and none of them is visible against a mock.

**The load-bearing one is `test_connectors_opened_together_close_cleanly`.** The natural way to
open several connectors at once is `asyncio.gather` over `AsyncExitStack.enter_async_context`,
which is exactly what `open_reachable` does for MAF, and on this engine it raises
`RuntimeError: Attempted to exit cancel scope in a different task than it was entered in` — the
MCP session is an `anyio` cancel scope and anyio pins a scope to the task that entered it. MAF
never met this because it holds each connector's lifecycle on its own task internally. That test
is what `HeldConnectorSession` exists for, and it fails without it.
"""

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack
from typing import Any, cast

import pytest
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.chemclaw_agent import connector_specs, connector_tools
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from chemclaw.connectors.identity import HEADER_ACTOR
from chemclaw.connectors.manifest import ConnectorManifest, HttpEndpoint
from chemclaw.connectors.registry import _mcp_connection, open_connector_specs
from chemclaw.connectors.server import connector_app
from chemclaw.connectors.transport import ConnectorSpec
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from tests.conftest import _free_port
from tests.fakes_langgraph import ScriptedChatModel, tool_outputs


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


def _probe_app(name: str, capture: list[str] | None = None) -> FastAPI:
    """A connector exposing two tools, optionally recording the actor of every request."""
    server = FastMCP(name)

    @server.tool()
    async def echo(text: str) -> str:
        """Return what it was given, so a call's arguments are observable."""
        return f"echoed:{text}"

    @server.tool()
    async def slow() -> str:
        """Slow enough that two turns genuinely overlap rather than serialize by luck."""
        await asyncio.sleep(0.3)
        return "ok"

    app = connector_app(server, name=name)
    if capture is not None:

        @app.middleware("http")
        async def _capture(request: Any, call_next: Any) -> Any:
            """Record the actor of every request that carries one."""
            actor = request.headers.get(HEADER_ACTOR.lower())
            if actor:
                capture.append(actor)
            return await call_next(request)

    return app


class _ManifestStub:
    """The one attribute `_mcp_connection` reads off a manifest."""

    def __init__(self, name: str) -> None:
        self.name = name


def _spec(name: str, port: int, allowed: tuple[str, ...] = ()) -> ConnectorSpec:
    """A spec pointing at the test server, built by the registry's own builder.

    Through `_mcp_connection` rather than by constructing a `ConnectorSpec` here, so these tests
    exercise the client factory, the identity hook and the redirect refusal that a real connector
    gets — a hand-built spec would quietly test a connection nothing in production uses.
    """
    # An allow-listed tool must also be classified: the manifest refuses an endpoint that leaves a
    # served tool's state-changing posture unstated, and equally one that classifies a tool it does
    # not serve. Both probe tools are reads, so the allow-list and `read_only` are the same list.
    endpoint = HttpEndpoint(
        url=f"http://127.0.0.1:{port}/mcp",
        tools=list(allowed),
        read_only=list(allowed),
    )
    return _mcp_connection(cast(ConnectorManifest, _ManifestStub(name)), endpoint)


@pytest.fixture
def probe() -> Iterator[int]:
    """One connector server on an ephemeral port, torn down with the test."""
    port = _free_port()
    with _Server(_probe_app("lg-probe"), port):
        yield port


def test_a_reachable_connectors_tools_reach_the_graph_and_run(probe: int) -> None:
    """The whole point: a live connector's tools are callable by the model, over a real session."""

    async def _turn() -> tuple[list[str], list[str]]:
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [_spec("lg-probe", probe)])
            assert not unreachable
            agent = build_langgraph_agent(
                ScriptedChatModel([{"name": "echo", "args": {"text": "hi"}}, "done"]),
                connectors=tools,
                audit_sink=NullAuditSink(),
            )
            result = await agent.ainvoke({"messages": [("user", "call echo")]})
            return [t.name for t in tools], tool_outputs(result["messages"])
        raise AssertionError("the exit stack cannot fall through")

    names, outputs = asyncio.run(_turn())
    assert {"echo", "slow"} <= set(names)
    assert any("echoed:hi" in output for output in outputs)


def test_an_unreachable_connector_costs_its_tools_and_not_the_turn() -> None:
    """Degradation, on the engine that has no `connect()` to override.

    Nothing is listening on this port, so the session never opens. The contract is the MAF one:
    the connector contributes no tools, its name is reported so a surface can say the answer is
    partial, and the turn still runs.
    """

    async def _turn() -> tuple[list[str], list[str], str]:
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [_spec("dark", _free_port())])
            agent = build_langgraph_agent(
                ScriptedChatModel(["answered without it"]),
                connectors=tools,
                audit_sink=NullAuditSink(),
            )
            result = await agent.ainvoke({"messages": [("user", "hello")]})
            return [t.name for t in tools], unreachable, str(result["messages"][-1].content)
        raise AssertionError("the exit stack cannot fall through")

    tools, unreachable, answer = asyncio.run(_turn())
    assert tools == []
    assert unreachable == ["dark"]
    assert answer == "answered without it"


def test_connectors_opened_together_close_cleanly(probe: int) -> None:
    """Several connectors open concurrently and tear down without a cross-task scope error.

    **This is the regression test for the reason `HeldConnectorSession` exists.** Opening these
    sessions the way `open_reachable` opens MAF's — `asyncio.gather` over
    `AsyncExitStack.enter_async_context` — enters each `anyio` cancel scope on a child task and
    exits it on the caller's, which anyio refuses: `RuntimeError: Attempted to exit cancel scope
    in a different task than it was entered in`. Confining each session to a task of its own is
    what makes concurrent opening legal, and concurrency is not optional here — a dark fleet
    otherwise costs the *sum* of its connect timeouts before the model is called.

    Three sessions, because one would pass even if they were opened serially.
    """

    async def _open_three() -> int:
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(
                stack, [_spec(f"lg-probe-{i}", probe) for i in range(3)]
            )
            assert not unreachable
            return len(tools)
        raise AssertionError("the exit stack cannot fall through")

    # No exception on the way out is the assertion; the count confirms all three really opened.
    assert asyncio.run(_open_three()) == 6


def test_the_manifest_allow_list_bounds_what_a_session_advertises(probe: int) -> None:
    """`allowed_tools` narrows the live surface, as MAF's `_filtered_functions` did in the tool.

    The server offers two tools and the allow-list names one, so the model must be handed one.
    Without this the narrowing would stop at the process boundary — the server would happily
    advertise everything it has, and a profile's attenuation would end where MCP begins.
    """

    async def _open() -> list[str]:
        async with AsyncExitStack() as stack:
            tools, _ = await open_connector_specs(
                stack, [_spec("lg-probe", probe, allowed=("echo",))]
            )
            return [t.name for t in tools]
        raise AssertionError("the exit stack cannot fall through")

    assert asyncio.run(_open()) == ["echo"]


def test_concurrent_turns_get_their_own_session_and_their_own_identity() -> None:
    """Per-turn sessions, proven the way the MAF twin proves it: two overlapping turns, two actors.

    Every request must carry the identity of the turn that made it. A shared session would send
    the second turn's calls over a connection opened in the first turn's context, attributing
    them to the wrong user in the connector's own log — which is why the graph is compiled per
    turn on this engine rather than held per process.
    """
    seen: list[str] = []
    port = _free_port()

    async def _turn(actor: str) -> None:
        token = set_current_identity(actor, frozenset())
        try:
            async with AsyncExitStack() as stack:
                tools, _ = await open_connector_specs(stack, [_spec("ident-probe", port)])
                slow = next(tool for tool in tools if tool.name == "slow")
                await slow.ainvoke({})
        finally:
            reset_current_identity(token)

    async def _both() -> None:
        await asyncio.gather(_turn("user-A"), _turn("user-B"))

    with _Server(_probe_app("ident-probe", capture=seen), port):
        asyncio.run(_both())

    assert set(seen) == {"user-A", "user-B"}


def test_a_profile_narrows_connectors_identically_on_both_engines() -> None:
    """Attenuation is the same decision whichever engine reads it.

    A profile that dropped a different set of connectors depending on `agent_engine` would be a
    different security posture behind one config value — the drift this migration's shared
    decisions exist to make impossible. Asserted against the *live* manifests rather than a
    fixture, so a new bundle is covered the day it lands.
    """
    profile = AgentProfile(
        name="narrow-both", tool_names=frozenset({"predict_pka", "screen_hazards"})
    )
    maf = {tool.name: tuple(tool.allowed_tools or ()) for tool in connector_tools(profile)}
    graph = {spec.name: tuple(spec.allowed_tools or ()) for spec in connector_specs(profile)}
    assert maf == graph
    assert maf == {"calc": ("predict_pka",), "safety": ("screen_hazards",)}


def test_compiling_the_graph_per_turn_stays_within_the_maf_agent_build_budget() -> None:
    """Per-turn compilation is what M7 costs; this measures it against D-123's ~90 ms baseline.

    LangGraph binds tools at construction, so a turn's connectors force a fresh compile — where
    MAF built one `Agent` per process and appended run-scoped tools. D-123 measured MAF's build at
    ~90 ms and called it "not expensive enough to fear"; this asserts the graph is no worse, since
    that is the number the decision to compile per turn was taken against.

    The bound is deliberately loose because a CI box is not a benchmark rig and a flaky
    performance test is worse than none. It is here to catch an order-of-magnitude regression — a
    compile that started dialing something, or rebuilt the skills tree per turn — not to police
    milliseconds. The measured figure is printed so a reader sees the real number, not only the
    bound.
    """
    model = ScriptedChatModel(["ok"])
    build_langgraph_agent(model, audit_sink=NullAuditSink())  # warm discovery, as a live pod is

    rounds = 5
    started = time.perf_counter()
    for _ in range(rounds):
        build_langgraph_agent(model, audit_sink=NullAuditSink())
    per_compile_ms = (time.perf_counter() - started) / rounds * 1000

    assert per_compile_ms < 270, f"per-turn graph compile took {per_compile_ms:.0f} ms"
    print(f"\nper-turn graph compile: {per_compile_ms:.0f} ms (MAF agent build baseline ~90 ms)")
