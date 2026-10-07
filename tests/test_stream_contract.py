"""Every SSE stream ends in a terminal event — including when it fails before the first token.

`run_turn` keeps the invariant inside itself; these tests cover the route around it: a failure
between the admission permit and `run_turn`, a database failure on the push-back tailer, a turn
admitted beside another during setup, and a client that stops reading. Each drives
`create_app()`, over `TestClient` or raw ASGI where the client never reads.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, MutableMapping
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent.session import TurnSession
from chemclaw.api.app import create_app
from chemclaw.api.state import (
    _actor_turns_in_flight,
    _claim_turn_slot,
    _release_turn_slot,
    _start_turn_lease,
)
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.metrics import METRICS
from chemclaw.core.session_context import get_current_session_id
from tests.fakes import asgi_client
from tests.fakes_turn import Piece, ScriptedTurn


class _AnsweringTurn(ScriptedTurn):
    """One token and an answer — enough for a stream to have a shape to break."""

    def create_session(self, *, session_id: str) -> TurnSession:
        """The one non-streaming method the front door calls on an agent."""
        return TurnSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Answer immediately; nothing here is about what the model says."""
        yield "ok"


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """No connector opens for these turns: every failure under test is the front door's own."""
    return []


def _app(**kwargs: Any) -> Any:
    """The production app with only the model faked, as every other front-door test builds it."""
    kwargs.setdefault("connector_factory", _no_connectors)
    return create_app(graph_factory=_AnsweringTurn().graph_factory, **kwargs)


