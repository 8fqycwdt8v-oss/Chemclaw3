"""The front-door HTTP surface runs a turn end-to-end with a fake agent (plan step F2-T1/F2-T2).

Exercises the real FastAPI app (health/readiness, session creation, the SSE message stream, the
static chat page) with an injected fake streaming agent — so the whole surface is proven without a
live model, MCP subprocess, or credentials. The MCP lifecycle is asserted to open/close exactly once
per turn via a spy tool.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chemclaw.agent.session import TurnSession
from chemclaw.api.app import LiveSession, _LiveSessions, create_app
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.fakes import asgi_client
from tests.fakes_turn import Piece, ScriptedTurn

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


class _FakeAgent(ScriptedTurn):
    """Fake agent: yields two tokens per turn. Connectors are the front door's business, not its."""

    def create_session(self, *, session_id: str) -> TurnSession:
        """The one non-streaming method the front door calls on an agent."""
        return TurnSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield "hi "
        yield "there"


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """The default connector factory for these tests: none.

    Most tests here are not about connectors, and defaulting to the real set would have every one of
    them dial a connector server that is not running.
    """
    return []


def _app(agent: ScriptedTurn | None = None, **kwargs: Any) -> FastAPI:
    """The app under test, wired to one fake through the seam a turn is driven by.

    `graph_factory` is how the fake gets in (see `tests.fakes_turn.ScriptedTurn`), so a test never
    needs a model credential and never builds a graph of its own. Connectors default to none for
    the reason `_no_connectors` records; a test that does want one passes a spec
    (`tests.test_capability_degradation._dark_connector`).
    """
    fake = agent if agent is not None else _FakeAgent()
    kwargs.setdefault("connector_factory", _no_connectors)
    return create_app(graph_factory=fake.graph_factory, **kwargs)


def _client(
    agent: _FakeAgent,
    connector_factory: Callable[[str | None], list[Any]] = _no_connectors,
) -> TestClient:
    """The app under test as a `TestClient`, with no connectors by default."""
    return TestClient(_app(agent, connector_factory=connector_factory))


def test_healthz_is_ok() -> None:
    """Liveness needs no agent and returns 200."""
    with _client(_FakeAgent()) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def _unreachable_database(*_args: Any, **_kwargs: Any) -> Any:
    """A `db.connection` replacement that fails the way an unreachable server does."""
    raise ConnectionError("Postgres unreachable at host=db: connection refused")


def test_readyz_reports_unready_when_the_store_it_needs_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under `session_store="postgres"` a pod that cannot reach Postgres cannot serve a turn.

    It reported itself ready anyway until the 2026-08-05 database review: the route probed every
    enabled connector — each of which costs the agent one capability — and never the store the
    session claim, the conversation history, the owner lookup and the audit sink all go through
    (D-2026-08-05-readiness-answers-for-the-store-it-cannot-serve-without).
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _unreachable_database)
    with _client(_FakeAgent()) as client:
        res = client.get("/readyz")
    assert res.status_code == 503
    assert res.json()["status"] == "database unreachable"


def test_a_database_outage_drains_the_pod_without_restarting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Liveness must not follow readiness here, or an outage becomes a fleet-wide crash loop.

    Restarting every front-door pod because a shared database is down destroys the capacity that
    would serve the moment it returns, and a restarted pod is no closer to reaching it. Draining
    them from the Route is the whole of the correct response.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _unreachable_database)
    with _client(_FakeAgent()) as client:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200


def test_readyz_does_not_probe_a_database_a_memory_deployment_does_not_have(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`session_store="memory"` has no store to answer for, so there is nothing to probe.

    The probe would otherwise report every dev run and every CLI-shaped deployment unready for
    lacking a database none of them use.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)

    def _must_not_be_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a memory-store deployment probed the database")

    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _must_not_be_called)
    with _client(_FakeAgent()) as client:
        res = client.get("/readyz")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"


def test_readyz_reuses_its_database_verdict_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unauthenticated route probed every ten seconds must not be a database fan-out on demand.

    The same cache the connector sweep uses, for the same reason: any caller can hit this route at
    will, so an uncached probe is one round trip per request against the store.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 60.0)
    probes = 0

    @asynccontextmanager
    async def _counting_connection(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        nonlocal probes
        probes += 1

        class _Conn:
            async def execute(self, _sql: str) -> None:
                return None

        yield _Conn()

    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _counting_connection)
    with _client(_FakeAgent()) as client:
        for _ in range(5):
            assert client.get("/readyz").status_code == 200
    assert probes == 1


def test_concurrent_readiness_probes_cost_one_connector_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fifty simultaneous `/readyz` probes must cost one sweep, not fifty.

    The cache is a check-then-act with an `await` between the check and the write, so the window
    only ever suppressed *sequential* repeats: under concurrency every in-flight request misses.
    Measured on the real app, 50 concurrent probes inside one 5 s window did 50 full connector
    fan-outs. `/readyz` is unauthenticated by necessity and therefore also outside
    `require_principal`'s per-principal budget, so the amplification factor is chosen by the
    caller — which is what makes one unauthenticated TCP connection worth N outbound connections
    to the connector fleet.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 60.0)
    sweeps = 0

    async def _counting_probe() -> list[Any]:
        nonlocal sweeps
        sweeps += 1
        # A suspension point, because the real sweep is an HTTP fan-out: without one there is no
        # window for a second caller to miss the cache, and the test would pass against the defect.
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr("chemclaw.api.app.probe_connectors", _counting_probe)

    async def _run() -> None:
        app = _app()
        async with asgi_client(app) as client:
            responses = await asyncio.gather(*(client.get("/readyz") for _ in range(50)))
        assert {res.status_code for res in responses} == {200}

    asyncio.run(_run())
    assert sweeps == 1, f"50 concurrent probes triggered {sweeps} connector sweeps"


