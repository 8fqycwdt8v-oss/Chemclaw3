"""A connector tool is governed exactly like an in-process one — the claim the whole seam rests on.

Moving capability out of process is only acceptable if it changes *where a tool runs* and
nothing about *what governs it*. Two of the four safety-rubric invariants are the ones a process
boundary could plausibly break, because both are tool-call middleware and a connector's tools are
not functions we wrote:

1. **GxP audit** — every call recorded with the turn's actor, whether it succeeded or was denied.
2. **Per-tool authorization** — `tool_role_gates` addresses a connector tool by the same name the
   model calls, and a denied call never runs on the connector.

Neither can be shown by inspecting the wiring: a connector's tools reach the model by a different
route from the configured ones, so "the middleware list is attached" proves nothing about whether
it wraps *these*. So this drives the real thing — a real compiled graph, a real connector server
over HTTP, a real tool node — and asserts on what the audit sink recorded and what the server
received.

The other two invariants need no test here because the boundary makes them structural: a
connector has no PR-gate access (its only route into the graph is a job result core publishes)
and no way to launch durable work (a `jobs:` entry is a core-generated tool). What could regress
is governance of the *call*, which is what this file pins.
"""

import asyncio
import threading
from collections.abc import Iterator
from contextlib import AsyncExitStack
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.chemclaw_agent import connector_specs
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.connectors.identity import HEADER_ACTOR
from chemclaw.connectors.registry import open_connector_specs
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.turn_signals import _KEY, Signal, ToolFailureSignal
from tests.conftest import _free_port
from tests.fakes_langgraph import ScriptedChatModel

_BUNDLE = """\
name: governed
description: a test connector whose one tool is called through the real agent
endpoint:
  transport: http
  url: {url}
  tools:
    - echo_subject
    - fail_subject
  read_only:
    - echo_subject
    - fail_subject
"""


class _RecordingSink:
    """An `AuditSink` that keeps every event, so the test can assert on the GxP trail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Keep the event; the real sink writes a hash-chained row."""
        self.events.append(event)


class _Server:
    """A uvicorn server on a background thread, started and stopped around one test."""

    def __init__(self, app: FastAPI, port: int) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "_Server":
        """Start the server and wait until it accepts connections."""
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return self
            threading.Event().wait(0.05)
        raise RuntimeError("connector test server did not start")

    def __exit__(self, *_exc: object) -> None:
        """Stop the server and join its thread."""
        self._server.should_exit = True
        self._thread.join(timeout=10)


class _Observed:
    """What the connector actually saw, so a test can tell *connecting* apart from *being called*.

    The distinction is the whole point of the denial test: opening the connection and
    discovering the tool list happen before the model chooses anything, so they are not evidence
    the gate leaked — only the tool body running is.
    """

    def __init__(self) -> None:
        self.actors: list[str] = []
        self.invocations: list[str] = []


@pytest.fixture
def governed(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Observed]:
    """Serve a one-tool connector and point the registry at its bundle; yields what it observed.

    Discovery is cached, but `tests/conftest.py`'s autouse fixture clears it around every test, so
    repointing `connectors_dir` here needs no local `cache_clear()`.
    """
    from chemclaw.connectors.server import connector_app

    observed = _Observed()
    server = FastMCP("governed")

    @server.tool()
    async def echo_subject(subject: str) -> str:
        """Echo the subject back — enough for a real tool call to happen."""
        observed.invocations.append(subject)
        return f"echoed {subject}"

    @server.tool()
    async def fail_subject(subject: str) -> str:
        """Fail the way a connector tool actually fails: raise *over there*, out of core's reach.

        The exception never crosses the process boundary as an exception. MCP reports it as a result
        with `isError=True`, which `langchain_mcp_adapters` converts inside `StructuredTool.ainvoke`
        into a returned `ToolMessage(status="error")` — so nothing raises in this process, and every
        reader that decides success by control flow calls this a success.
        """
        observed.invocations.append(subject)
        raise ChemclawError("the instrument is offline")

    app = connector_app(server, name="governed")

    @app.middleware("http")
    async def _capture(request: Any, call_next: Any) -> Any:
        """Record the actor of each request, so a *denied* call is visibly never sent."""
        actor = request.headers.get(HEADER_ACTOR.lower())
        if actor:
            observed.actors.append(actor)
        return await call_next(request)

    port = _free_port()
    bundle = tmp_path / "governed"
    bundle.mkdir()
    (bundle / "connector.yaml").write_text(
        _BUNDLE.format(url=f"http://127.0.0.1:{port}/mcp"), encoding="utf-8"
    )
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(tmp_path))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")
    with _Server(app, port):
        yield observed


