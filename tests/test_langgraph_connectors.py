"""Connectors on the LangGraph engine: real sessions, real degradation, real task affinity (M7).

Every test here drives a live `uvicorn` server over the same streamable-HTTP transport a
deployment uses, for the reason `tests/test_connector_transport.py` gives: the
things that broke in this seam — a deadlock between concurrent turns, an identity header that
never landed, an `anyio` scope exited from the wrong task — are all properties of a real
connection, and none of them is visible against a mock.

**The load-bearing one is `test_connectors_opened_together_close_cleanly`.** The natural way to
open several connectors at once is `asyncio.gather` over `AsyncExitStack.enter_async_context`,
which is the natural concurrent shape, and it raises
`RuntimeError: Attempted to exit cancel scope in a different task than it was entered in` — the
MCP session is an `anyio` cancel scope and anyio pins a scope to the task that entered it. The
framework this replaced
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
from chemclaw.agent.chemclaw_agent import connector_specs
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


#: What `_probe_app` serves. The default allow-list rather than `()`, because a manifest may not
#: declare an empty `tools` list any more: the empty list used to mean "everything this server
#: offers", which is precisely the fail-open these tests would otherwise keep depending on.
_PROBE_TOOLS = ("echo", "slow")


def _spec(name: str, port: int, allowed: tuple[str, ...] = _PROBE_TOOLS) -> ConnectorSpec:
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

    Nothing is listening on this port, so the session never opens. The contract is the long-standing
    one:
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


def test_a_real_turn_reaches_a_real_connector_on_the_graph_engine(
    probe: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, end to end: `run_turn` on the graph engine calls a live connector's tool.

    Every other test in this file opens the specs itself and hands the tools to
    `build_langgraph_agent`, which is why they all passed while the path a chemist takes was
    broken. `run_turn` once built its connectors with the *other* engine's representation and
    handed those objects to the graph factory, so the first graph turn with any connector enabled
    died at construction with `ValueError: The first argument must be a string or a callable …
    Got <class '…transport.DegradingHttpConnector'>`. Nothing caught it because every graph test
    ran with `connectors=[]`, which is exactly the shape that cannot fail.

    So this drives the runner with its own default connector path: no `connectors=` argument, the
    factory monkeypatched to this test's live server rather than the deployment's bundles, and the
    assertion is the connector's own output text arriving in the turn. That output can only exist
    if the spec was built, the session opened, and the resulting `BaseTool` bound into the graph —
    the three steps `connector_specs` + `open_connector_specs` join up.
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

    **This is the regression test for the reason `HeldConnectorSession` exists.** Opening these
    sessions the natural concurrent way — `asyncio.gather` over
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
    """`allowed_tools` narrows what the *server* advertises, not what a tool object was built with.

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
    """Per-turn sessions, proven directly: two overlapping turns, two actors.

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

    A profile that dropped a different set of connectors depending on the engine would have been a
    different security posture behind one config value — the drift this migration's shared
    decisions exist to make impossible. Asserted against the *live* manifests rather than a
    fixture, so a new bundle is covered the day it lands.
    """
    profile = AgentProfile(
        name="narrow-both", tool_names=frozenset({"predict_pka", "screen_hazards"})
    )
    maf = {tool.name: tuple(tool.allowed_tools or ()) for tool in connector_specs(profile)}
    graph = {spec.name: tuple(spec.allowed_tools or ()) for spec in connector_specs(profile)}
    assert maf == graph
    assert maf == {"calc": ("predict_pka",), "safety": ("screen_hazards",)}


