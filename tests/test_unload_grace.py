"""An unloading page's stop waits for a reload (`D-2026-10-03-an-unload-stop-waits-for-a-reload`).

A browser cannot tell a reload from a close at unload time, so `POST …/turn/stop?reason=unload`
is deferred for `service_turn_unload_grace_seconds` and the sender reattaching cancels it. Driven
over a real uvicorn server, since a dropped stream followed by a stop cannot be expressed through
`TestClient`; the window's bounds are pinned on `DetachableTurn` directly.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from chemclaw.api.detach import DetachableTurn
from chemclaw.api.routes.turns import TURN_CORRELATION_HEADER
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.test_session_turn_queue import (
    _ANA,
    _BEN,
    _as,
    _Ledger,
    _served,
    _shared_session,
    _started,
    _stores,  # noqa: F401 - the autouse fixture those helpers assume (identity by header)
    _until,
    _watch,
)

_COUNTERS = (
    "chemclaw_turns_stop_deferred_total",
    "chemclaw_turns_stop_resumed_total",
    "chemclaw_turns_stop_expired_total",
    "chemclaw_turns_stopped_total",
)


def _counts() -> dict[str, float]:
    """The four counters this decision moves, as they stand now."""
    return {name: METRICS.value(name) for name in _COUNTERS}


def _moved(before: dict[str, float]) -> dict[str, float]:
    """How far each counter moved since `before` — the process-global registry is shared."""
    return {name: METRICS.value(name) - value for name, value in before.items()}


async def _start_and_unload(client: httpx.AsyncClient, session_id: str) -> str:
    """Ana starts a held turn, reads its first event and drops the socket — the page unloading.

    Returns the turn's correlation id, as the sender's response header carried it.
    """
    async with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": "hold"}, headers=_as(_ANA)
    ) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                break
        return response.headers["x-chemclaw-correlation-id"]


async def _reattach(
    client: httpx.AsyncClient, session_id: str, attached: asyncio.Event, named: list[str]
) -> list[dict[str, Any]]:
    """The reloaded page following its turn: `named` gets the turn id the watch response names."""
    events: list[dict[str, Any]] = []
    async with client.stream(
        "GET", f"/sessions/{session_id}/turn/stream", headers=_as(_ANA)
    ) as response:
        assert response.status_code == 200, await response.aread()
        named.append(response.headers.get(TURN_CORRELATION_HEADER, ""))
        attached.set()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:")))
    return events


async def _unload_stop(client: httpx.AsyncClient, session_id: str, *, who: Any = _ANA) -> Any:
    """The stop a `pagehide` handler sends."""
    return await client.post(
        f"/sessions/{session_id}/turn/stop", params={"reason": "unload"}, headers=_as(who)
    )


def _running(app: Any, session_id: str) -> bool:
    """Whether the session's turn is still live on this process."""
    return app.state.running_turns.get(session_id) is not None


def test_a_reload_inside_the_window_reattaches_and_the_turn_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect itself: unload stop, then the reloaded page reattaches — and gets its answer."""
    monkeypatch.setattr(settings, "service_turn_unload_grace_seconds", 0.5)
    agent = _Ledger()
    before = _counts()

    async def _run() -> tuple[Any, list[dict[str, Any]], bool, str, list[str]]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client)
                sent = await _start_and_unload(client, session_id)
                deferred = await _unload_stop(client, session_id)
                attached = asyncio.Event()
                named: list[str] = []
                watch = asyncio.create_task(_reattach(client, session_id, attached, named))
                await asyncio.wait_for(attached.wait(), 10)
                # Well past the window: had the reattach not cancelled the stop, it fired by now.
                await asyncio.sleep(1.5)
                still_running = _running(served.app, session_id)
                agent.gate.set()
                events = await watch
                served.wait_for_slot_release(session_id)
                return deferred, events, still_running, sent, named

    deferred, events, still_running, sent, named = asyncio.run(_run())
    assert deferred.status_code == 200 and deferred.json() == {"stopped": False, "deferred": True}
    assert sent and named == [sent], "the watch response did not name the turn its page sent"
    assert still_running, "the deferred stop fired although the sender reattached in its window"
    assert events and events[-1]["type"] == "answer", events
    assert events[-1]["text"].endswith("done hold"), events[-1]
    assert _moved(before) == {
        "chemclaw_turns_stop_deferred_total": 1,
        "chemclaw_turns_stop_resumed_total": 1,
        "chemclaw_turns_stop_expired_total": 0,
        "chemclaw_turns_stopped_total": 0,
    }


def test_a_reload_that_reattaches_before_the_unload_stop_arrives_keeps_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reloaded page's watch lands first and the old page's stop arrives later.

    `resume` has nothing to cancel, so the window's expiry must check who is watching rather than
    kill the turn the reloaded page is following.
    """
    monkeypatch.setattr(settings, "service_turn_unload_grace_seconds", 0.5)
    agent = _Ledger()
    before = _counts()

    async def _run() -> tuple[Any, list[dict[str, Any]], bool]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client)
                await _start_and_unload(client, session_id)
                attached = asyncio.Event()
                named: list[str] = []
                watch = asyncio.create_task(_reattach(client, session_id, attached, named))
                await asyncio.wait_for(attached.wait(), 10)
                deferred = await _unload_stop(client, session_id)
                await asyncio.sleep(1.5)  # well past the window
                still_running = _running(served.app, session_id)
                agent.gate.set()
                events = await watch
                served.wait_for_slot_release(session_id)
                return deferred, events, still_running

    deferred, events, still_running = asyncio.run(_run())
    assert deferred.json() == {"stopped": False, "deferred": True}
    assert still_running, "an unload stop arriving after the reload's reattach killed the turn"
    assert events and events[-1]["type"] == "answer", events
    moved = _moved(before)
    assert moved["chemclaw_turns_stop_expired_total"] == 0
    assert moved["chemclaw_turns_stop_resumed_total"] == 1


