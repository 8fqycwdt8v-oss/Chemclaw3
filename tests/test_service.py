"""The front-door HTTP surface runs a turn end-to-end with a fake agent (plan step F2-T1/F2-T2).

Exercises the real FastAPI app (health/readiness, session creation, the SSE message stream, the
static chat page) with an injected fake streaming agent — so the whole surface is proven without a
live model, MCP subprocess, or credentials. The MCP lifecycle is asserted to open/close exactly once
per turn via a spy tool.
"""

import asyncio
import json
from collections.abc import Callable, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agent_framework import AgentSession
from fastapi.testclient import TestClient

from chemclaw.api.app import LiveSession, _LiveSessions, create_app
from chemclaw.api.metrics import METRICS

# A minimal ASGI HTTP scope, for the one test that drives the app below `TestClient` (which
# cannot express "the handler was cancelled and nothing was ever sent").
_ASGI_GET_SCOPE: dict[str, Any] = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.1"},
    "http_version": "1.1",
    "method": "GET",
    "scheme": "http",
    "path": "/drained",
    "raw_path": b"/drained",
    "query_string": b"",
    "root_path": "",
    "headers": [(b"host", b"testserver")],
    "client": ("127.0.0.1", 1234),
    "server": ("testserver", 80),
}


class _SpyMcpTool:
    """An async-context-manager stand-in for a connector tool that records connect/teardown.

    `is_connected` is what `open_reachable` reads to report which connectors came up, and MAF reads
    it too — a spy that omitted it would look permanently unreachable.
    """

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.is_connected = False
        self.name = "spy"

    async def __aenter__(self) -> "_SpyMcpTool":
        self.entered += 1
        self.is_connected = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1


class _Update:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.contents: list[object] = []
        self.user_input_requests: list[object] = []


class _FakeAgent:
    """Fake agent: yields two tokens per turn. Connectors are the front door's business, not its."""

    def __init__(self) -> None:
        self.mcp_tools: list[object] = []

    def create_session(self, *, session_id: str) -> AgentSession:
        return AgentSession(session_id=session_id)

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> object:
        async def _gen() -> object:
            yield _Update(text="hi ")
            yield _Update(text="there")

        return _gen()


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """The default connector factory for these tests: none.

    Most tests here are not about connectors, and defaulting to the real set would have every one of
    them dial a connector server that is not running.
    """
    return []


def _client(
    agent: _FakeAgent,
    connector_factory: Callable[[str | None], list[Any]] = _no_connectors,
) -> TestClient:
    """The app under test, with no connectors by default (see `_no_connectors`)."""
    return TestClient(
        create_app(agent_factory=lambda _profile: agent, connector_factory=connector_factory)
    )


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


def test_security_headers_reach_a_streaming_sse_response() -> None:
    """The SSE turn stream carries the same headers as a static page.

    The static-page test above passes under *any* middleware implementation; a streamed
    `EventSourceResponse` is the case that distinguishes them, because it is the response whose
    headers are sent long before its body exists.
    """
    agent = _FakeAgent()
    with _client(agent) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hello"}
        ) as res:
            assert res.status_code == 200
            assert res.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]


def test_a_cancelled_request_closes_the_connection_instead_of_500ing() -> None:
    """A handler cancelled before it responds must not be turned into a 500 with a traceback.

    This is the multi-worker blocker, and it is not hypothetical: a 50-user load run logged 44
    `RuntimeError("No response returned.")` tracebacks, every one on the SSE turn route, each
    served to a chemist as an HTTP 500. `BaseHTTPMiddleware` produced them — it runs the app in a
    second task and pipes its ASGI messages through a memory stream, so a handler that ends
    without responding (a pod draining mid-stream, a client that gave up waiting for an
    admission permit) reaches `call_next` as a closed stream and is re-raised as a server error.

    Driven at the raw ASGI level rather than through `TestClient`, because the distinction *is*
    the ASGI contract: cancellation must propagate out of the app (the server then simply closes
    the connection) rather than being converted into a response. Counterfactual: with the old
    `BaseHTTPMiddleware` this raises `RuntimeError`, not `CancelledError`.
    """
    app = create_app(agent_factory=lambda _profile: _FakeAgent(), connector_factory=_no_connectors)

    @app.get("/drained")
    async def drained() -> dict[str, str]:
        """Stand in for a handler the server cancels mid-request."""
        raise asyncio.CancelledError

    # The static UI is mounted at "/" and would otherwise swallow the path, since Starlette
    # matches routes in registration order.
    app.router.routes.insert(0, app.router.routes.pop())

    sent: list[MutableMapping[str, Any]] = []

    async def _send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    async def _receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _drive() -> None:
        with pytest.raises(asyncio.CancelledError):
            await app(_ASGI_GET_SCOPE, _receive, _send)

    asyncio.run(_drive())
    assert sent == [], f"a cancelled handler still emitted a response: {sent}"


