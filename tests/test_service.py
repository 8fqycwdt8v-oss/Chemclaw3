"""The front-door HTTP surface runs a turn end-to-end with a fake agent (plan step F2-T1/F2-T2).

Exercises the real FastAPI app (health/readiness, session creation, the SSE message stream, the
static chat page) with an injected fake streaming agent — so the whole surface is proven without a
live model, MCP subprocess, or credentials. The MCP lifecycle is asserted to open/close exactly once
per turn via a spy tool.
"""

import json

from agent_framework import AgentSession
from fastapi.testclient import TestClient

from service.app import _LiveSessions, create_app


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
    """The browser chat surface is served at the root, with security headers, and still loads."""
    with _client(_FakeAgent()) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "Chemclaw" in res.text  # SEC-5: the CSP does not break the inline-styled UI
        # SEC-5: the browser security headers are present on the response.
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]
        assert "Strict-Transport-Security" in res.headers


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


def test_turn_is_shed_with_503_at_capacity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A turn that cannot get an admission permit within the timeout is shed with 503 (AG-15)."""
    import asyncio

    from chemclaw.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.05)
    app = create_app(agent_factory=lambda: _FakeAgent())
    # Zero permits → every turn is shed after the admission timeout (deterministic, no concurrency).
    app.state.turn_semaphore = asyncio.Semaphore(0)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
        assert res.status_code == 503


def test_permit_is_released_after_each_turn(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A finished turn returns its permit, so more turns than permits still all succeed (AG-15).

    Guards the subtle half of admission control — the `finally: semaphore.release()` in the SSE
    generator. With a single permit, three sequential turns can only all pass if each releases; a
    dropped release would silently collapse capacity (every later turn would 503 until restart).
    """
    import asyncio

    from chemclaw.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 1.0)
    app = create_app(agent_factory=lambda: _FakeAgent())
    app.state.turn_semaphore = asyncio.Semaphore(1)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        for _ in range(3):
            with client.stream(
                "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
            ) as res:
                assert res.status_code == 200
                for _line in res.iter_lines():  # drain the stream so the generator's finally runs
                    pass
    assert app.state.turn_semaphore._value == 1  # the permit is back, not leaked


def test_message_to_unknown_session_is_404() -> None:
    """Posting to a session that was never created is a clean 404, not a 500."""
    with _client(_FakeAgent()) as client:
        res = client.post("/sessions/nope/messages", json={"message": "hi"})
        assert res.status_code == 404


def test_oversized_message_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A message past the configured cap is a clean 422, not an unbounded read (SEC-4)."""
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "service_max_message_chars", 10)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "x" * 11})
        assert res.status_code == 422


def test_a_session_is_owner_scoped() -> None:
    """A user cannot post into or stream a session another user created (review finding)."""
    from service.auth import Principal, require_principal

    app = create_app(agent_factory=lambda: _FakeAgent())
    alice = Principal(oid="alice", upn="alice@corp", roles=frozenset())
    bob = Principal(oid="bob", upn="bob@corp", roles=frozenset())
    client = TestClient(app)

    app.dependency_overrides[require_principal] = lambda: alice
    session_id = client.post("/sessions").json()["session_id"]

    app.dependency_overrides[require_principal] = lambda: bob
    assert client.post(f"/sessions/{session_id}/messages", json={"message": "x"}).status_code == 404
    assert client.get(f"/sessions/{session_id}/events").status_code == 404  # not even existence

    app.dependency_overrides[require_principal] = lambda: alice
    ok = client.post(f"/sessions/{session_id}/messages", json={"message": "x"})
    assert ok.status_code == 200  # the owner still gets in


def test_job_pushback_streams_completed_events(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The events endpoint streams a finished job's push-back to the session (F3-T3)."""
    import service.app as app_module
    from agents.session_events import SessionEvent

    async def _fake_stream(session_id: str) -> object:
        yield SessionEvent(
            session_id=session_id,
            kind="job_completed",
            payload={"job_id": "qm-1", "converged": True},
        )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            assert res.status_code == 200
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    assert events == [
        {
            "type": "job_completed",
            "job_id": "qm-1",
            "summary": {"job_id": "qm-1", "converged": True},
        }
    ]


def test_job_pushback_flips_the_harness_awaiting_todo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A `job_completed` push-back flips the harness todo waiting on it (F3-T3 follow-up)."""
    import asyncio

    from agent_framework import DEFAULT_TODO_SOURCE_ID, TodoSessionStore

    import service.app as app_module
    from agents.harness_todo import mark_awaiting_job
    from agents.session_events import SessionEvent
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "harness_enabled", True)

    async def _fake_stream(session_id: str) -> object:
        yield SessionEvent(session_id=session_id, kind="job_completed", payload={"job_id": "qm-1"})

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    app = create_app(agent_factory=lambda: _FakeAgent())
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        live_session, _owner = app.state.live_sessions.get(session_id)
        asyncio.run(mark_awaiting_job(live_session, "qm-1", title="Await QM job qm-1"))

        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            assert res.status_code == 200
            for _line in res.iter_lines():  # drain so the handler actually runs
                pass

    items = asyncio.run(
        TodoSessionStore().load_items(live_session, source_id=DEFAULT_TODO_SOURCE_ID)
    )
    assert items[0].is_complete is True


def test_job_pushback_does_not_touch_todos_when_harness_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With the harness off, a push-back never touches the (harness-only) todo list."""
    import asyncio

    from agent_framework import DEFAULT_TODO_SOURCE_ID, TodoSessionStore

    import service.app as app_module
    from agents.harness_todo import mark_awaiting_job
    from agents.session_events import SessionEvent
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "harness_enabled", False)

    async def _fake_stream(session_id: str) -> object:
        yield SessionEvent(session_id=session_id, kind="job_completed", payload={"job_id": "qm-1"})

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    app = create_app(agent_factory=lambda: _FakeAgent())
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        live_session, _owner = app.state.live_sessions.get(session_id)
        asyncio.run(mark_awaiting_job(live_session, "qm-1", title="Await QM job qm-1"))

        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            for _line in res.iter_lines():
                pass

    items = asyncio.run(
        TodoSessionStore().load_items(live_session, source_id=DEFAULT_TODO_SOURCE_ID)
    )
    assert items[0].is_complete is False


