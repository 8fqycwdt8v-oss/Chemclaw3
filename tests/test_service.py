"""The front-door HTTP surface runs a turn end-to-end with a fake agent (plan step F2-T1/F2-T2).

Exercises the real FastAPI app (health/readiness, session creation, the SSE message stream, the
static chat page) with an injected fake streaming agent — so the whole surface is proven without a
live model, MCP subprocess, or credentials. The MCP lifecycle is asserted to open/close exactly once
per turn via a spy tool.
"""

import asyncio
import json
from collections.abc import Callable

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

    async def _fake_stream(session_id: str, **_: object) -> object:
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

    async def _fake_stream(session_id: str, **_: object) -> object:
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

    async def _fake_stream(session_id: str, **_: object) -> object:
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


def _gated_agent_factory(
    gate: asyncio.Event, started: asyncio.Event, blocked_message: str
) -> Callable[[], _FakeAgent]:
    """An agent factory whose turn for `blocked_message` parks on `gate` (concurrency tests).

    The sync TestClient runs each request to completion before returning, so it cannot hold one
    turn open while another is issued — these tests drive the app over httpx's ASGI transport on
    a real event loop instead, with `started`/`gate` sequencing the overlap deterministically.
    """

    class _GatedAgent(_FakeAgent):
        def run(self, message: str, *, stream: bool, session: AgentSession) -> object:
            async def _gen() -> object:
                if message == blocked_message:
                    started.set()
                    await gate.wait()
                yield _Update(text="done")

            return _gen()

    return lambda: _GatedAgent()