def test_message_stream_runs_a_turn_and_connects_its_connectors_once() -> None:
    """Create a session, post a message, stream the turn; the turn's connector opens once."""
    agent = _FakeAgent()
    spy = _SpyMcpTool()
    with _client(agent, connector_factory=lambda _profile: [spy]) as client:
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
    # The connector lifecycle is the service's, and it is per *turn*: one connect and one teardown
    # for this turn, from the factory the app calls each time (not a set held on the agent, which
    # would be shared across concurrent turns — see `agents.chemclaw_agent.connector_tools`).
    assert spy.entered == 1 and spy.exited == 1


def test_a_launched_job_reaches_the_browser_as_an_sse_event() -> None:
    """A job announced by a tool is serialized into the turn's SSE stream, before the answer.

    The end-to-end half of D-042: without it the chemist saw nothing between their message and
    the answer, with the first sign of the job arriving only as the completion push-back.
    """
    from chemclaw.agent.turn_signals import record_job_started

    class _JobAgent(_FakeAgent):
        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> object:
            async def _gen() -> object:
                record_job_started("qm-sse", "report")
                yield _Update(text="submitted")

            return _gen()

    with _client(_JobAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": "go"}) as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    # Order is chronological: the fake announces the job *before* yielding its text, and the
    # consolidated sink (agents.turn_signals) drains at the top of each update for exactly that
    # reason — a tool that ran while the model was producing an update ran before the text it then
    # produced. main's original assertion had token-first, which reported the text ahead of the job
    # that preceded it; the property this test names ("before the answer") holds either way.
    assert [e["type"] for e in events] == ["job_started", "token", "answer"]
    assert events[0]["job_id"] == "qm-sse"


def test_turn_is_shed_with_503_at_capacity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A turn that cannot get an admission permit within the timeout is shed with 503 (AG-15)."""
    import asyncio

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.05)
    app = create_app(agent_factory=lambda _profile: _FakeAgent())
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

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 1.0)
    app = create_app(agent_factory=lambda _profile: _FakeAgent())
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
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_max_message_chars", 10)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "x" * 11})
        assert res.status_code == 422


def test_a_session_is_owner_scoped() -> None:
    """A user cannot post into or stream a session another user created (review finding)."""
    from chemclaw.api.auth import Principal, require_principal

    app = create_app(agent_factory=lambda _profile: _FakeAgent())
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
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent

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

    import chemclaw.api.app as app_module
    from chemclaw.agent.harness_todo import mark_awaiting_job
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "harness_enabled", True)

    async def _fake_stream(session_id: str, **_: object) -> object:
        yield SessionEvent(session_id=session_id, kind="job_completed", payload={"job_id": "qm-1"})

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    app = create_app(agent_factory=lambda _profile: _FakeAgent())
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        live_session = app.state.live_sessions.get(session_id).session
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

    import chemclaw.api.app as app_module
    from chemclaw.agent.harness_todo import mark_awaiting_job
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "harness_enabled", False)

    async def _fake_stream(session_id: str, **_: object) -> object:
        yield SessionEvent(session_id=session_id, kind="job_completed", payload={"job_id": "qm-1"})

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    app = create_app(agent_factory=lambda _profile: _FakeAgent())
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        live_session = app.state.live_sessions.get(session_id).session
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
        # Stored beside the owner, as the real table stores it, so a rehydration test can see the
        # profile survive an eviction rather than a fake supplying what the column would (REV-14).
        self.profiles: dict[str, str | None] = {}
        self.created: dict[str, datetime] = {}

    async def record(self, session_id: str, owner: str | None, profile: str | None = None) -> None:
        if session_id not in self.owners:
            self.owners[session_id] = owner
            self.profiles[session_id] = profile
            # Distinct, increasing timestamps so "newest first" is actually observable.
            self.created[session_id] = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
                minutes=len(self.created)
            )

    async def lookup(self, session_id: str) -> tuple[bool, str | None, str | None]:
        if session_id in self.owners:
            return (True, self.owners[session_id], self.profiles[session_id])
        return (False, None, None)

    async def list_for_owner(self, owner: str | None) -> list[tuple[str, datetime]]:
        rows = [(sid, self.created[sid]) for sid, own in self.owners.items() if own == owner]
        return sorted(rows, key=lambda row: row[1], reverse=True)


class _SharedTurnClaims:
    """The `session_turns` row, in memory — one instance stands in for the shared database.

    Two apps built over one of these are the faithful model of two uvicorn workers or two pods:
    separate processes, separate `active_turns` sets, one durable claim between them.
    """

    def __init__(self) -> None:
        self.holders: dict[str, str] = {}

    async def claim(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        if session_id in self.holders:
            return False
        self.holders[session_id] = holder
        return True

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> None:
        pass  # nothing elapses inside a test, so a refresh has nothing to do

    async def release(self, session_id: str, holder: str) -> None:
        if self.holders.get(session_id) == holder:
            del self.holders[session_id]


def test_a_turn_running_on_another_worker_is_a_409_not_a_second_turn() -> None:
    """A turn already claimed by another process is refused here, not admitted a second time.

    The 409 guard was a `set` in one process's memory while the shipped chart runs the front door
    at `minReplicas: 2`, so a double-submit that landed on the other replica was admitted and the
    two turns interleaved their messages into one conversation thread — the exact corruption the
    guard exists to prevent. The claim row is the only trace of the sibling process this one can
    see, so seeding it *is* the other worker, faithfully: nothing else about that turn is
    observable from here.

    Counterfactual: with only the per-process set this process has no record of the session's
    running turn and answers 200.
    """
    claims = _SharedTurnClaims()
    app = create_app(
        agent_factory=lambda _profile: _FakeAgent(),
        owner_store=_FakeOwnerStore(),
        connector_factory=_no_connectors,
        turn_claims=claims,
    )
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        claims.holders[session_id] = "another-worker"  # a turn is in flight over there
        conflict = client.post(f"/sessions/{session_id}/messages", json={"message": "second"})

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "a turn is already running for this session"
    assert claims.holders == {session_id: "another-worker"}  # the refusal did not steal the slot


def test_a_finished_turn_hands_its_cross_process_claim_back() -> None:
    """The slot is taken for a turn's streamed run and given back when it ends, not leaked.

    A claim that outlived its turn would 409 the session for a whole lease every time — the
    durable version of the bug that once bricked a session's turns until the pod restarted.
    """
    claims = _SharedTurnClaims()
    app = create_app(
        agent_factory=lambda _profile: _FakeAgent(),
        owner_store=_FakeOwnerStore(),
        connector_factory=_no_connectors,
        turn_claims=claims,
    )
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        for _ in range(2):  # a second turn proves the first genuinely released
            with client.stream(
                "POST", f"/sessions/{session_id}/messages", json={"message": "hello"}
            ) as res:
                assert res.status_code == 200
                for _line in res.iter_lines():
                    pass
            assert claims.holders == {}


class _UnreachableOwnerStore(_FakeOwnerStore):
    """An ownership registry whose every call fails the way a starved pool checkout does.

    `chemclaw.core.db.connection` maps both `PoolTimeout` and an unreachable server to
    `ConnectionError`, so this is exactly what a route sees when no pooled connection can be
    handed over in time.
    """

    async def record(self, session_id: str, owner: str | None, profile: str | None = None) -> None:
        raise ConnectionError("Postgres unreachable at host=db: couldn't get a connection")


def test_a_failed_postgres_checkout_sheds_with_503_and_is_counted() -> None:
    """Creating a session when no connection can be got is a retryable 503, never a 500.

    `create_session` writes the owner row before it returns an id, and under load 16 of those
    writes raised `psycopg_pool.PoolTimeout` with no handler anywhere — HTTP 500, which tells a
    client the request is broken and must not be retried. It is the opposite: the pool held 13 of
    a permitted 64 connections and opened none, so the caller was waiting for a connection that
    was free, and retrying is precisely the right move.

    Counterfactual: without the `ConnectionError` handler this call raises out of the app and
    `TestClient` re-raises it (a 500 in production), and the counter stays at 0.
    """
    before = METRICS.value("chemclaw_db_unavailable_total")
    app = create_app(
        agent_factory=lambda _profile: _FakeAgent(),
        owner_store=_UnreachableOwnerStore(),
        connector_factory=_no_connectors,
    )
    with TestClient(app) as client:
        res = client.post("/sessions")
    assert res.status_code == 503
    assert res.json()["detail"] == "server at capacity; retry shortly"
    assert METRICS.value("chemclaw_db_unavailable_total") == before + 1


def test_session_list_is_owner_scoped_and_newest_first() -> None:
    """`GET /sessions` returns the caller's own sessions, newest first — and nobody else's.

    The list is how a client that lost its local state finds sessions it still owns; ids are
    minted server-side, so one it forgot is otherwise unreachable while its history sits in the
    store. Scoping is the security half: a session id is a capability, and listing someone else's
    would hand it out.
    """
    from chemclaw.api.auth import Principal, require_principal

    alice = Principal(oid="alice", upn="a@corp", roles=frozenset())
    bob = Principal(oid="bob", upn="b@corp", roles=frozenset())
    app = create_app(agent_factory=lambda _profile: _FakeAgent(), owner_store=_FakeOwnerStore())
    client = TestClient(app)

    app.dependency_overrides[require_principal] = lambda: alice
    first = client.post("/sessions").json()["session_id"]
    second = client.post("/sessions").json()["session_id"]
    app.dependency_overrides[require_principal] = lambda: bob
    bobs = client.post("/sessions").json()["session_id"]

    app.dependency_overrides[require_principal] = lambda: alice
    listed = [row["session_id"] for row in client.get("/sessions").json()]
    assert listed == [second, first]  # newest first
    assert bobs not in listed

    app.dependency_overrides[require_principal] = lambda: bob
    assert [row["session_id"] for row in client.get("/sessions").json()] == [bobs]


def test_session_list_is_empty_without_a_durable_registry() -> None:
    """Under the in-memory store there is no durable registry, so the list is honestly empty.

    Reporting the process's live LRU instead would answer a question about the deployment with an
    eviction-dependent guess that a pod restart silently changes.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = create_app(
        agent_factory=lambda _profile: _FakeAgent()
    )  # owner_store None under the memory store
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    client.post("/sessions")
    assert app.state.session_owners is None
    assert client.get("/sessions").json() == []


def test_transcript_reads_back_the_stored_thread() -> None:
    """`GET /sessions/{id}/messages` returns the session's stored thread, so a reload restores it.

    History is seeded through `app.state.history` — the very provider a real turn stores through —
    rather than by running the fake agent, which yields updates without persisting anything. That
    keeps the test on the route's own behavior (ownership gate, ordering, MAF-shape flattening)
    instead of re-implementing MAF's storage in a fake and asserting the fake.
    """
    from agent_framework import Message

    from chemclaw.api.auth import Principal, require_principal

    app = create_app(agent_factory=lambda _profile: _FakeAgent())
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]
    assert client.get(f"/sessions/{session_id}/messages").json() == []  # nothing said yet

    session = app.state.live_sessions.get(session_id).session
    asyncio.run(
        app.state.history.save_messages(
            session_id,
            [Message("user", ["hello"]), Message("assistant", ["hi there"])],
            state=session.state,
        )
    )

    transcript = client.get(f"/sessions/{session_id}/messages").json()
    assert [row["role"] for row in transcript] == ["user", "assistant"]
    assert transcript[0]["text"] == "hello"
    assert transcript[1]["text"] == "hi there"