def test_pushback_for_unknown_session_is_404() -> None:
    """Subscribing to push-back for a session that never existed is a clean 404."""
    with _client(_FakeAgent()) as client:
        assert client.get("/sessions/nope/events").status_code == 404


class _FakeOwnerStore:
    """In-memory stand-in for the durable session-ownership registry (no database)."""

    def __init__(self) -> None:
        self.owners: dict[str, str | None] = {}

    async def record(self, session_id: str, owner: str | None) -> None:
        self.owners.setdefault(session_id, owner)

    async def lookup(self, session_id: str) -> tuple[bool, str | None]:
        if session_id in self.owners:
            return (True, self.owners[session_id])
        return (False, None)


def test_session_rehydrates_after_a_restart() -> None:
    """A returning client reattaches to its session after the live cache is wiped (F3).

    Simulates the pod restart the front door previously could not survive: ownership persists, so a
    cache miss looks the owner up and rebuilds the live handle instead of forcing a new session.
    """
    from chemclaw.config import settings
    from service.auth import Principal, require_principal

    owners = _FakeOwnerStore()
    app = create_app(agent_factory=lambda: _FakeAgent(), owner_store=owners)
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="alice@corp", roles=frozenset()
    )
    client = TestClient(app)

    session_id = client.post("/sessions").json()["session_id"]
    assert session_id in owners.owners  # ownership persisted at creation

    # Restart: the in-process live-session cache is gone; the durable owner record survives.
    app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)
    assert app.state.live_sessions.get(session_id) is None

    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 200  # reattached, not a 404
    assert app.state.live_sessions.get(session_id) is not None  # re-registered in the cache


def test_rehydration_is_owner_scoped() -> None:
    """After a restart, a different user still cannot reattach to someone else's session (F3)."""
    from chemclaw.config import settings
    from service.auth import Principal, require_principal

    owners = _FakeOwnerStore()
    app = create_app(agent_factory=lambda: _FakeAgent(), owner_store=owners)
    client = TestClient(app)

    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    session_id = client.post("/sessions").json()["session_id"]
    app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)  # restart

    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="bob", upn="b@corp", roles=frozenset()
    )
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "x"})
    assert res.status_code == 404  # not the owner → no reattach, no existence leak


def test_no_rehydration_without_durable_store() -> None:
    """With no durable owner store (the in-memory session store), a cache miss stays a 404.

    The default path is unchanged: rehydration is gated on `session_store="postgres"`.
    """
    from chemclaw.config import settings

    app = create_app(agent_factory=lambda: _FakeAgent())  # owner_store None under the memory store
    assert app.state.session_owners is None
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)  # restart
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "x"})
        assert res.status_code == 404


def test_turn_is_refused_over_budget(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Once a session's turn budget is spent, the next turn is refused with 429 (budget #3)."""
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 1)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        # First turn runs to completion (draining the stream books it against the budget).
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
        ) as res:
            assert res.status_code == 200
            for _line in res.iter_lines():
                pass
        # Second turn exceeds the one-turn cap → refused before any streaming starts.
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "again"})
        assert res.status_code == 429


def test_budget_disabled_allows_many_turns(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With budgets off (the default), turn count is never capped (unchanged behavior)."""
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "budget_enabled", False)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 1)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        for _ in range(3):
            with client.stream(
                "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
            ) as res:
                assert res.status_code == 200
                for _line in res.iter_lines():
                    pass


def test_live_sessions_evicts_least_recently_used() -> None:
    """The bounded registry drops the LRU entry past capacity, keeping recent ones (COR-3)."""
    reg = _LiveSessions(capacity=2)
    reg.add("a", "sess-a", "owner-a")
    reg.add("b", "sess-b", "owner-b")
    # Touch "a" so "b" becomes the least-recently-used before the third insert.
    assert reg.get("a") == ("sess-a", "owner-a")
    reg.add("c", "sess-c", "owner-c")
    assert reg.get("b") is None  # evicted (LRU)
    assert reg.get("a") == ("sess-a", "owner-a")  # kept (recently used)
    assert reg.get("c") == ("sess-c", "owner-c")  # kept (newest)


def test_live_sessions_never_exceeds_capacity() -> None:
    """Adding far more sessions than the cap keeps the map bounded (no unbounded growth)."""
    reg = _LiveSessions(capacity=3)
    for i in range(100):
        reg.add(f"s{i}", f"sess-{i}", "owner")
    # Only the last 3 survive; the map never grew past the cap.
    assert reg.get("s99") is not None
    assert reg.get("s0") is None
    assert sum(reg.get(f"s{i}") is not None for i in range(100)) == 3
