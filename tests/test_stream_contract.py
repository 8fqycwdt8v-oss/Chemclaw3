"""Every SSE stream ends in a terminal event — including when it fails before the first token.

`chemclaw.api.events` states the invariant for a turn ("ending with an `AnswerEvent` on success or
an `ErrorEvent` on failure") and `run_turn` keeps it faithfully — but only from *inside* itself. The
2026-08-26 front-door audit drove the real app and found four ways a stream ends with the client
holding an HTTP 200, an SSE content-type and no explanation at all: a failure between the admission
permit and `run_turn` (the arguments the route evaluates to call it), a database failure on the job
push-back tailer, a turn admitted beside another because the in-process lease was stamped before two
store round trips, and a client that stops reading, whose teardown is left to the async-generator GC
finalizer in a foreign `Context`.

All four are properties of the *route*, not of the runner, which is why none of them was visible to
the suite that covers `run_turn`. Each test here drives `create_app()` — over `TestClient` where a
status code and a body are the whole question, and over raw ASGI where the question is what happens
to a client that never reads, which no test client can express.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, MutableMapping
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent.session import TurnSession
from chemclaw.api.app import create_app
from chemclaw.api.state import _claim_turn_slot, _release_turn_slot, _start_turn_lease
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
    """A stream that dies between the permit and `run_turn` must say so, not fall silent.

    The concrete production trigger is a session whose profile the deployment no longer ships:
    `deps._rehydrate_session` restores the stored profile without re-validating it (deliberately),
    so `front.connector_factory(live.profile)` — an *argument expression* the route evaluates, one
    frame above every handler `run_turn` owns — raises `ValueError: unknown agent profile`. The
    audit measured the result over raw ASGI: `status: [200]`, `bodies: []`, and the exception
    escaping the ASGI app, because `EventSourceResponse` had already written
    `http.response.start` and Starlette's `ExceptionMiddleware` can no longer run a handler.

    A client cannot tell that from a turn that answered nothing, which is precisely the silent
    death `empty_answer` was added to eliminate — reproduced one layer above where that guard
    lives.
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
    """A `ConnectionError` from the tailer must reach the browser as a terminal event and a count.

    `create_app` registers a `ConnectionError` handler so every route that touches durable session
    state sheds retryably. That handler is structurally unreachable here: the response has already
    started, so Starlette raises `RuntimeError("Caught handled exception, but response already
    started.")` *instead of* calling it. The client then gets a truncated stream it cannot
    distinguish from "nothing has happened yet", and `chemclaw_db_unavailable_total` — the counter
    an operator alerts on — never moves for the population that matters most: the open tabs.
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
    assert METRICS.render() != before, "the outage was invisible to chemclaw_db_unavailable_total"
    # The stream's admission slot comes back, as it already did — this must not regress.
    assert app.state.event_streams == {}


# --- F5: the in-process turn lease ------------------------------------------------------------


class _SlowOwnerStore:
    """A session-ownership registry whose title write takes as long as a saturated pool does.

    The round trip is the point: `set_title_if_absent` is awaited *after* the turn slot's deadline
    has been stamped and *before* the streaming response exists, so a lease stamped at claim time
    can lapse while the turn that owns it has not started.
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