def test_transcript_of_an_unknown_session_is_404() -> None:
    """An id nobody owns is a 404, same as every other session-scoped route."""
    client = _client(_FakeAgent())
    assert client.get("/sessions/nope/messages").status_code == 404


def test_session_rehydrates_after_a_restart() -> None:
    """A returning client reattaches to its session after the live cache is wiped (F3).

    Simulates the pod restart the front door previously could not survive: ownership persists, so a
    cache miss looks the owner up and rebuilds the live handle instead of forcing a new session.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    owners = _FakeOwnerStore()
    app = create_app(agent_factory=lambda _profile: _FakeAgent(), owner_store=owners)
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
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    owners = _FakeOwnerStore()
    app = create_app(agent_factory=lambda _profile: _FakeAgent(), owner_store=owners)
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
    from chemclaw.core.config import settings

    app = create_app(
        agent_factory=lambda _profile: _FakeAgent()
    )  # owner_store None under the memory store
    assert app.state.session_owners is None
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)  # restart
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "x"})
        assert res.status_code == 404


def test_turn_is_refused_over_budget(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Once a session's turn budget is spent, the next turn is refused with 429 (budget #3)."""
    from chemclaw.core.config import settings

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
    from chemclaw.core.config import settings

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
    assert reg.get("a") == LiveSession(session="sess-a", owner="owner-a")
    reg.add("c", "sess-c", "owner-c")
    assert reg.get("b") is None  # evicted (LRU)
    assert reg.get("a") == LiveSession(session="sess-a", owner="owner-a")  # kept (recently used)
    assert reg.get("c") == LiveSession(session="sess-c", owner="owner-c")  # kept (newest)


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
) -> Callable[[str | None], _FakeAgent]:
    """An agent factory whose turn for `blocked_message` parks on `gate` (concurrency tests).

    The sync TestClient runs each request to completion before returning, so it cannot hold one
    turn open while another is issued — these tests drive the app over httpx's ASGI transport on
    a real event loop instead, with `started`/`gate` sequencing the overlap deterministically.
    """

    class _GatedAgent(_FakeAgent):
        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> object:
            async def _gen() -> object:
                if message == blocked_message:
                    started.set()
                    await gate.wait()
                yield _Update(text="done")

            return _gen()

    return lambda _profile: _GatedAgent()


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
    from chemclaw.core.config import settings

    class _HungAgent(_FakeAgent):
        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> object:
            async def _gen() -> object:
                import asyncio

                yield _Update(text="partial")
                await asyncio.sleep(60)  # a hung LLM endpoint: never yields again
                yield _Update(text="never")

            return _gen()

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.2)
    app = create_app(agent_factory=lambda _profile: _HungAgent())
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