def test_concurrent_readiness_probes_cost_one_database_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same, for the probe that borrows from a 16-connection pool.

    Under `session_store="postgres"` (what the chart ships) each miss checks out a pooled
    connection, so 50 concurrent probes requested 50 checkouts against `pg_pool_max_size=16` —
    and every authenticated request needing the store in that window waits behind them and, past
    `pg_pool_timeout_seconds`, is shed 503.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 60.0)
    monkeypatch.setattr("chemclaw.api.app.probe_connectors", _no_probe)
    checkouts = 0

    @asynccontextmanager
    async def _counting_connection(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        nonlocal checkouts
        checkouts += 1
        await asyncio.sleep(0.05)

        class _Conn:
            async def execute(self, _sql: str) -> None:
                return None

        yield _Conn()

    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _counting_connection)

    async def _run() -> None:
        app = _app()
        async with asgi_client(app) as client:
            responses = await asyncio.gather(*(client.get("/readyz") for _ in range(50)))
        assert {res.status_code for res in responses} == {200}

    asyncio.run(_run())
    assert checkouts == 1, f"50 concurrent probes requested {checkouts} pooled connections"


async def _no_probe() -> list[Any]:
    """A connector sweep that answers instantly, for the tests that are about the other probe."""
    return []


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
    app = _app()

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


def test_a_launched_job_reaches_the_browser_as_an_sse_event() -> None:
    """A job announced by a tool is serialized into the turn's SSE stream, before the answer.

    The end-to-end half of D-042: without it the chemist saw nothing between their message and
    the answer, with the first sign of the job arriving only as the completion push-back.
    """
    from chemclaw.core.turn_signals import record_job_started

    class _JobAgent(_FakeAgent):
        """A fake whose turn announces a durable job before it says anything."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            record_job_started("qm-sse", "report")
            yield "submitted"

    with _client(_JobAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": "go"}) as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    # Order is chronological: the fake announces the job *before* yielding its text, and the
    # consolidated sink (core.turn_signals) drains at the top of each update for exactly that
    # reason — a tool that ran while the model was producing an update ran before the text it then
    # produced. main's original assertion had token-first, which reported the text ahead of the job
    # that preceded it; the property this test names ("before the answer") holds either way.
    # Dropping `capability_degraded` first: no Temporal broker runs in a test process, so every
    # turn truthfully opens by announcing the durable subsystem is down. What this test is about
    # is that a launched job reaches the browser, and where in the order it does.
    streamed = [e for e in events if e["type"] != "capability_degraded"]
    assert [e["type"] for e in streamed] == ["job_started", "token", "answer"]
    assert streamed[0]["job_id"] == "qm-sse"


def _stream_events(  # type: ignore[no-untyped-def]
    client, session_id: str, message: str = "hi"
) -> list[dict[str, Any]]:
    """POST a turn and collect its SSE payloads, draining the stream so the generator finishes."""
    events: list[dict[str, Any]] = []
    with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": message}
    ) as res:
        assert res.status_code == 200
        for line in res.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def test_a_waiting_turn_says_so_and_is_shed_on_the_stream(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """At capacity the turn reports `queued`, then ends with an error event — not an HTTP 503.

    The admission wait used to happen before the response existed, so a client saw nothing at all
    for up to `service_turn_admission_timeout_seconds` and then a bare 503: a busy front door and
    a dead one were indistinguishable for the whole of that window (D-166). Now the stream opens
    first and the wait is on it.
    """
    import asyncio

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.05)
    app = _app()
    # Zero permits → the turn can only wait and then be shed (deterministic, no concurrency).
    app.state.turn_semaphore = asyncio.Semaphore(0)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = _stream_events(client, session_id)

    assert [e["type"] for e in events] == ["queued", "error"]
    assert events[-1]["message"] == "server at capacity; retry shortly"
    # And the shed turn left nothing behind: the session takes another turn immediately.
    assert session_id not in app.state.active_turns


def test_an_uncontended_turn_emits_no_queued_event() -> None:
    """`queued` is a report of an actual wait, so the common case must not carry one.

    An event on every turn would be noise a surface has to render and then immediately un-render,
    and it would tell an operator the front door is contended when it is idle.
    """
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = _stream_events(client, session_id)

    assert "queued" not in [e["type"] for e in events]
    assert events[-1]["type"] == "answer"


def test_a_queued_turn_runs_once_a_permit_frees(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Waiting is not failing: a turn that queues still answers when capacity returns.

    The half a shed-only test cannot see. Moving admission inside the generator put the acquire
    on the same code path as the run, and a mistake there (releasing a permit never taken, or
    returning after the wait) would end the stream instead of continuing into the turn.
    """
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 30.0)
    queued_before = METRICS.value("chemclaw_turns_queued_total")

    async def _run() -> None:
        app = _app()
        semaphore = asyncio.Semaphore(0)  # nothing free yet
        app.state.turn_semaphore = semaphore

        async def _free_a_permit_once_the_turn_waits() -> None:
            # Driven off the counter rather than a sleep, so the release lands *after* the turn
            # has parked — the moment this test is about. `httpx.ASGITransport` buffers the whole
            # response, so reacting to the `queued` line on the wire is not available here.
            async with asyncio.timeout(5):
                while METRICS.value("chemclaw_turns_queued_total") == queued_before:
                    await asyncio.sleep(0.01)
            semaphore.release()

        async with asgi_client(app) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            releaser = asyncio.create_task(_free_a_permit_once_the_turn_waits())
            res = await client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
            await releaser
            assert res.status_code == 200  # the stream opened *before* a permit existed
            events = [
                json.loads(line[len("data:") :].strip())
                for line in res.text.splitlines()
                if line.startswith("data:")
            ]
        types = [e["type"] for e in events]
        assert types[0] == "queued"  # the wait is reported, and reported first
        assert types[-1] == "answer"  # ...and the turn then runs to a real answer
        assert "error" not in types
        # The permit taken after the wait is handed back, not leaked.
        assert semaphore._value == 1

    asyncio.run(_run())


