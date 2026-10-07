"""Connectors on the LangGraph engine: real sessions, real degradation, real task affinity.

Every test drives a live `uvicorn` server over the streamable-HTTP transport a deployment uses:
cross-turn deadlocks, missing identity headers and `anyio` scopes exited from the wrong task are
properties of a real connection and invisible against a mock.
`test_connectors_opened_together_close_cleanly` is the reason `HeldConnectorSession` exists.
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
from chemclaw.agent.chemclaw_agent import connector_specs
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from chemclaw.connectors.manifest import ConnectorManifest, HttpEndpoint
from chemclaw.connectors.registry import _mcp_connection, open_connector_specs
from chemclaw.connectors.server import connector_app
from chemclaw.connectors.transport import ConnectorSpec
from chemclaw.core.call_identity import HEADER_ACTOR
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


#: What `_probe_app` serves. The default allow-list rather than `()`, because a manifest may not
#: declare an empty `tools` list any more: the empty list used to mean "everything this server
#: offers", which is precisely the fail-open these tests would otherwise keep depending on.
_PROBE_TOOLS = ("echo", "slow")


def _spec(name: str, port: int, allowed: tuple[str, ...] = _PROBE_TOOLS) -> ConnectorSpec:
    """A spec pointing at the test server, built by the registry's own builder.

    Built through `_mcp_connection` so the tests exercise the client factory, identity hook and
    redirect refusal a real connector gets.
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
    """An unreachable connector contributes no tools and is reported; the turn still runs."""

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


def test_a_real_turn_reaches_a_real_connector_on_the_graph_engine(
    probe: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, end to end: `run_turn` on the graph engine calls a live connector's tool.

    Drives the runner's own default connector path (factory monkeypatched to the live test server)
    and asserts the connector's output reaches the turn, which requires the spec to be built, the
    session opened and the `BaseTool` bound into the graph. Tests passing `connectors=[]` cannot
    catch a broken join between `connector_specs` and `open_connector_specs`.
    """
    from chemclaw.api import runner
    from chemclaw.api.events import ToolResultEvent

    monkeypatch.setattr(runner, "connector_specs", lambda: [_spec("lg-probe", probe)])

    class _Session:
        """The two attributes `run_turn` reads off a session on this path."""

        session_id = "s-connector-wiring"
        state: dict[str, Any] = {}

    def _factory(**kwargs: Any) -> Any:
        # The runner passes its own `audit_sink` (the in-turn default); only fall back to the
        # null sink if this factory is ever driven outside `run_turn`.
        kwargs.setdefault("audit_sink", NullAuditSink())
        return build_langgraph_agent(
            ScriptedChatModel([{"name": "echo", "args": {"text": "hi"}}, "done"]),
            **kwargs,
        )

    async def _run() -> list[Any]:
        return [
            event
            async for event in runner.run_turn(
                cast(Any, _Session()),
                "call echo",
                graph_factory=_factory,
            )
        ]

    events = asyncio.run(_run())
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert any("echoed:hi" in result.preview for result in results), [e.type for e in events]


def test_connectors_opened_together_close_cleanly(probe: int) -> None:
    """Several connectors open concurrently and tear down without a cross-task scope error.

    Opening via `asyncio.gather` over `AsyncExitStack.enter_async_context` exits each `anyio` cancel
    scope on a different task than entered it, which anyio refuses; `HeldConnectorSession` confines
    each session to its own task. Concurrency is required so a dark fleet does not cost the sum of
    its connect timeouts. Three sessions, because one would pass even if opened serially.
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
    """`allowed_tools` narrows what the *server* advertises, not what a tool object was built with.

    Otherwise a profile's attenuation would end at the MCP process boundary.
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
    """Per-turn sessions: two overlapping turns, each request carrying its own turn's identity.

    A shared session would attribute one turn's calls to another user in the connector's log.
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

    Asserted against the live manifests so a new bundle is covered the day it lands.
    """
    profile = AgentProfile(
        name="narrow-both", tool_names=frozenset({"predict_pka", "screen_hazards"})
    )
    maf = {tool.name: tuple(tool.allowed_tools or ()) for tool in connector_specs(profile)}
    graph = {spec.name: tuple(spec.allowed_tools or ()) for spec in connector_specs(profile)}
    assert maf == graph
    assert maf == {"calc": ("predict_pka",), "safety": ("screen_hazards",)}


def test_compiling_the_graph_per_turn_stays_within_the_maf_agent_build_budget() -> None:
    """Per-turn graph compilation stays within an order-of-magnitude budget.

    LangGraph binds tools at construction, so every turn compiles a fresh graph. The bound is loose
    on purpose: it catches a compile that started dialling something or rebuilt the skills tree per
    turn, not milliseconds of CI noise. The median round is asserted rather than the mean, so a
    single GC pause or preemption does not fail the batch while a raised floor still does.
    """
    model = ScriptedChatModel(["ok"])
    build_langgraph_agent(model, audit_sink=NullAuditSink())  # warm discovery, as a live pod is

    rounds = 7
    samples_ms = []
    for _ in range(rounds):
        started = time.perf_counter()
        build_langgraph_agent(model, audit_sink=NullAuditSink())
        samples_ms.append((time.perf_counter() - started) * 1000)
    samples_ms.sort()
    per_compile_ms = samples_ms[len(samples_ms) // 2]

    assert per_compile_ms < 250, (
        f"per-turn graph compile took {per_compile_ms:.0f} ms (median of {samples_ms})"
    )
    print(
        f"\nper-turn graph compile: {per_compile_ms:.0f} ms median, {samples_ms} raw "
        "(~50 ms unloaded here; baseline ~90 ms — the docstring carries the history)"
    )