def _sse_events(client: TestClient, method: str, path: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Drive one SSE request to the end of its stream and return the payloads it carried."""
    events: list[dict[str, Any]] = []
    with client.stream(method, path, **kwargs) as res:
        assert res.status_code == 200
        for line in res.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


# --- F1: a failure before the first token ------------------------------------------------------


def test_a_turn_that_fails_before_run_turn_still_ends_in_an_error_event() -> None:
    """A turn that fails before `run_turn` still ends in an error event.

    E.g. a rehydrated session whose stored profile no longer exists raises while the route evaluates
    `run_turn`'s arguments, after `http.response.start`, where no exception handler can run.
    """

    def _broken_registry(_profile: str | None = None) -> list[Any]:
        """Stand in for the stale-profile raise, at the same call site and with the same shape."""
        raise ValueError("unknown agent profile 'retired-profile'; known: ['default']")

    app = _app(connector_factory=_broken_registry)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = _sse_events(
            client, "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
        )

    assert events, "the stream carried no events at all — the silent death this test exists for"
    assert events[-1]["type"] == "error", f"the stream did not end in an error event: {events}"
    assert events[-1]["code"] == "internal"
    assert events[-1]["correlation_id"], "the error carries no id a bug report could quote"
    # The detail stays server-side: the client is told a turn failed, not which profile is missing.
    assert "retired-profile" not in events[-1]["message"]
    # And the guards the failure ran through are handed back, so the session is usable again.
    assert session_id not in app.state.active_turns
    assert app.state.turn_semaphore._value == settings.service_max_concurrent_turns


# --- F3: a mid-stream database failure on the push-back stream ---------------------------------


def test_a_push_back_stream_whose_tailer_dies_ends_in_an_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tailer raising `ConnectionError` ends the stream in an error event and is counted.

    The app's `ConnectionError` handler cannot run once the response has started, so the stream must
    handle it and move `chemclaw_db_unavailable_total` itself.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent

    async def _dying_stream(session_id: str, **_: object) -> AsyncIterator[SessionEvent]:
        """One real push-back, then the failure a rolled Postgres delivers on the next poll."""
        yield SessionEvent(session_id=session_id, kind="job_completed", payload={"job_id": "qm-1"})
        raise ConnectionError("Postgres unreachable at host=db")

    monkeypatch.setattr(app_module, "stream_new_events", _dying_stream)
    before = METRICS.render()

    app = _app()
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = _sse_events(client, "GET", f"/sessions/{session_id}/events")

    assert [event["type"] for event in events] == ["job_completed", "error"]
    assert events[-1]["code"] == "storage_unavailable"
    assert events[-1]["retryable"] is True, "a database outage is the retryable failure"
    # The join key, on the one error a chemist sees when the push-back channel dies. The turn
    # route's four events were swept onto `get_current_correlation_id()`; this one was missed, so
    # a chemist asked to quote an id had nothing to quote while the response header carried one.
    assert events[-1]["correlation_id"], (
        "the push-back stream's error carries no correlation id a chemist could quote"
    )
    assert METRICS.render() != before, "the outage was invisible to chemclaw_db_unavailable_total"
    # The stream's admission slot comes back, as it already did — this must not regress.
    assert app.state.event_streams == {}


def test_each_push_back_tailer_polls_on_an_interval_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each push-back tailer polls on an interval of its own.

    Tailers restarted together would otherwise stay synchronised and hit the pool as one burst.
    Asserted at the seam where the route chooses the interval; move this test if the spread moves
    into the tailer.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent

    intervals: list[float] = []

    async def _record(session_id: str, **kwargs: Any) -> AsyncIterator[SessionEvent]:
        """Record the interval this stream was given, then end it immediately."""
        intervals.append(float(kwargs.get("poll_seconds", settings.session_event_poll_seconds)))
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(app_module, "stream_new_events", _record)

    with TestClient(_app()) as client:
        for _ in range(12):
            session_id = client.post("/sessions").json()["session_id"]
            _sse_events(client, "GET", f"/sessions/{session_id}/events")

    assert len(intervals) == 12
    assert len(set(intervals)) == len(intervals), (
        f"tailers share a poll interval and therefore a phase: {intervals}"
    )
    base = settings.session_event_poll_seconds
    assert max(intervals) - min(intervals) > base * 0.1, (
        f"the intervals are spread over {max(intervals) - min(intervals):.4f}s of a {base}s "
        "period — too narrow to pull a synchronised fleet apart"
    )
    assert all(base * 0.75 <= interval <= base * 1.25 for interval in intervals), (
        f"a stream was given {min(intervals)}..{max(intervals)}s against a configured {base}s; the "
        "spread must not become a second, unstated poll interval"
    )


# --- F5: the in-process turn lease ------------------------------------------------------------


class _SlowOwnerStore:
    """A session-ownership registry whose title write takes as long as a saturated pool does.

    The write happens after the slot is reserved and before the streaming response exists.
    """

    def __init__(self) -> None:
        """Start with nothing recorded and nobody waiting."""
        self.inside = asyncio.Event()
        self.release = asyncio.Event()
        self.parked = False
        self.owners: dict[str, str | None] = {}

    async def record(self, session_id: str, owner: str | None, profile: str | None = None) -> None:
        """Record a session's owner at creation (fast — only the title write is slow)."""
        self.owners[session_id] = owner

    async def lookup(self, session_id: str) -> tuple[bool, str | None, str | None]:
        """Answer the ownership question for a session this store has seen."""
        if session_id not in self.owners:
            return False, None, None
        return True, self.owners[session_id], None

    async def set_title_if_absent(self, session_id: str, title: str) -> None:
        """Park the *first* caller until the test lets go — one slow store round trip, held open.

        Only the first: a second turn that gets admitted must be able to *finish*, or the
        counterfactual reads as a hung test rather than as the extra turn it is.
        """
        if self.parked:
            return
        self.parked = True
        self.inside.set()
        await self.release.wait()

    async def list_for_owner(self, owner: str | None) -> list[Any]:
        """Nothing here lists sessions."""
        return []


async def test_a_turn_still_setting_up_holds_the_session_against_a_second_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second POST during the first turn's setup waits in line, not beside it.

    The lease clock starts only at hand-off, so store latency during setup cannot let a second turn
    in. The fake agent answers at once, so a turn admitted beside the first would finish long before
    the first is released.
    """
    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.05)
    store = _SlowOwnerStore()

    app = _app(owner_store=store)
    async with asgi_client(app, timeout=10.0) as client:
        session_id = (await client.post("/sessions")).json()["session_id"]
        first = asyncio.create_task(
            client.post(f"/sessions/{session_id}/messages", json={"message": "one"})
        )
        async with asyncio.timeout(5):
            await store.inside.wait()
        # Past the lease the claim was stamped with, with the first turn not yet begun.
        await asyncio.sleep(0.2)
        second = asyncio.create_task(
            client.post(f"/sessions/{session_id}/messages", json={"message": "two"})
        )
        await asyncio.sleep(0.2)
        ran_beside = second.done()
        # Released before the assertion, so a failure reports rather than hanging on the
        # first turn's parked round trip.
        store.release.set()
        admitted = await first
        waited = await second
        assert not ran_beside, "a second turn ran while the first was still being set up"
        assert admitted.status_code == 200
        assert waited.status_code == 200
        assert '"type":"queued"' in waited.text and '"ticket"' in waited.text, (
            "the second message did not report its place in the session's line"
        )


class _BrokenTitleStore(_SlowOwnerStore):
    """A registry whose title write fails once, the way a saturated pool does."""

    async def set_title_if_absent(self, session_id: str, title: str) -> None:
        """Fail the first write and succeed afterwards, so the recovery is observable."""
        if not self.parked:
            self.parked = True
            raise ConnectionError("no connection available in 10s")


def test_a_store_failure_before_the_stream_gives_the_sessions_slot_back() -> None:
    """A store failure before the stream gives the session's slot back.

    The reservation has no expiry until hand-off, so every setup path must release it in
    `post_message`'s `finally`.
    """
    app = _app(owner_store=_BrokenTitleStore())
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        shed = client.post(f"/sessions/{session_id}/messages", json={"message": "one"})
        assert shed.status_code == 503
        assert app.state.active_turns == {}, "the shed turn kept the session's slot"
        events = _sse_events(
            client, "POST", f"/sessions/{session_id}/messages", json={"message": "two"}
        )
    assert events[-1]["type"] == "answer", "the session was 409-bricked by the shed turn"


def test_a_lapsed_turns_teardown_cannot_revoke_its_successors_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lapsed turn's teardown removes only its own entry, never its successor's claim."""
    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.01)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.0)
    active: dict[str, Any] = {}

    first = _claim_turn_slot(active, "s1", actor="alice")
    assert first is not None
    _start_turn_lease(active, "s1", first)
    # The lease lapses (the never-advanced-generator window this expiry exists for), and a
    # successor takes the slot.
    import time

    while any(lease.deadline > time.monotonic() for lease in active.values()):
        time.sleep(0.005)
    second = _claim_turn_slot(active, "s1", actor="alice")
    assert second is not None

    _release_turn_slot(active, "s1", first)
    assert "s1" in active, "the lapsed turn's teardown revoked its successor's claim"
    _release_turn_slot(active, "s1", second)
    assert active == {}