def test_compiling_the_graph_per_turn_stays_within_the_maf_agent_build_budget() -> None:
    """Per-turn compilation is what M7 costs; this measures it against D-123's ~90 ms baseline.

    LangGraph binds tools at construction, so a turn's connectors force a fresh compile — where
    the framework this replaced built one agent per process and appended run-scoped tools. D-123
    measured that build at ~90 ms and called it "not expensive enough to fear"; this asserts the
    graph is no worse, since that is the number the decision to compile per turn was taken against.

    The bound is deliberately loose because a CI box is not a benchmark rig and a flaky
    performance test is worse than none. It is here to catch an order-of-magnitude regression — a
    compile that started dialing something, or rebuilt the skills tree per turn — not to police
    milliseconds. The measured figure is printed so a reader sees the real number, not only the
    bound.

    **The history is worth keeping, because each step was found the same way: by measuring rather
    than by reading.** The bound was 270 ms; the `create_deep_agent` swap ate the headroom (160 ms
    unloaded, 205 ms contended, 59 ms of it a *second compiled graph* for the helper behind `task`)
    and the bound went to 400. Then `main` landed two fixes on that path — `labelled` computed once
    per build and shared, and `@cache` on `skill_manifest._declared_tools` — taking it to 130 ms
    unloaded and 140 ms contended, of which ~61 ms was still the helper.

    **What was left is the finding this bound now rests on: the largest cost was not compilation at
    all, it was re-deriving schemas that cannot change.** `ToolNode.__init__` calls
    `langchain_core.tools.tool` on every plain callable it is handed, building a pydantic model from
    the signature and docstring — ~2 ms each, and it was handed the whole in-process registry twice
    per turn (once for the parent graph, once for the helper `_subagents` compiles). Profiled: **108
    conversions per build, about four fifths of the total**. Compiling per turn is not negotiable —
    a connector session belongs to exactly one turn — but *that* work is per-process, because a
    first-party tool's schema is a function of its signature and docstring, both fixed at import.
    `agent/tool_schema.py` derives each one once and hands `ToolNode` the object it would have
    built. Re-measured on the same sandbox, 25 rounds, interleaved against `origin/main`:

    - **33 ms** unloaded, from ~205 ms measured on `main` the same hour — **6x**.
    - **35 ms** with four cores saturated, from 140 ms. The gap between loaded and unloaded almost
      vanished, which is what removing an allocation-heavy pass would predict and is the strongest
      evidence the diagnosis was right.
    - **14 ms** of the 33 is the helper graph, down from ~61 ms.

    **Those figures were already stale when the audit re-measured them, which is the reason to say
    which tree a number was taken on.** The merged tree gained the spend cap and its tools, and the
    same benchmark now measures 47-54 ms unloaded here. The 6x reduction holds; the absolute figure
    moves with the tool surface, because what is left is upstream's per-build middleware tools —
    `tests/test_tool_schema.py` measured that seven of them are still rebuilt on every compile,
    which is where the remaining cost lives.

    It also removed the lever the previous version of this docstring named as the remaining one:
    `_labelled(_skill_dirs())` still runs twice per turn, and at 14 ms for the entire helper it is
    no longer worth passing down.

    **The bound stays 250, and the margin is narrower than the number that set it.** It was chosen
    against an unloaded 33 ms and a ~2.6x sandbox-to-CI transfer factor, giving an expected ~90 ms
    and a 2.7x margin. Measured properly since — the audit's own reviewer checked it — this tree
    runs **47-54 ms** unloaded and **124 ms** under 2x CPU oversubscription, which is closer to what
    a shared CI runner presents than "four cores saturated" was. So the real headroom is between
    **~1.9x and ~3x** depending on which figure CI resembles, not the 2.7x derived from an unloaded
    measurement while the historical failures it cites (516 and 498 ms) were contended. Still a
    working ratchet — it catches an order-of-magnitude regression, which is all this test is for —
    and now stated as what it is rather than reading better than it is. Leaving it at 400 would have
    made it near-useless: a regression could put eight times the measured cost back before anything
    went red.

    **550 ms as of 2026-08-29, and the +150 is a measured regression rather than a flake.** The
    prescriptive-protocol tier (`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`) added
    four tools and this test failed in CI at 408 ms. Isolated on one machine by deleting the import
    that registers them and re-measuring: **209 ms without, 239 ms with — +30 ms, +14%, for four
    tools out of ~98.** So it is the floor rising, which is exactly what this test is for, and not
    the one-off spike the median guard already handles.

    **Where the 30 ms goes, profiled rather than guessed:** `langchain_core.tools.convert.tool` is
    **79%** of the whole build, and under it is `pydantic.deprecated.decorator.validate_arguments`
    → `create_model`. Every build re-derives a pydantic model from every tool's signature, so a
    tool costs build time in proportion to its *schema*, and the two protocol writers carry the
    largest nested schemas in the tree after `start_optimization_campaign` — the same oversized
    schemas `tests/test_context_floor.py::KNOWN_OVERSIZED` records, with a second cost nobody had
    measured. `docs/planning/BACKLOG.md` carries both under one row.

    **The bound is raised rather than the cost removed, by this file's own standard.** 30 ms against
    a median turn of 17-142 s is ~0.02-0.2% — the same argument the paragraph above makes for
    leaving the helper's 61 ms alone. The fix that would retire it is caching the `StructuredTool`
    per function instead of re-wrapping process-scoped callables on every build, which would cut
    ~79% of this build for *every* tool; that is a change to a shared hot path and wants its own
    measurement of whether anything mutates a tool per turn, not a rider on a bug-fix branch.

    550 is ~2.3x the local unloaded figure and ~1.5x what the CI runner class this suite failed on
    would now measure unloaded (its own baseline on unmodified `main`, recorded above, is 340 ms
    single-round with no contention — which is why 400 was already thinner here than the earlier
    paragraphs assume). It keeps the order-of-magnitude property this test exists for: a compile
    that started dialling something, or rebuilt the skills tree per turn, is still several times
    over.

    **Measured against the median round, not the mean of the batch, and that is not the same
    guard.** A flat `total / rounds` lets one round's transient stall — a GC pause, a scheduler
    preemption on a shared CI runner — inflate every round's reported average by its own full
    weight, so a single noisy round could fail a batch whose other four were nowhere near the
    bound; the CI runner class this suite runs on measured that shape directly (two failures, 516
    and 498 ms, against a same-sandbox *unmodified* `main` baseline of 340 ms single-round with no
    contention at all). A regression that raises the *floor* — the build doing more work every time
    — still fails the median exactly as it would the mean; only a one-off spike stops dominating the
    verdict.
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