async def _turn_calling(tool_name: str, sink: _RecordingSink, stack: AsyncExitStack) -> Any:
    """The compiled graph for one turn whose scripted model calls `tool_name`.

    Split out of `_run_turn_calling` because the signal test needs the *same* turn driven on the
    custom stream rather than through `ainvoke` — two ways of running one graph, not two graphs.
    """
    connectors, _unreachable = await open_connector_specs(stack, connector_specs())
    return build_langgraph_agent(
        model=ScriptedChatModel([{"name": tool_name, "args": {"subject": "benzene"}}, "done"]),
        audit_sink=sink,
        connectors=connectors,
    )


def _run_turn_calling(tool_name: str, sink: _RecordingSink) -> str:
    """Drive one real agent turn whose scripted model calls `tool_name`, and return its text."""

    async def _go() -> str:
        async with AsyncExitStack() as stack:
            graph = await _turn_calling(tool_name, sink, stack)
            result = await graph.ainvoke({"messages": [("user", "check the subject")]})
            return str(result["messages"][-1].content)
        raise AssertionError("unreachable")  # pragma: no cover - satisfies the type checker

    return asyncio.run(_go())


def _signals_from_turn_calling(tool_name: str, sink: _RecordingSink) -> list[Signal]:
    """Drive the same turn on the graph's custom stream and return what it announced.

    The chemist's transcript is fed by `chemclaw.core.turn_signals`, which publishes through
    `get_stream_writer()` — so the only honest way to ask "did the chemist see this fail" is to
    consume the real custom stream of the real graph, exactly as `tests/signals.collect_signals`
    does for a bare tool body. `stream_mode="custom"` yields only those payloads.
    """

    async def _go() -> list[Signal]:
        async with AsyncExitStack() as stack:
            graph = await _turn_calling(tool_name, sink, stack)
            signals: list[Signal] = []
            async for payload in graph.astream(
                {"messages": [("user", "check the subject")]}, stream_mode="custom"
            ):
                if isinstance(payload, dict) and isinstance(payload.get(_KEY), Signal):
                    signals.append(payload[_KEY])
            return signals
        raise AssertionError("unreachable")  # pragma: no cover - satisfies the type checker

    return asyncio.run(_go())


def test_a_connector_tool_call_is_recorded_in_the_gxp_audit_trail(governed: _Observed) -> None:
    """Invariant 1: the audit middleware wraps a connector's tool, not just the in-process ones.

    This is the assertion that could not be made by reading the wiring. A connector's tools are
    loaded off a live MCP session rather than registered like the configured ones, so whether the
    middleware chain reaches them is a property of how the framework binds tools, not of our
    construction — and it is the property the whole out-of-process move depends on.
    """
    sink = _RecordingSink()
    identity = set_current_identity("user-42", frozenset({"process-chemist"}))
    try:
        answer = _run_turn_calling("echo_subject", sink)
    finally:
        reset_current_identity(identity)

    assert answer == "done"
    recorded = {event.tool: event for event in sink.events}
    assert "echo_subject" in recorded, f"connector tool was not audited; saw {sorted(recorded)}"
    event = recorded["echo_subject"]
    assert event.outcome == "ok"
    assert event.actor == "user-42"  # attributed to the turn's user, not the service identity
    assert "benzene" in event.arguments  # the call's arguments are in the trail
    # And the call really did leave the process and run there, under that user's identity — on
    # every request the connection made (handshake, discovery, the call), never under some other
    # one.
    assert governed.invocations == ["benzene"]
    assert governed.actors and set(governed.actors) == {"user-42"}