# --- F6: the client that stops reading ---------------------------------------------------------


def _scope(method: str, path: str) -> dict[str, Any]:
    """A minimal ASGI HTTP scope for one request against the front door."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


async def _request(app: Any, method: str, path: str, payload: dict[str, Any]) -> tuple[int, bytes]:
    """Drive one ordinary request to completion and return its status and body."""
    sent: list[MutableMapping[str, Any]] = []
    body = json.dumps(payload).encode()

    async def _receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def _send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(_scope(method, path), _receive, _send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    chunks = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(status), chunks


class _StallingTurn(ScriptedTurn):
    """A turn that streams many small pieces, so a client can stop reading in the middle of one."""

    def create_session(self, *, session_id: str) -> TurnSession:
        """The one non-streaming method the front door calls on an agent."""
        return TurnSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Produce tokens for longer than the send deadline the test sets."""
        for _ in range(200):
            await asyncio.sleep(0.01)
            yield "tok "


def test_a_client_that_stops_reading_detaches_the_stream_and_the_turn_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that stops reading detaches the stream; the turn still cleans up.

    `send_timeout` ends the view only; the pump keeps driving the turn and releases the permit,
    lease and ambients in its own context, never via the async-generator GC finalizer. Asserted in
    two halves: the ASGI call returns while the turn is held, then every guard is back. Raw ASGI,
    since no test client can stop reading after the headers.
    """
    monkeypatch.setattr(settings, "service_sse_send_timeout_seconds", 1.0)
    # Far above the send deadline, so what ends the stream is unambiguously the transport bound —
    # the case the turn deadline cannot answer.
    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 30.0)

    async def _run() -> None:
        app = create_app(
            graph_factory=_StallingTurn().graph_factory, connector_factory=_no_connectors
        )
        _, body = await _request(app, "POST", "/sessions", {})
        session_id = json.loads(body)["session_id"]

        stalled = asyncio.Event()
        delivered = False
        accepted = 0  # body messages the "client" took before it stopped reading

        async def _receive() -> MutableMapping[str, Any]:
            """Deliver the body, then say nothing ever again — no disconnect, just silence."""
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": json.dumps({"message": "hi"}).encode(),
                    "more_body": False,
                }
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def _send(message: MutableMapping[str, Any]) -> None:
            """Take the response headers, then block on the first body byte and never drain."""
            nonlocal accepted
            if message["type"] != "http.response.body":
                return
            accepted += 1
            stalled.set()
            await asyncio.Event().wait()

        turn = asyncio.create_task(
            app(_scope("POST", f"/sessions/{session_id}/messages"), _receive, _send)
        )
        async with asyncio.timeout(20):
            await stalled.wait()
            # The send deadline, not the turn deadline, is what ends the *stream* — and it ends
            # it cleanly: the app returns rather than raising out of the ASGI callable.
            await turn

            # First half: the stream is gone but the turn is not. The session stays 409-locked
            # and the permit stays taken for exactly as long as the model is still working —
            # a detach is not a teardown.
            running = app.state.running_turns.get(session_id)
            assert running is not None and running.detached, (
                "the stalled client's turn should continue detached, not die with the stream"
            )
            assert session_id in app.state.active_turns, (
                "a detached turn must keep its session lease until it actually ends"
            )

            # Second half: the turn ends on its own (the scripted stream is ~2s), and the pump's
            # completion — not a garbage collector — is what releases everything.
            while app.state.running_turns.get(session_id) is not None:
                await asyncio.sleep(0.05)

        assert app.state.active_turns == {}, "the finished turn kept the session's turn slot"
        assert app.state.turn_semaphore._value == settings.service_max_concurrent_turns, (
            "the finished turn kept its admission permit"
        )
        # The ambients the turn stamped are gone, which is the half the GC finalizer used to
        # abandon at its first `ValueError`.
        assert get_current_session_id() is None
        assert get_current_actor() is None

    asyncio.run(_run())


async def test_a_turn_torn_down_in_a_foreign_context_still_unstamps_every_ambient() -> None:
    """A turn torn down in a foreign `Context` still resets every ambient.

    Each contextvar reset raises `ValueError` there; the teardown tolerates it so later resets run.
    """
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner import _turn_ambient

    async def _turn() -> AsyncIterator[str]:
        """Stand in for `run_turn`: stamp the turn's ambients, then park at a yield."""
        with _turn_ambient(
            "s-foreign",
            "u-1",
            frozenset({"chemist"}),
            False,
            "cid-1",
            TurnUsage(),
            ["hello"],
        ):
            yield "parked"

    # The concrete object is an async *generator*; the cast narrows the declared type to the
    # real one, as `tests/test_turn_cancellation._closable` does for the same reason.
    stream = cast(AsyncGenerator[str, None], _turn())
    # Advanced inside a task, so the tokens are created in *that* task's copy of the context —
    # exactly as sse-starlette's `_stream_response` task creates them.
    await asyncio.create_task(anext(stream))
    # Closed from a different task, as the async-generator GC finalizer does.
    await asyncio.create_task(stream.aclose())