def test_a_turn_still_setting_up_holds_the_session_against_a_second_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second POST during the first turn's store round trips is a 409, not a second turn.

    `_claim_turn_slot` justifies ignoring an expired entry with "the deadline is the widest wall
    clock a *live* turn can hold the slot ... so an expired entry provably belongs to no running
    turn". It was stamped before two awaits — the title write and the durable claim — so the real
    ceiling was `(store latency) + admission + turn timeout`, strictly larger than the deadline.
    The audit drove it: B was admitted with 200 while A was still live, and B's deadline replaced
    A's under the same key.

    Both turns then hold the *same* `TurnSession` handle and interleave their messages into one
    conversation, which is the corruption the guard exists to prevent — and under
    `session_store="memory"` (the code default and what the dev lanes run) there is no second guard
    behind it.
    """
    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.05)
    store = _SlowOwnerStore()

    async def _run() -> None:
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
            second = await client.post(f"/sessions/{session_id}/messages", json={"message": "two"})
            # Released before the assertion, so a failure reports rather than hanging on the
            # first turn's parked round trip.
            store.release.set()
            admitted = await first
            assert second.status_code == 409, (
                "a second turn was admitted while the first was still being set up"
            )
            assert admitted.status_code == 200

    asyncio.run(_run())


class _BrokenTitleStore(_SlowOwnerStore):
    """A registry whose title write fails once, the way a saturated pool does."""

    async def set_title_if_absent(self, session_id: str, title: str) -> None:
        """Fail the first write and succeed afterwards, so the recovery is observable."""
        if not self.parked:
            self.parked = True
            raise ConnectionError("no connection available in 10s")


def test_a_store_failure_before_the_stream_gives_the_sessions_slot_back() -> None:
    """A turn that dies during setup must not hold the session — the reservation has no expiry.

    The slot is reserved before the store round trips and its lease clock only starts at the
    hand-off (`_start_turn_lease`), which is sound exactly because every path in between is inside
    `post_message`'s `try`/`finally`. `set_title_if_absent` is one of those paths and it is the one
    that fails in production: a failed pool checkout raises `ConnectionError` and is shed 503. From
    outside that block the session would answer 409 to its own owner for the pod's lifetime.
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
    """The teardown removes the entry *this* turn wrote, or a third turn gets admitted.

    Both teardown paths used to `pop(session_id, None)` — whatever is under the key, not the entry
    they own. So once one lease had lapsed and a successor had claimed the slot, the first turn's
    teardown revoked the successor's claim and the guard admitted a third turn beside a live one.
    """
    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.01)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.0)
    active: dict[str, Any] = {}

    first = _claim_turn_slot(active, "s1")
    assert first is not None
    _start_turn_lease(active, "s1", first)
    # The lease lapses (the never-advanced-generator window this expiry exists for), and a
    # successor takes the slot.
    import time

    while any(lease.deadline > time.monotonic() for lease in active.values()):
        time.sleep(0.005)
    second = _claim_turn_slot(active, "s1")
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
    """A stalled *transport* detaches the view; the turn ends deterministically, never via the GC.

    `asyncio.timeout` cancels the task that entered it. When the model stalls that task is
    executing inside the generator and the conversion to `TimeoutError` works (the suite covers
    it). When the stall is in the transport — `await send(...)` blocked on a client that has
    stopped reading — the response task is parked in the send, `sse_starlette`'s `send_timeout`
    ends the stream in the task serving it, and under
    `D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` that closes the *view only*: the pump task
    keeps driving the turn, so the permit, the lease and the ambients are released in the pump's
    own context at the turn's true end — never by asyncio's async-generator GC finalizer, whose
    different-`Context` `aclose()` is what the audit measured
    (`ValueError: <Token ...> was created in a different Context` out of `_turn_ambient`).

    So the assertion is in two halves: the ASGI call returns while the turn is still *held* (the
    detach), and once the pump finishes every guard is back (the deterministic teardown).

    Driven at the raw ASGI level because the property *is* the ASGI contract: no test client can
    express "the client took the headers and then stopped reading".
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


def test_a_turn_torn_down_in_a_foreign_context_still_unstamps_every_ambient() -> None:
    """The GC finalizer's `aclose()` runs in a different `Context`; the teardown must survive it.

    A contextvar `Token` records the `Context` it was created in, so every `reset_*` in
    `_turn_ambient`'s `finally` raises `ValueError` when the generator is closed from a task other
    than the one that ran it — and the *first* raise aborted the five resets after it, including
    `reset_current_identity`. Nothing is lost by tolerating it (the context holding those tokens is
    being discarded either way); what was lost was the rest of the teardown and any attribution,
    since the traceback names a `ContextVar` and no session.
    """
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner import _turn_ambient

    async def _turn() -> AsyncIterator[str]:
        """Stand in for `run_turn`: stamp the turn's ambients, then park at a yield."""
        with _turn_ambient("s-foreign", "u-1", frozenset({"chemist"}), False, "cid-1", TurnUsage()):
            yield "parked"

    async def _run() -> None:
        # The concrete object is an async *generator*; the cast narrows the declared type to the
        # real one, as `tests/test_turn_cancellation._closable` does for the same reason.
        stream = cast(AsyncGenerator[str, None], _turn())
        # Advanced inside a task, so the tokens are created in *that* task's copy of the context —
        # exactly as sse-starlette's `_stream_response` task creates them.
        await asyncio.create_task(anext(stream))
        # Closed from a different task, as the async-generator GC finalizer does.
        await asyncio.create_task(stream.aclose())

    asyncio.run(_run())