def test_permit_is_released_after_each_turn(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A finished turn returns its permit, so more turns than permits still all succeed (AG-15).

    Guards the subtle half of admission control — the `finally: semaphore.release()` in the SSE
    generator. With a single permit, three sequential turns can only all pass if each releases; a
    dropped release would silently collapse capacity (every later turn would 503 until restart).
    """
    import asyncio

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 1.0)
    app = _app()
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

    app = _app()
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


def test_null_owner_session_is_unreachable_once_entra_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NULL-owner session must not become everyone's once identity enforcement turns on (Sec-3).

    `session_owners.owner` is nullable by design (a dev/system session with no Entra oid is still
    reattachable) and `entra_required=True` never *mints* a new NULL row — but a row written while
    the deployment ran in dev mode survives a later flip to enforcement. Reverting
    `_owner_authorizes` to the old `owner is not None and owner != principal.oid` check makes this
    test fail (a stranger reads the session), proving the fix is load-bearing.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    app = _app()
    stranger = Principal(oid="stranger", upn="stranger@corp", roles=frozenset())
    app.dependency_overrides[require_principal] = lambda: stranger
    client = TestClient(app)

    session_id = "null-owner-session"
    app.state.live_sessions.add(
        session_id, _FakeAgent().create_session(session_id=session_id), None, None
    )

    # Dev-mode default (unchanged): an owner-less session degrades open, as documented.
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 200

    monkeypatch.setattr(settings, "entra_required", True)
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 404
    assert client.get(f"/sessions/{session_id}/events").status_code == 404


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
        # The two facts a conversation list is built from. `titles` is written by the turn route on
        # a session's first turn, which is also what makes a session *listable* — the real query
        # derives last-activity from `session_messages` and drops a session with none, so here a
        # session with no `updated` entry is one nobody has spoken in.
        self.titles: dict[str, str] = {}
        self.updated: dict[str, datetime] = {}

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

    async def set_title_if_absent(self, session_id: str, title: str) -> None:
        self.titles.setdefault(session_id, title)
        # Stands in for the message row the real turn would have written: a turn happened, so this
        # session now has a last activity and starts being listed.
        self.updated[session_id] = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(
            minutes=len(self.updated)
        )

    async def list_for_owner(
        self, owner: str | None
    ) -> list[tuple[str, datetime, datetime, str | None, str | None]]:
        rows = [
            (
                sid,
                self.created[sid],
                self.updated[sid],
                self.titles.get(sid),
                self.profiles[sid],
            )
            for sid, own in self.owners.items()
            if own == owner and sid in self.updated
        ]
        return sorted(rows, key=lambda row: row[2], reverse=True)


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

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        # Nothing elapses inside a test, so a refresh has nothing to do — but it still reports
        # whether the claim is ours, which is what the heartbeat now acts on.
        return self.holders.get(session_id) == holder

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
    app = _app(owner_store=_FakeOwnerStore(), turn_claims=claims)
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
    app = _app(owner_store=_FakeOwnerStore(), turn_claims=claims)
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
    app = _app(owner_store=_UnreachableOwnerStore())
    with TestClient(app) as client:
        res = client.post("/sessions")
    assert res.status_code == 503
    assert res.json()["detail"] == "server at capacity; retry shortly"
    assert METRICS.value("chemclaw_db_unavailable_total") == before + 1


def _turn(client: TestClient, session_id: str, message: str) -> None:
    """Run one turn to completion, so the session has an activity and a name."""
    with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": message}) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass


def test_session_list_is_owner_scoped_and_most_recently_used_first() -> None:
    """`GET /sessions` returns the caller's own sessions, most recently used first — nobody else's.

    The list is how a client that lost its local state finds sessions it still owns; ids are
    minted server-side, so one it forgot is otherwise unreachable while its history sits in the
    store. Scoping is the security half: a session id is a capability, and listing someone else's
    would hand it out.

    Ordered by last activity rather than by creation, which is the order a conversation list is
    actually read in — `first` is used again below and has to come back to the top.
    """
    from chemclaw.api.auth import Principal, require_principal

    alice = Principal(oid="alice", upn="a@corp", roles=frozenset())
    bob = Principal(oid="bob", upn="b@corp", roles=frozenset())
    app = _app(owner_store=_FakeOwnerStore())
    client = TestClient(app)

    app.dependency_overrides[require_principal] = lambda: alice
    first = client.post("/sessions").json()["session_id"]
    second = client.post("/sessions").json()["session_id"]
    _turn(client, first, "What is the pKa of acetic acid?")
    _turn(client, second, "Which ligand for the Suzuki?")
    app.dependency_overrides[require_principal] = lambda: bob
    bobs = client.post("/sessions").json()["session_id"]
    _turn(client, bobs, "Bob's question.")

    app.dependency_overrides[require_principal] = lambda: alice
    listed = [row["session_id"] for row in client.get("/sessions").json()]
    assert listed == [second, first]
    assert bobs not in listed

    # Returning to the older conversation moves it to the top. Under the previous ordering — the
    # row's creation date — it would have stayed second forever, which is the whole complaint.
    _turn(client, first, "And in DMSO?")
    assert [row["session_id"] for row in client.get("/sessions").json()] == [first, second]

    app.dependency_overrides[require_principal] = lambda: bob
    assert [row["session_id"] for row in client.get("/sessions").json()] == [bobs]


def test_session_list_names_each_conversation_after_its_opening_question() -> None:
    """A conversation list needs names, and the service is the only thing that can supply them.

    Without this the response was ids and dates, so every client had to invent the same
    placeholder and every restored conversation looked identical until it was opened.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app(owner_store=_FakeOwnerStore())
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]

    _turn(client, session_id, "  What is   the pKa\nof acetic acid? ")
    # Collapsed, not summarised, and not re-derived from the stored serialization.
    assert client.get("/sessions").json()[0]["title"] == "What is the pKa of acetic acid?"

    # A conversation is named by how it started, so a later turn must not rename it — otherwise the
    # sidebar entry a chemist navigates by changes under them on every message.
    _turn(client, session_id, "And in DMSO?")
    assert client.get("/sessions").json()[0]["title"] == "What is the pKa of acetic acid?"


