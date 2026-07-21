"""The front-door HTTP surface runs a turn end-to-end with a fake agent (plan step F2-T1/F2-T2).

Exercises the real FastAPI app (health/readiness, session creation, the SSE message stream, the
static chat page) with an injected fake streaming agent — so the whole surface is proven without a
live model, MCP subprocess, or credentials. The MCP lifecycle is asserted to open/close exactly once
per turn via a spy tool.
"""

import json

from agent_framework import AgentSession
from fastapi.testclient import TestClient

from service.app import create_app


class _SpyMcpTool:
    """An async-context-manager stand-in for an MCP tool that records enter/exit."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "_SpyMcpTool":
        self.entered += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1


class _Update:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.contents: list[object] = []
        self.user_input_requests: list[object] = []


class _FakeAgent:
    """Fake agent: yields two tokens per turn and exposes one spy MCP tool."""

    def __init__(self) -> None:
        self.mcp_tools = [_SpyMcpTool()]

    def create_session(self, *, session_id: str) -> AgentSession:
        return AgentSession(session_id=session_id)

    def run(self, message: str, *, stream: bool, session: AgentSession) -> object:
        async def _gen() -> object:
            yield _Update(text="hi ")
            yield _Update(text="there")

        return _gen()


def _client(agent: _FakeAgent) -> TestClient:
    return TestClient(create_app(agent_factory=lambda: agent))


def test_healthz_is_ok() -> None:
    """Liveness needs no agent and returns 200."""
    with _client(_FakeAgent()) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_static_chat_page_is_served() -> None:
    """The browser chat surface is served at the root."""
    with _client(_FakeAgent()) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "Chemclaw" in res.text


def test_message_stream_runs_a_turn_and_opens_mcp_once() -> None:
    """Create a session, post a message, stream the turn's events; MCP opens/closes once."""
    agent = _FakeAgent()
    spy = agent.mcp_tools[0]
    with _client(agent) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hello"}
        ) as res:
            assert res.status_code == 200
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    kinds = [e["type"] for e in events]
    assert kinds == ["token", "token", "answer"]
    assert "".join(e["text"] for e in events if e["type"] == "token") == "hi there"
    assert spy.entered == 1 and spy.exited == 1  # MCP lifecycle handled once, in the service


def test_message_to_unknown_session_is_404() -> None:
    """Posting to a session that was never created is a clean 404, not a 500."""
    with _client(_FakeAgent()) as client:
        res = client.post("/sessions/nope/messages", json={"message": "hi"})
        assert res.status_code == 404