def test_without_a_reattach_the_turn_is_stopped_after_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chemist who really left: the turn runs on past the stop, and ends with the window."""
    monkeypatch.setattr(settings, "service_turn_unload_grace_seconds", 0.4)
    agent = _Ledger()
    before = _counts()

    async def _run() -> tuple[Any, bool, float]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client)
                await _start_and_unload(client, session_id)
                deferred = await _unload_stop(client, session_id)
                asked = time.monotonic()
                running_after_stop = _running(served.app, session_id)
                # The gate is never set: the only way this turn ends is the deferred stop.
                served.wait_for_slot_release(session_id)
                return deferred, running_after_stop, time.monotonic() - asked

    deferred, running_after_stop, took = asyncio.run(_run())
    assert deferred.json() == {"stopped": False, "deferred": True}
    assert running_after_stop, "an unload stop was applied at once rather than deferred"
    assert took < 8, f"the deferred stop never fired ({took:.1f}s)"
    assert _moved(before) == {
        "chemclaw_turns_stop_deferred_total": 1,
        "chemclaw_turns_stop_resumed_total": 0,
        "chemclaw_turns_stop_expired_total": 1,
        "chemclaw_turns_stopped_total": 1,
    }


def test_an_explicit_stop_is_immediate_even_over_a_pending_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Stop button never waits — not for a window an earlier unload opened."""
    monkeypatch.setattr(settings, "service_turn_unload_grace_seconds", 60.0)
    agent = _Ledger()

    async def _run() -> tuple[Any, Any, float]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client)
                await _start_and_unload(client, session_id)
                deferred = await _unload_stop(client, session_id)
                asked = time.monotonic()
                stopped = await client.post(f"/sessions/{session_id}/turn/stop", headers=_as(_ANA))
                served.wait_for_slot_release(session_id)
                return deferred, stopped, time.monotonic() - asked

    deferred, stopped, took = asyncio.run(_run())
    assert deferred.json() == {"stopped": False, "deferred": True}
    assert stopped.status_code == 200 and stopped.json() == {"stopped": True}
    assert took < 10, f"a plain stop waited out the 60 s unload window ({took:.1f}s)"


def test_with_the_window_off_an_unload_stop_is_immediate(monkeypatch: pytest.MonkeyPatch) -> None:
    """`service_turn_unload_grace_seconds=0` is the old unload stop, exactly."""
    monkeypatch.setattr(settings, "service_turn_unload_grace_seconds", 0.0)
    agent = _Ledger()

    async def _run() -> Any:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client)
                await _start_and_unload(client, session_id)
                stopped = await _unload_stop(client, session_id)
                served.wait_for_slot_release(session_id)
                return stopped

    assert asyncio.run(_run()).json() == {"stopped": True}