def test_concurrent_turn_on_same_session_is_409() -> None:
    """While one turn runs, a second POST to the same session is rejected with 409.

    Two concurrent turns would drive `agent.run` against the same AgentSession at once,
    interleaving two turns' messages into one conversation thread — so the second is shed
    (matching the admission semaphore's shed-don't-queue semantics), and the slot frees when
    the running turn's stream ends.
    """
    import httpx

    async def _run() -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        app = create_app(agent_factory=_gated_agent_factory(gate, started, "first"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            first = asyncio.create_task(
                client.post(f"/sessions/{session_id}/messages", json={"message": "first"})
            )
            await asyncio.wait_for(started.wait(), timeout=5)  # the first turn is mid-run
            dup = await client.post(f"/sessions/{session_id}/messages", json={"message": "second"})
            assert dup.status_code == 409
            gate.set()
            assert (await first).status_code == 200
            # The slot is released with the stream — the next turn is admitted again.
            ok = await client.post(f"/sessions/{session_id}/messages", json={"message": "third"})
            assert ok.status_code == 200

    asyncio.run(_run())


def test_concurrent_turns_on_different_sessions_are_admitted() -> None:
    """The per-session gate is per session: a turn on another session is not blocked."""
    import httpx

    async def _run() -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        app = create_app(agent_factory=_gated_agent_factory(gate, started, "blocked"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = (await client.post("/sessions")).json()["session_id"]
            second = (await client.post("/sessions")).json()["session_id"]
            blocked = asyncio.create_task(
                client.post(f"/sessions/{first}/messages", json={"message": "blocked"})
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            other = await client.post(f"/sessions/{second}/messages", json={"message": "b"})
            assert other.status_code == 200  # a different session's turn runs concurrently
            gate.set()
            assert (await blocked).status_code == 200

    asyncio.run(_run())


def test_stalled_turn_times_out_and_frees_the_permit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A turn past the wall-clock bound ends with one error event and releases its permit.

    Without this, a hung model stream would hold one of the few admission permits forever; a
    handful of stalls would collapse the front door (every turn shed 503) until restart.
    """
    from chemclaw.config import settings

    class _HungAgent(_FakeAgent):
        def run(self, message: str, *, stream: bool, session: AgentSession) -> object:
            async def _gen() -> object:
                import asyncio

                yield _Update(text="partial")
                await asyncio.sleep(60)  # a hung LLM endpoint: never yields again
                yield _Update(text="never")

            return _gen()

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.2)
    app = create_app(agent_factory=lambda: _HungAgent())
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
        ) as res:
            assert res.status_code == 200
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))
    assert events[-1]["type"] == "error"
    assert "time limit" in events[-1]["message"]
    # The permit and the session's turn slot are both released — capacity is not pinned.
    assert app.state.turn_semaphore._value == settings.service_max_concurrent_turns
    assert session_id not in app.state.active_turns


def test_event_streams_are_capped_per_user(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Past the per-user cap, another push-back stream is refused with 429 (DB-load guard).

    Each stream polls the database for its whole lifetime; unbounded streams from one user are
    a connection-exhaustion vector against the shared session store.
    """
    import contextlib

    import httpx

    import service.app as app_module
    from agents.session_events import SessionEvent
    from chemclaw.config import settings

    async def _idle_stream(session_id: str, **_: object) -> object:
        while True:  # holds the stream open without ever delivering
            await asyncio.sleep(3600)
            yield SessionEvent(session_id=session_id, kind="job_completed", payload={})

    monkeypatch.setattr(app_module, "stream_new_events", _idle_stream)
    monkeypatch.setattr(settings, "service_max_event_streams_per_user", 1)

    async def _run() -> None:
        app = create_app(agent_factory=lambda: _FakeAgent())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            first = asyncio.create_task(client.get(f"/sessions/{session_id}/events"))
            async with asyncio.timeout(5):
                while not app.state.event_streams:  # the first stream is admitted and counted
                    await asyncio.sleep(0.01)
            second = await client.get(f"/sessions/{session_id}/events")
            assert second.status_code == 429  # the per-user cap binds
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await first
            async with asyncio.timeout(5):
                while app.state.event_streams:  # closing the stream freed the user's slot
                    await asyncio.sleep(0.01)

    asyncio.run(_run())


def test_events_route_claims_only_job_completed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The push-back route scopes its (destructive) claim to `job_completed` in the claim itself.

    The claim marks rows consumed atomically; claiming every kind and filtering afterwards would
    silently destroy events of other kinds meant for other consumers.
    """
    import service.app as app_module
    from agents.session_events import SessionEvent

    captured: dict[str, object] = {}

    async def _fake_stream(session_id: str, **kwargs: object) -> object:
        captured.update(kwargs)
        yield SessionEvent(session_id=session_id, kind="job_completed", payload={"job_id": "j1"})

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            for _line in res.iter_lines():
                pass
    assert captured["kinds"] == ("job_completed",)


def test_every_session_scoped_route_is_ownership_gated() -> None:
    """Every route carrying a session id resolves ownership — a non-owner gets 404 on all of them.

    Enumerates the app's routes rather than hardcoding today's two, so a future session-scoped
    route that skips the `_resolve_session` gate fails here: the inventory assertion forces a
    conscious update, and the behavioral sweep then proves the new route 404s for a non-owner.
    """
    from fastapi.routing import APIRoute

    from service.auth import Principal, require_principal

    app = create_app(agent_factory=lambda: _FakeAgent())
    session_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and "{session_id}" in route.path
    ]
    inventory = {
        (route.path, method)
        for route in session_routes
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
    }
    assert inventory == {
        ("/sessions/{session_id}/messages", "POST"),
        ("/sessions/{session_id}/events", "GET"),
    }, (
        "new session-scoped route detected — it MUST resolve ownership via _resolve_session, "
        "and this inventory + the non-owner sweep below must cover it"
    )

    alice = Principal(oid="alice", upn="a@corp", roles=frozenset())
    bob = Principal(oid="bob", upn="b@corp", roles=frozenset())
    client = TestClient(app)
    app.dependency_overrides[require_principal] = lambda: alice
    session_id = client.post("/sessions").json()["session_id"]

    app.dependency_overrides[require_principal] = lambda: bob
    for route in session_routes:
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}:
            url = route.path.format(session_id=session_id)
            res = client.request(method, url, json={"message": "x"})
            assert res.status_code == 404, (
                f"{method} {route.path} answered {res.status_code} for a non-owner — "
                "it must resolve ownership (404, no existence leak) before doing anything"
            )