def test_session_list_omits_a_session_nobody_ever_spoke_in() -> None:
    """A created-but-unused session is not a conversation, and must not be listed as one.

    The companion UI mints the session on the first keystroke so the first message costs one
    round-trip instead of two, which means every abandoned draft leaves an ownership row. Listing
    those gave a client a column of empty conversations indistinguishable from ones whose
    transcript had failed to load — both read as an empty array from outside.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app(owner_store=_FakeOwnerStore())
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    used = client.post("/sessions").json()["session_id"]
    warmed = client.post("/sessions").json()["session_id"]
    _turn(client, used, "A real question.")

    listed = [row["session_id"] for row in client.get("/sessions").json()]
    assert listed == [used]
    assert warmed not in listed


def test_session_list_is_empty_without_a_durable_registry() -> None:
    """Under the in-memory store there is no durable registry, so the list is honestly empty.

    Reporting the process's live LRU instead would answer a question about the deployment with an
    eviction-dependent guess that a pod restart silently changes.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app()  # owner_store None under the memory store
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
    keeps the test on the route's own behavior (ownership gate, ordering, message flattening)
    instead of re-implementing storage in a fake and asserting the fake.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from chemclaw.api.auth import Principal, require_principal

    app = _app()
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
            [HumanMessage(content="hello"), AIMessage(content="hi there")],
            state=session.state,
        )
    )

    transcript = client.get(f"/sessions/{session_id}/messages").json()
    assert [row["role"] for row in transcript] == ["user", "assistant"]
    assert transcript[0]["text"] == "hello"
    assert transcript[1]["text"] == "hi there"