def test_cancelled_admission_wait_does_not_brick_the_session(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A request cancelled while waiting for a turn permit frees the session's turn slot.

    CancelledError is a BaseException: an `except Exception` cleanup missed it, so a client
    disconnecting mid-admission leaked the active-turns entry — every later POST to that
    session answered 409 until restart. The slot must be released on *any* pre-handoff exit,
    and the session must accept a turn again once capacity exists.
    """
    import contextlib

    import httpx

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 30.0)

    async def _run() -> None:
        app = create_app(agent_factory=lambda _profile: _FakeAgent())
        # Zero permits: the turn parks on the semaphore *after* taking the session's slot.
        app.state.turn_semaphore = asyncio.Semaphore(0)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            waiting = asyncio.create_task(
                client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
            )
            async with asyncio.timeout(5):
                while session_id not in app.state.active_turns:  # parked mid-admission
                    await asyncio.sleep(0.01)
            waiting.cancel()  # the client goes away between add and handoff
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await waiting

            assert session_id not in app.state.active_turns  # the slot is freed, not leaked
            app.state.turn_semaphore.release()  # capacity returns...
            res = await client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
            assert res.status_code == 200  # ...and the session is usable, not 409-bricked

    asyncio.run(_run())


def test_event_streams_are_capped_per_user(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Past the per-user cap, another push-back stream is refused with 429 (DB-load guard).

    Each stream polls the database for its whole lifetime; unbounded streams from one user are
    a connection-exhaustion vector against the shared session store.
    """
    import contextlib

    import httpx

    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.core.config import settings

    async def _idle_stream(session_id: str, **_: object) -> object:
        while True:  # holds the stream open without ever delivering
            await asyncio.sleep(3600)
            yield SessionEvent(session_id=session_id, kind="job_completed", payload={})

    monkeypatch.setattr(app_module, "stream_new_events", _idle_stream)
    monkeypatch.setattr(settings, "service_max_event_streams_per_user", 1)

    async def _run() -> None:
        app = create_app(agent_factory=lambda _profile: _FakeAgent())
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
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent

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

    from chemclaw.api.auth import Principal, require_principal

    app = create_app(agent_factory=lambda _profile: _FakeAgent())
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
        ("/sessions/{session_id}/messages", "GET"),
        ("/sessions/{session_id}/events", "GET"),
        ("/sessions/{session_id}/attachments", "POST"),
        # The pre-execution approval gate (REV-1, D-137). Both must be owner-scoped: reading a
        # plan leaks what another chemist is doing, and deciding on one would let a stranger
        # authorize it.
        ("/sessions/{session_id}/plan", "GET"),
        ("/sessions/{session_id}/plan/decision", "POST"),
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
            # The upload route takes multipart, the others JSON; send whichever the route expects so
            # a 404 here proves the *ownership* gate rather than a body-parsing rejection.
            if url.endswith("/attachments"):
                res = client.request(method, url, files={"file": ("a.txt", b"x", "text/plain")})
            elif url.endswith("/plan/decision"):
                res = client.request(method, url, json={"approved": True, "plan_hash": "x"})
            else:
                res = client.request(method, url, json={"message": "x"})
            assert res.status_code == 404, (
                f"{method} {route.path} answered {res.status_code} for a non-owner — "
                "it must resolve ownership (404, no existence leak) before doing anything"
            )