def test_another_participant_can_neither_defer_the_stop_nor_cancel_it_by_watching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ben, a member, watching Ana's turn does not keep alive a turn Ana's page walked away from."""
    monkeypatch.setattr(settings, "service_turn_unload_grace_seconds", 0.5)
    agent = _Ledger()
    before = _counts()

    async def _run() -> tuple[int, Any, list[dict[str, Any]]]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN)
                await _start_and_unload(client, session_id)
                await _until(lambda: _started(agent, 1))
                refused = (await _unload_stop(client, session_id, who=_BEN)).status_code
                deferred = await _unload_stop(client, session_id)
                attached = asyncio.Event()
                ben = asyncio.create_task(_watch(client, _BEN, session_id, attached))
                await asyncio.wait_for(attached.wait(), 10)
                # Never released: the turn ends only if Ben's view failed to cancel the stop.
                served.wait_for_slot_release(session_id)
                return refused, deferred, await ben

    refused, deferred, ben_saw = asyncio.run(_run())
    assert refused == 403, "a member deferred a stop on somebody else's turn"
    assert deferred.json() == {"stopped": False, "deferred": True}
    assert not any(event.get("type") == "answer" for event in ben_saw), ben_saw
    moved = _moved(before)
    assert moved["chemclaw_turns_stop_resumed_total"] == 0, "a watcher cancelled the sender's stop"
    assert moved["chemclaw_turns_stop_expired_total"] == 1


# --- the window's bounds, on the pump directly --------------------------------------------------


async def _forever() -> AsyncIterator[dict[str, str]]:
    """A turn that runs until something stops it."""
    yield {"event": "token", "data": "working"}
    await asyncio.Event().wait()
    yield {"event": "token", "data": "never"}  # pragma: no cover - unreachable by design


def test_a_second_unload_stop_does_not_move_the_deadline() -> None:
    """One window per stop: repeating the unload stop cannot keep the turn alive."""

    async def _run() -> tuple[bool, bool, bool]:
        turn = DetachableTurn(_forever(), session_id="s-once")
        first = turn.defer_stop(0.3, resumers=frozenset({"ana"}), max_deferrals=3)
        timer = turn._pending_stop
        await asyncio.sleep(0.2)
        again = turn.defer_stop(0.3, resumers=frozenset({"ana"}), max_deferrals=3)
        same_timer = turn._pending_stop is timer
        async with asyncio.timeout(5):
            while turn.running:
                await asyncio.sleep(0.01)
        return first, again, same_timer

    first, again, same_timer = asyncio.run(_run())
    assert first and again, "a pending window should answer as pending"
    assert same_timer, "a repeated unload stop restarted the window"


def test_a_turn_grants_at_most_its_cap_of_deferrals_and_only_a_resumer_cancels_one() -> None:
    """A reload loop restarts the window only so often; past the cap an unload stop is immediate."""

    async def _run() -> list[bool]:
        turn = DetachableTurn(_forever(), session_id="s-cap")
        seen: list[bool] = []
        for _ in range(2):
            seen.append(turn.defer_stop(30, resumers=frozenset({"ana"}), max_deferrals=2))
            seen.append(turn.resume("ben"))  # a stranger to the stop: no effect
            seen.append(turn.resume("ana"))
        seen.append(turn.defer_stop(30, resumers=frozenset({"ana"}), max_deferrals=2))
        seen.append(turn.stop_pending)
        await turn.stop()
        return seen

    assert asyncio.run(_run()) == [True, False, True, True, False, True, False, False]


def test_a_turn_that_ends_inside_the_window_takes_its_pending_stop_with_it() -> None:
    """A turn finishing on its own is not then "stopped" by a timer nobody cancelled."""
    before = _counts()

    async def _short() -> AsyncIterator[dict[str, str]]:
        await asyncio.sleep(0.05)
        yield {"event": "token", "data": "done"}

    async def _run() -> bool:
        turn = DetachableTurn(_short(), session_id="s-ends")
        turn.defer_stop(0.3, resumers=frozenset({"ana"}), max_deferrals=1)
        await asyncio.sleep(0.6)
        return turn.stop_pending

    assert asyncio.run(_run()) is False
    assert _moved(before)["chemclaw_turns_stop_expired_total"] == 0


def test_a_view_open_at_the_stop_that_closes_in_the_window_does_not_keep_the_turn() -> None:
    """A page that was itself following the turn unloads with its watch still open.

    That view closes within the window, so the stop lands — it is read at expiry, not at the stop.
    A view still open at expiry keeps the turn only when it is a resumer's.
    """

    async def _run() -> tuple[bool, bool]:
        leaving = DetachableTurn(_forever(), session_id="s-leaving")
        dying = leaving.watch("ana")
        assert dying is not None
        leaving.defer_stop(0.2, resumers=frozenset({"ana"}), max_deferrals=3)
        dying.close()  # the unloading page's socket goes
        stranger = DetachableTurn(_forever(), session_id="s-stranger")
        assert stranger.watch("ben") is not None
        stranger.defer_stop(0.2, resumers=frozenset({"ana"}), max_deferrals=3)
        await asyncio.sleep(0.6)
        return leaving.running, stranger.running

    leaving_running, stranger_running = asyncio.run(_run())
    assert not leaving_running, "a view that closed inside the window kept the turn alive"
    assert not stranger_running, "a non-resumer's view kept the turn alive past the window"