def test_a_turn_writes_itself_into_the_transcript() -> None:
    """A turn that ran is readable afterwards — the half the seeded test cannot see.

    `test_transcript_reads_back_the_stored_thread` seeds `session_messages` by calling
    `save_messages` itself, deliberately and for a stated reason: it pins the route's ordering,
    flattening and ownership gate without re-implementing storage in a fake. The cost is that it
    asserts over rows it wrote itself, so it passes whether or not a turn writes anything — and for
    a while none did. `session_messages` was
    filled as a side effect of MAF's history provider; the graph keeps its thread in the
    checkpointer and calls no such hook, so when the MAF branch went the table stopped being
    written. Measured at the time: one complete turn, 0 rows, while the same session accumulated 8
    checkpoint rows. The conversation was intact and the transcript route returned `[]`.

    So this one seeds nothing. It posts a message, lets the turn run, and reads the route back —
    which is the only shape of test that can fail when the writer disappears again.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app()
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]

    with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": "what is the pKa?"}
    ) as res:
        assert res.status_code == 200
        for _line in res.iter_lines():
            pass

    transcript = client.get(f"/sessions/{session_id}/messages").json()
    assert [row["role"] for row in transcript] == ["user", "assistant"], transcript
    assert transcript[0]["text"] == "what is the pKa?"
    # `_FakeAgent` streams "hi " then "there"; the transcript stores the assembled answer, not the
    # fragments, because that is what a chemist reading back is owed.
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
    app = _app(owner_store=owners)
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
    app = _app(owner_store=owners)
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


def test_null_owner_rehydration_is_blocked_once_entra_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable-rehydration path applies the same NULL-owner rule as the live-cache path (Sec-3).

    A NULL owner recorded in the durable table (dev-mode write, surviving into enforcement) must
    404 on reattach once `entra_required` is on, exactly as the live-cache miss above does.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    owners = _FakeOwnerStore()
    session_id = "null-owner-durable"
    owners.owners[session_id] = None
    owners.profiles[session_id] = None
    owners.created[session_id] = datetime(2026, 1, 1, tzinfo=UTC)

    app = _app(owner_store=owners)
    stranger = Principal(oid="stranger", upn="stranger@corp", roles=frozenset())
    app.dependency_overrides[require_principal] = lambda: stranger
    client = TestClient(app)

    # Dev-mode default (unchanged): rehydration reattaches an owner-less durable session.
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 200

    app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)  # force rehydration
    monkeypatch.setattr(settings, "entra_required", True)
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 404


def test_no_rehydration_without_durable_store() -> None:
    """With no durable owner store (the in-memory session store), a cache miss stays a 404.

    The default path is unchanged: rehydration is gated on `session_store="postgres"`.
    """
    from chemclaw.core.config import settings

    app = _app()  # owner_store None under the memory store
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


def test_a_concurrent_burst_cannot_overrun_the_budget_by_more_than_the_permits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget's documented overshoot bound is the permit count — it was the request count.

    `BudgetTracker`'s docstring promises "up to `service_max_concurrent_turns` in-flight turns may
    pass `check` before any of them `record`". That was a statement about a call site rather than
    about the class, and the call site made it false: `check` ran at request entry, before
    admission, so *every* turn in a burst passed it while the first was still streaming. Measured
    with production-shaped values — 8 permits, 40 concurrent POSTs, a one-turn-per-user cap —
    **40 turns ran and 40,000 tokens were booked, with no 429 at all**.

    Here, scaled down so the assertion is a bound and not a race: one permit, a one-turn cap, ten
    concurrent posts on ten sessions of one user. With the re-check after the permit, at most one
    turn can be past both gates at once, so exactly one answers and the rest end with a
    `budget_exhausted` error event on their own stream (D-166 — the stream is already open by
    then, so a 429 is no longer available to say it).

    Counterfactual: delete the re-check in `_turn_events` and this reports ten answers.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 1)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 0)
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 1)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 10.0)

    async def _drive() -> None:
        app = _app()
        app.dependency_overrides[require_principal] = lambda: Principal(oid="u1", upn="u1@corp")
        async with asgi_client(app, timeout=30) as client:
            sessions = [(await client.post("/sessions")).json()["session_id"] for _ in range(10)]

            async def _turn(session_id: str) -> list[dict[str, Any]]:
                res = await client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
                if res.status_code == 429:
                    return [{"type": "429"}]
                return [
                    json.loads(line[len("data:") :].strip())
                    for line in res.text.splitlines()
                    if line.startswith("data:")
                ]

            streams = await asyncio.gather(*(_turn(s) for s in sessions))

        kinds = [[event["type"] for event in stream] for stream in streams]
        answered = [k for k in kinds if "answer" in k]
        assert len(answered) == 1, f"{len(answered)} turns ran against a one-turn budget: {kinds}"
        # Every other turn was told why, on its own stream or by status — never silently dropped.
        for stream in streams:
            if any(event["type"] == "answer" for event in stream):
                continue
            last = stream[-1]
            assert last["type"] in ("error", "429"), stream
            if last["type"] == "error":
                assert last["code"] == "budget_exhausted", last
        booked = app.state.budget._users.get("u1")
        assert booked is not None and booked.turns == 1, booked

    asyncio.run(_drive())


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


def _gated_agent(gate: asyncio.Event, started: asyncio.Event, blocked_message: str) -> _FakeAgent:
    """A fake whose turn for `blocked_message` parks on `gate` (concurrency tests).

    The sync TestClient runs each request to completion before returning, so it cannot hold one
    turn open while another is issued — these tests drive the app over httpx's ASGI transport on
    a real event loop instead, with `started`/`gate` sequencing the overlap deterministically.
    """

    class _GatedAgent(_FakeAgent):
        """A fake whose turn parks until the test releases it."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            if message == blocked_message:
                started.set()
                await gate.wait()
            yield "done"

    return _GatedAgent()