def test_a_failed_connector_tool_is_audited_as_an_error_not_a_success(
    governed: _Observed,
) -> None:
    """Invariant 1's other half: the trail's `outcome` means the same thing across the boundary.

    An in-process tool signals failure by raising, and the audit middleware reads that off control
    flow. **An MCP tool never raises**: `langchain_mcp_adapters` attaches `handle_tool_error`, so an
    `isError=True` result is converted inside `StructuredTool.ainvoke` and returned as a
    `ToolMessage(status="error")`. Deriving the outcome from control flow alone therefore wrote `ok`
    for every failed connector call — with the error text in `detail`, the field an auditor reads as
    the call's *effect*. A GxP trail that records a failure as a success is worse than one that
    records nothing, because it looks answered.
    """
    sink = _RecordingSink()
    identity = set_current_identity("user-42", frozenset({"process-chemist"}))
    try:
        answer = _run_turn_calling("fail_subject", sink)
    finally:
        reset_current_identity(identity)

    # The turn survives the failed step — that part was never broken and must stay true.
    assert answer == "done"
    assert governed.invocations == ["benzene"]  # the tool really did run, and really did fail
    recorded = {event.tool: event for event in sink.events}
    event = recorded["fail_subject"]
    assert event.outcome == "error", "a failed connector call was recorded as a success"
    assert "the instrument is offline" in event.detail
    assert event.actor == "user-42"  # still attributed to the turn's user


def test_a_failed_connector_tool_is_announced_to_the_chemist(governed: _Observed) -> None:
    """The transcript says the step did not work — the other reader control flow had misled.

    `announce_tool_failures` only caught exceptions, so a connector failure raised no
    `ToolFailureSignal`. That is not merely a missing announcement: `api/graph_stream` suppresses a
    `ToolMessage` with `status == "error"` on the documented ground that it is "already reported as
    tool_failed" — false for exactly these tools — so the failed call left a `tool_call` event with
    no result and no failure beside it, and vanished from the transcript entirely.
    """
    sink = _RecordingSink()
    identity = set_current_identity("user-42", frozenset({"process-chemist"}))
    try:
        signals = _signals_from_turn_calling("fail_subject", sink)
    finally:
        reset_current_identity(identity)

    failures = [signal for signal in signals if isinstance(signal, ToolFailureSignal)]
    assert [failure.tool for failure in failures] == ["fail_subject"], (
        f"the chemist was never told the step failed; saw {signals}"
    )
    assert "the instrument is offline" in failures[0].message


def test_a_successful_connector_tool_announces_no_failure(governed: _Observed) -> None:
    """The mirror of the two tests above: nothing is reported for a call that worked.

    Worth pinning because the fix widened what `announce_tool_failures` inspects. A predicate that
    fired on any returned `ToolMessage` — or on a *refusal*, which is deliberately not
    `status="error"` — would flood the transcript with failures for calls that succeeded, which is
    the same class of defect as the one being fixed, mirrored.
    """
    sink = _RecordingSink()
    identity = set_current_identity("user-42", frozenset({"process-chemist"}))
    try:
        signals = _signals_from_turn_calling("echo_subject", sink)
    finally:
        reset_current_identity(identity)

    assert [signal for signal in signals if isinstance(signal, ToolFailureSignal)] == []
    assert {event.tool: event.outcome for event in sink.events}["echo_subject"] == "ok"


def test_a_denied_connector_tool_never_runs_and_is_still_audited(
    governed: _Observed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 2: `tool_role_gates` addresses a connector tool by name, and denial is recorded.

    Two halves, and both matter. The gate must *stop* the call — a connector that is reachable
    from the front door would otherwise happily serve a user who was refused — and the denial
    must still appear in the trail, because "who was refused what" is exactly the question a GxP
    audit asks. The audit middleware is attached outermost for this reason, and this proves it
    holds across the process boundary too.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.entra_required", True)
    monkeypatch.setattr(
        "chemclaw.core.config.settings.tool_role_gates", {"echo_subject": ["structure-analyst"]}
    )
    sink = _RecordingSink()
    identity = set_current_identity("user-42", frozenset({"process-chemist"}))  # lacks the role
    try:
        _run_turn_calling("echo_subject", sink)
    finally:
        reset_current_identity(identity)

    recorded = {event.tool: event for event in sink.events}
    denial = recorded["echo_subject"]
    assert denial.outcome == "error"
    assert "AuthorizationError" in denial.detail
    # The message names *who* was refused and *which* tool, which is what an auditor reads; the
    # exact wording is `agents.authz`'s to own, so this asserts the parts that must not drift.
    assert "not authorized to use echo_subject" in denial.detail
    assert "user-42" in denial.detail
    # The decisive half: the tool never ran. The gate is in core, before the call goes out — the
    # connection and the tool listing still happen, because those precede any model decision.
    assert governed.invocations == []