# --- F5: the route's own error events carry the same joins the runner's do ----------------------


def test_the_routes_own_error_events_carry_the_correlation_id_the_header_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's own error events carry the correlation id the response header does.

    Asserted equal to the header, since the field's value is being the same id.
    """
    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.05)

    stalling = create_app(
        graph_factory=_StallingTurn().graph_factory, connector_factory=_no_connectors
    )
    with TestClient(stalling) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
        ) as res:
            assert res.status_code == 200
            header = res.headers["X-Chemclaw-Correlation-Id"]
            events = [
                json.loads(line[len("data:") :].strip())
                for line in res.iter_lines()
                if line.startswith("data:")
            ]

    timeout = [event for event in events if event.get("code") == "turn_timeout"]
    assert timeout, f"the turn did not time out: {events}"
    assert timeout[0]["correlation_id"] == header, (
        "the timeout event carries "
        f"{timeout[0]['correlation_id']!r} while the header and `turn_costs` carry {header!r}"
    )


def test_a_shed_turn_and_a_spent_budget_do_not_share_one_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admission shed and a spent budget do not share one `ErrorCode`.

    Their remedies are opposite (retry soon versus stop), and the code is how a surface chooses.
    `retryable` is unchanged on both.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 1)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.05)

    async def _run() -> list[dict[str, Any]]:
        app = create_app(
            graph_factory=_StallingTurn().graph_factory, connector_factory=_no_connectors
        )
        async with asgi_client(app, timeout=10.0) as client:
            first_id = (await client.post("/sessions")).json()["session_id"]
            second_id = (await client.post("/sessions")).json()["session_id"]
            holder = asyncio.create_task(
                client.post(f"/sessions/{first_id}/messages", json={"message": "hold"})
            )
            await asyncio.sleep(0.2)  # the holder takes the one permit
            shed = await client.post(f"/sessions/{second_id}/messages", json={"message": "shed"})
            holder.cancel()
            return [
                json.loads(line[len("data:") :].strip())
                for line in shed.text.splitlines()
                if line.startswith("data:")
            ]

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 5.0)
    events = asyncio.run(_run())

    errors = [event for event in events if event.get("type") == "error"]
    assert errors, f"the second turn was not shed: {events}"
    assert errors[-1]["code"] == "at_capacity", (
        f"an admission shed reached the client as {errors[-1]['code']!r}, the same code a spent "
        "budget uses — with the opposite remedy"
    )
    assert errors[-1]["retryable"] is True


def test_an_expired_lease_does_not_hold_an_actors_slot() -> None:
    """An expired lease does not hold an actor's slot.

    The per-actor count is derived from the lease map rather than kept as an integer, since a
    client gone before its generator first runs executes no `finally`; expiry bounds the cost to one
    lease width.
    """
    active: dict[str, Any] = {}
    token = _claim_turn_slot(active, "s1", actor="alice")
    assert token is not None
    assert _actor_turns_in_flight(active, "alice", besides="other") == 1

    # Lapse it the way `_start_turn_lease` would, with a deadline already in the past.
    active["s1"] = type(active["s1"])(
        token=token, deadline=0.0, actor="alice", claimed_at=active["s1"].claimed_at
    )
    assert _actor_turns_in_flight(active, "alice", besides="other") == 0


def test_a_turns_own_session_is_not_counted_against_its_actor() -> None:
    """`besides=` is what keeps a double-submit joining its session's line rather than a 429.

    Without it the status code for one unchanged user action — posting twice to a session that is
    already running — would depend on how many *other* sessions that chemist had open.
    """
    active: dict[str, Any] = {}
    assert _claim_turn_slot(active, "s1", actor="alice") is not None
    assert _actor_turns_in_flight(active, "alice", besides="s1") == 0
    assert _actor_turns_in_flight(active, "alice", besides="s2") == 1


def test_a_maintenance_hold_is_not_a_turn() -> None:
    """Fork and delete take the same slot to *exclude* a turn; neither is one.

    They pass `actor=None`, so a chemist deleting a session does not spend a concurrency slot they
    never asked for — and `None` can never collide with a principal id.
    """
    active: dict[str, Any] = {}
    assert _claim_turn_slot(active, "s1", actor=None) is not None
    assert _actor_turns_in_flight(active, "alice", besides="other") == 0
