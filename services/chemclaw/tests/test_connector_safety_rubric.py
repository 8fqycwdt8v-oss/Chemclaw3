"""A connector tool is governed exactly like an in-process one — the claim the whole seam rests on.

Moving capability out of process is only acceptable if it changes *where a tool runs* and
nothing about *what governs it*. Two of the four safety-rubric invariants are the ones a process
boundary could plausibly break, because both are MAF function middleware and a connector's tools
are not MAF
functions we wrote:

1. **GxP audit** — every call recorded with the turn's actor, whether it succeeded or was denied.
2. **Per-tool authorization** — `tool_role_gates` addresses a connector tool by the same name the
   model calls, and a denied call never runs on the connector.

Neither can be shown by inspecting the wiring: MAF assembles MCP tools into the run's tool list
at a different point from the configured ones, so "the middleware list is attached" proves
nothing about whether it wraps *these*. So this drives the real thing — a real `Agent`, a real
connector server over HTTP, MAF's own tool-calling loop — and asserts on what the audit sink
recorded and what the server received.

The other two invariants need no test here because the boundary makes them structural: a
connector has no PR-gate access (its only route into the graph is a job result core publishes)
and no way to launch durable work (a `jobs:` entry is a core-generated tool). What could regress
is governance of the *call*, which is what this file pins.
"""

import asyncio
import socket
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

import pytest
import uvicorn
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from agent_framework._tools import FunctionInvocationLayer
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from agents.audit import AuditEvent
from agents.chemclaw_agent import build_agent, connector_tools
from agents.identity_context import reset_current_identity, set_current_identity
from connectors.identity import HEADER_ACTOR
from connectors.registry import discovered, open_reachable

_BUNDLE = """\
name: governed
description: a test connector whose one tool is called through the real agent
endpoint:
  transport: http
  url: {url}
  tools:
    - echo_subject
"""


class _ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):
    """A real chat client with scripted replies, so MAF's own tool-calling loop does the calling.

    Same shape as `tests/test_harness_execution.py`'s: only the model's replies are fake. The
    tool execution path — including whatever middleware wraps it — is the framework's real one,
    which is the entire point of testing governance here rather than against a stand-in context.
    """

    def __init__(self, script: Sequence[Callable[[], ChatResponse]]) -> None:
        """Start with the given reply script, consumed one entry per model call."""
        super().__init__()
        self._script = list(script)

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        """Pop and return the next scripted reply."""
        response = self._script.pop(0)()

        async def _await_response() -> ChatResponse:
            return response

        return _await_response()


def _call(name: str, arguments: dict[str, object]) -> Callable[[], ChatResponse]:
    """A scripted turn that asks for one tool call."""

    def _reply() -> ChatResponse:
        return ChatResponse(
            messages=[
                Message(
                    role="assistant",
                    contents=[Content.from_function_call("c1", name, arguments=arguments)],
                )
            ],
            response_id="r",
        )

    return _reply


def _text(text: str) -> Callable[[], ChatResponse]:
    """A scripted turn that replies with plain text, ending the loop."""

    def _reply() -> ChatResponse:
        return ChatResponse(
            messages=[Message(role="assistant", contents=[Content.from_text(text)])],
            response_id="r",
        )

    return _reply


class _RecordingSink:
    """An `AuditSink` that keeps every event, so the test can assert on the GxP trail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Keep the event; the real sink writes a hash-chained row."""
        self.events.append(event)


def _free_port() -> int:
    """An unused localhost port, so concurrent runs cannot collide."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    """Serve a one-tool connector and point the registry at its bundle; yields what it observed."""
    from connectors.server import connector_app

    observed = _Observed()
    server = FastMCP("governed")

    @server.tool()
    async def echo_subject(subject: str) -> str:
        """Echo the subject back — enough for a real tool call to happen."""
        observed.invocations.append(subject)
        return f"echoed {subject}"

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
    monkeypatch.setattr("chemclaw.config.settings.connectors_dir", str(tmp_path))
    monkeypatch.setattr("chemclaw.config.settings.connectors_enabled", "")
    discovered.cache_clear()
    try:
        with _Server(app, port):
            yield observed
    finally:
        discovered.cache_clear()


def _run_turn_calling(tool_name: str, sink: _RecordingSink) -> str:
    """Drive one real agent turn whose scripted model calls `tool_name`, and return its text."""
    agent = build_agent(
        chat_client=_ScriptedChatClient([_call(tool_name, {"subject": "benzene"}), _text("done")]),
        audit_sink=sink,
    )

    async def _go() -> str:
        async with AsyncExitStack() as stack:
            turn_connectors = connector_tools()
            await open_reachable(stack, turn_connectors)
            response = await agent.run("check the subject", tools=turn_connectors)
            return str(response.text)
        raise AssertionError("unreachable")  # pragma: no cover - satisfies the type checker

    return asyncio.run(_go())


def test_a_connector_tool_call_is_recorded_in_the_gxp_audit_trail(governed: _Observed) -> None:
    """Invariant 1: the audit middleware wraps a connector's tool, not just the in-process ones.

    This is the assertion that could not be made by reading the wiring. MAF appends MCP-derived
    functions to the run's tool list separately from the configured ones, so whether the agent's
    middleware chain reaches them is a property of the framework, not of our construction — and
    it is the property the whole out-of-process move depends on.
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
    monkeypatch.setattr("chemclaw.config.settings.entra_required", True)
    monkeypatch.setattr(
        "chemclaw.config.settings.tool_role_gates", {"echo_subject": ["structure-analyst"]}
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
    assert "lacks a role permitted to call echo_subject" in denial.detail
    # The decisive half: the tool never ran. The gate is in core, before the call goes out — the
    # connection and the tool listing still happen, because those precede any model decision.
    assert governed.invocations == []