def test_concurrent_turn_on_same_session_is_409() -> None:
    """While one turn runs, a second POST to the same session is rejected with 409.

    Two concurrent turns would drive `agent.run` against the same TurnSession at once,
    interleaving two turns' messages into one conversation thread — so the second is shed
    (matching the admission semaphore's shed-don't-queue semantics), and the slot frees when
    the running turn's stream ends.
    """

    async def _run() -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        app = _app(_gated_agent(gate, started, "first"))
        async with asgi_client(app) as client:
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

    async def _run() -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        app = _app(_gated_agent(gate, started, "blocked"))
        async with asgi_client(app) as client:
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
        """A fake standing in for a hung LLM endpoint: one token, then nothing."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield "partial"
            await asyncio.sleep(60)  # a hung LLM endpoint: never yields again
            yield "never"

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.2)
    app = _app(_HungAgent())
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


def test_a_client_cancelled_mid_admission_leaves_a_turn_that_finishes_itself(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A disconnect during the admission wait detaches the client; the turn queues, runs, frees.

    This test used to pin the opposite: the cancellation freed the session's slot immediately,
    because a disconnect *was* the stop. Under
    `D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` the turn belongs to the chemist's request
    rather than to the socket that carried it — so the abandoned turn keeps its claim (a 409 for
    a concurrent second tab is *correct*: the session is genuinely busy), takes its permit when
    capacity returns, runs to completion unwatched, and only then frees the slot. What must not
    happen is the old leak this test was born for: a slot held forever by a turn that no longer
    exists. The turn existing and finishing is what prevents that now.
    """
    import contextlib

    import httpx

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 30.0)

    async def _run() -> None:
        app = _app()
        # Zero permits: the turn parks on the semaphore *after* taking the session's slot.
        app.state.turn_semaphore = asyncio.Semaphore(0)
        async with asgi_client(app) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            waiting = asyncio.create_task(
                client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
            )
            async with asyncio.timeout(5):
                while session_id not in app.state.active_turns:  # parked mid-admission
                    await asyncio.sleep(0.01)
            waiting.cancel()  # the client goes away; the turn does not
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await waiting

            # The detached turn still holds the session — queued, not leaked.
            assert session_id in app.state.active_turns
            app.state.turn_semaphore.release()  # capacity returns...
            # ...and the abandoned turn runs to completion unwatched, freeing the slot at its
            # true end — the transcript, not this socket, is where its answer went.
            async with asyncio.timeout(5):
                while session_id in app.state.active_turns:
                    await asyncio.sleep(0.01)
            res = await client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
            assert res.status_code == 200  # the session is usable, not 409-bricked

    asyncio.run(_run())


def test_a_session_with_a_turn_in_flight_is_pinned_against_eviction(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Capacity pressure must not evict a mid-turn session and mint a second live handle (A5).

    The live cache is a pure-capacity LRU with no notion of "in use": evicting a session does not
    stop its running turn — the turn holds the `TurnSession` object directly — it makes the next
    request rehydrate a brand-new handle over the same durable history, and the two then diverge
    in `session.state`. So a session whose turn is in flight (an unexpired `active_turns` lease)
    is pinned, and the cache briefly holds over capacity instead.

    Counterfactual: drop the pin (`_LiveSessions.add` evicting purely by LRU) and the identity
    assertion fails — the transcript read rehydrates a second handle while the first still
    streams.
    """
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_max_live_sessions", 1)

    async def _run() -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        app = _app(_gated_agent(gate, started, "long"), owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            first = (await client.post("/sessions")).json()["session_id"]
            original = app.state.live_sessions.get(first).session
            turn = asyncio.create_task(
                client.post(f"/sessions/{first}/messages", json={"message": "long"})
            )
            await asyncio.wait_for(started.wait(), timeout=5)  # the turn is mid-stream
            # One slot: creating a second session is exactly the pressure that used to evict.
            await client.post("/sessions")
            # Touch the first session the way any request would; on the unpinned code this is
            # the moment a second handle is minted over the same durable id.
            transcript = await client.get(f"/sessions/{first}/messages")
            assert transcript.status_code == 200
            entry = app.state.live_sessions.get(first)
            assert entry is not None, "the in-flight session was evicted under capacity pressure"
            assert entry.session is original, (
                "a second live handle was minted for a session whose turn is still streaming"
            )
            gate.set()
            assert (await turn).status_code == 200

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
        app = _app()
        async with asgi_client(app) as client:
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


def test_events_route_claims_only_the_job_outcome_kinds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The push-back route scopes its (destructive) claim to the job-outcome kinds in the claim.

    The claim marks rows consumed atomically; claiming every kind and filtering afterwards would
    silently destroy events of other kinds meant for other consumers. Both outcomes are claimed:
    a job that failed after its turn ended has the same claim on the asker's attention as one that
    succeeded, and until 2026-08-04 only the successful one had any way of reaching them.
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
    assert captured["kinds"] == ("job_completed", "job_failed")


def test_a_failed_job_reaches_the_asker_with_its_reason(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A durable job that fails after its turn must say so, and say why.

    Found live (2026-08-04): `compare_solvents` was launched, the turn told the chemist it was
    running, and it failed ~30 s later on an unknown ALPB solvent name. `ConnectorJobWorkflow`
    awaited its child with no failure path, so `notify_session_best_effort` was never reached and
    no event of any kind was emitted — the "job started" promise stood forever and the reason was
    reachable only by polling `get_durable_job_status` with an id nobody had kept.

    The reason travels because a failure without one is the defect this repository has now met
    three times: an outcome that says nothing is an invitation to assume the good one.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent

    async def _fake_stream(session_id: str, **kwargs: object) -> object:
        yield SessionEvent(
            session_id=session_id,
            kind="job_failed",
            payload={
                "job_id": "calc-compare_solvents-abc",
                "reason": "unknown ALPB solvent '2-methyltetrahydrofuran'",
            },
        )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            body = "".join(line for line in res.iter_lines())

    assert "job_failed" in body
    assert "calc-compare_solvents-abc" in body
    assert "2-methyltetrahydrofuran" in body, (
        "the reason must survive to the client, not just the type"
    )


def test_a_session_with_no_plan_has_nothing_to_decide_on() -> None:
    """`POST /plan/decision` must refuse a session that is proposing nothing.

    The empty todo list hashes to a constant — the same string for every session in every
    deployment — so a decision recorded against it is not a fact about this session's plan, and it
    is one a rehydrated session (which has lost its todo state) proposes again for free. The route
    recorded it anyway, with no emptiness check. (The CLI's `/approve` looked like it had one, and
    did not: it guarded on `todo_titles` — the *display* list, which counts the launcher's
    `awaiting-job:` rows — while recording against a hash that strips them, so a bookkeeping-only
    session recorded the same useless approval there too. Both now ask `approvable_plan_hash`.)

    409 rather than 422: the request is well-formed, it conflicts with the session's current state,
    which is exactly what the sibling "the plan changed since it was shown" refusal means. The hash
    posted here is the real one `GET /plan` reports, so this cannot pass by mismatching.
    """
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        plan = client.get(f"/sessions/{session_id}/plan").json()
        assert plan["plan"] == [], "the precondition is a session with no plan"
        res = client.post(
            f"/sessions/{session_id}/plan/decision",
            json={"approved": True, "plan_hash": plan["plan_hash"]},
        )
        assert res.status_code == 409, f"the empty plan was decided on: {res.status_code}"
        assert client.get(f"/sessions/{session_id}/plan").json()["decided_by"] is None, (
            "a decision was recorded against the empty plan"
        )
        # And a row that exists anyway — written before this refused, and durable — must not come
        # back as an approval either, or the display disagrees with the gate that refuses it.
        asyncio.run(
            client.app.state.plan_approvals.record(  # type: ignore[attr-defined]
                session_id, plan["plan_hash"], "someone", True
            )
        )
        assert client.get(f"/sessions/{session_id}/plan").json()["approved"] is False, (
            "the front door reported the empty plan as approved"
        )


def test_every_session_scoped_route_is_ownership_gated() -> None:
    """Every route carrying a session id resolves ownership — a non-owner gets 404 on all of them.

    Enumerates the app's routes rather than hardcoding today's two, so a future session-scoped
    route that skips the `_resolve_session` gate fails here: the inventory assertion forces a
    conscious update, and the behavioral sweep then proves the new route 404s for a non-owner.
    """
    from fastapi.routing import APIRoute

    from chemclaw.api.auth import Principal, require_principal

    app = _app()
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
        # The stored full text of what one tool returned. Owner-scoped for the reason the route
        # is hung off a session at all: a ref is the SHA-256 of a result's own text, so it is
        # unguessable but not secret, and this gate — not the ref — is what says who may read it.
        ("/sessions/{session_id}/tool-results/{ref}", "GET"),
        # The explicit stop (D-2026-08-27-a-disconnect-is-a-detach-not-a-stop). Owner-scoped
        # because cancelling someone else's running turn is exactly the interference the
        # ownership gate exists to refuse — and 404 either way, so a stranger cannot learn
        # whether a session is mid-turn.
        ("/sessions/{session_id}/turn/stop", "POST"),
        # Deleting the conversation
        # (`D-2026-08-27-a-session-list-is-a-cursor-and-a-session-is-deletable`). Owner-scoped
        # through the *same* gate reading it is, deliberately: a caller who cannot read a session
        # must not be able to delete it, and one gate cannot drift from itself. 404 either way, so
        # a stranger cannot use it to learn which ids exist.
        ("/sessions/{session_id}", "DELETE"),
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
            # `ref` is supplied for the tool-result route and ignored by every other path;
            # `str.format` drops the surplus keyword rather than complaining, so one line still
            # builds a URL for all of them.
            url = route.path.format(session_id=session_id, ref="0" * 64)
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


def test_the_per_connector_health_gauge_actually_renders_a_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`chemclaw_connector_unhealthy` was declared, panelled, and bound by nothing.

    A gauge *family* renders only what its bound source returns, so an unbound one contributes no
    lines to `/metrics` at all — and the "Connector reachability" panel querying
    `max by (connector) (chemclaw_connector_unhealthy)` was therefore a graph that could never
    draw. Empty for a healthy fleet and empty for a broken one is exactly the failure the
    unlabelled count beside it exists to end, reproduced one level up by the metric that was
    supposed to say *which*.

    Asserted over the rendered exposition rather than over the binding, because the binding is not
    the thing that was missing — the series was.
    """
    from chemclaw.api import app as service_app
    from chemclaw.connectors.health import ConnectorHealth

    async def _mixed() -> list[ConnectorHealth]:
        return [
            ConnectorHealth(name="calc", state="unreachable", detail="connection refused"),
            ConnectorHealth(name="molfp", state="healthy"),
            ConnectorHealth(name="results", state="unprobed"),
        ]

    monkeypatch.setattr(service_app, "probe_connectors", _mixed)
    monkeypatch.setattr(service_app, "check_connectors_at_startup", _mixed)
    monkeypatch.setattr(settings, "session_store", "memory")

    with TestClient(service_app.create_app(connector_factory=_no_connectors)) as client:
        exposition = client.get("/metrics").text
    assert 'chemclaw_connector_unhealthy{connector="calc"} 1' in exposition, exposition
    assert 'chemclaw_connector_unhealthy{connector="molfp"} 0' in exposition, exposition
    # `unprobed` is deliberately 0 rather than absent: `connectors/health.py` does not count it as
    # unhealthy, and omitting it would make "no series" mean both "reachable" and "never asked".
    assert 'chemclaw_connector_unhealthy{connector="results"} 0' in exposition, exposition
    # And the unlabelled count it has to agree with, from the same probe result.
    assert "chemclaw_connectors_unhealthy 1" in exposition, exposition


def test_readyz_does_not_name_the_connector_fleet_to_an_unauthenticated_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/readyz` is outside `require_principal` by necessity, so its body is a public document.

    A kubelet cannot present a token, which is why the route is open and why that is right. What
    the body carried was more than a readiness verdict: the name of every enabled connector and
    which of them was currently down — an inventory of the deployment's internal capability
    surface, plus a live signal of when a dependency is degraded, to anyone who can reach the pod
    or the Route (which declares no `spec.path`, so `/readyz` is reachable on the external host).

    The verdict and a count answer every question a probe or an operator's `curl` actually asks;
    the names stay where they were already accepted as scrape-visible —
    `chemclaw_connectors_unhealthy` on `/metrics`, and the per-connector WARNING each failed probe
    already logs. The chart's own comment accepts "operational reconnaissance" for `/metrics`
    counts; it never argued it for names.
    """
    from chemclaw.api import app as service_app
    from chemclaw.connectors.health import ConnectorHealth

    async def _degraded() -> list[ConnectorHealth]:
        return [
            ConnectorHealth(name="calc", state="unreachable", detail="connection refused"),
            ConnectorHealth(name="molfp", state="healthy"),
        ]

    monkeypatch.setattr(service_app, "probe_connectors", _degraded)
    monkeypatch.setattr(service_app, "check_connectors_at_startup", _degraded)
    monkeypatch.setattr(settings, "session_store", "memory")

    with TestClient(service_app.create_app(connector_factory=_no_connectors)) as client:
        body = client.get("/readyz").json()
    assert body["status"] == "ready"
    rendered = json.dumps(body)
    assert "calc" not in rendered and "molfp" not in rendered, rendered
    assert "connection refused" not in rendered, rendered
    assert body["connectors_unhealthy"] == 1


def _stream_with_header(  # type: ignore[no-untyped-def]
    client, session_id: str, message: str = "hi"
) -> tuple[str, list[dict[str, Any]]]:
    """POST a turn; return the response's correlation-id header beside its SSE payloads.

    The header and the events are read from the *same* response on purpose: the whole question
    these tests ask is whether a chemist quoting the id in an error event names the turn an
    operator finds under the id the response, the access log and the audit trail all carry.
    """
    events: list[dict[str, Any]] = []
    with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": message}
    ) as res:
        assert res.status_code == 200
        header = res.headers.get("x-chemclaw-correlation-id", "")
        for line in res.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    assert header, "the front door always stamps a correlation id on its responses"
    return header, events


def test_a_shed_turn_names_the_correlation_id_the_request_ran_under(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Every error event this route emits must quote the *request's* id, not none and not a new one.

    `ErrorEvent.correlation_id` exists for exactly one job — "quoting it in a bug report is what
    lets an operator find the turn" — and `run_turn` already adopts the ambient id for that reason,
    naming the alternative as "two ids for one event, which is the failure a correlation id exists
    to prevent". The three error events `api/routes/turns.py` raises around `run_turn` did not: the
    shed, the budget refusal and the timeout sent `""`, so the one failure a chemist is most likely
    to report — a turn that ran out of wall clock — carried nothing to look up.
    """
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.05)
    app = _app()
    app.state.turn_semaphore = asyncio.Semaphore(0)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        header, events = _stream_with_header(client, session_id)
    assert [e["type"] for e in events] == ["queued", "error"]
    assert events[-1]["correlation_id"] == header


def test_a_timed_out_turn_names_the_correlation_id_the_request_ran_under(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The wall-clock kill is the failure a chemist reports; it must be findable."""

    class _HungAgent(_FakeAgent):
        """A fake standing in for a hung LLM endpoint: one token, then nothing."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield "partial"
            await asyncio.sleep(60)
            yield "never"

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.2)
    app = _app(_HungAgent())
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        header, events = _stream_with_header(client, session_id)
    assert events[-1]["code"] == "turn_timeout"
    assert events[-1]["correlation_id"] == header


def test_the_streams_catch_all_quotes_the_requests_id_rather_than_minting_one() -> None:
    """The one path that *did* send an id sent a fabricated one — worse than sending none.

    `failure_event(exc, session_id, uuid.uuid4().hex)` handed the chemist a hex string that
    appears in no log line, no `audit_events` row and no access log, while the route's own
    `logger.exception` one line above was stamped with the request's id. Measured: the response
    header and the exception's log record both read `537fe7ea…`; the event told the user
    `8568a58b…`.

    The reachable trigger is the one the route's own comment names — a session whose stored profile
    the deployment no longer ships, so `connector_factory` raises on every turn.
    """

    def _retired_profile(_profile: str | None = None) -> Any:
        raise ValueError("profile 'gone' is not shipped by this deployment")

    app = _app(_FakeAgent(), connector_factory=_retired_profile)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        header, events = _stream_with_header(client, session_id)
    assert events[-1]["code"] == "internal"
    assert events[-1]["correlation_id"] == header
