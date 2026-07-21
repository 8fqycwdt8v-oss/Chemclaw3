"""The job→session push-back channel: tailer logic (unit) + Postgres round-trip (skips offline).

The tailer's loop is proven with injected `fetch`/`mark` (no database), so its ordering and
consume-once behavior are verified deterministically; the DB writer/reader are proven against a real
database when one is present (F3-T2).
"""

import asyncio

from agents.session_events import (
    SessionEvent,
    fetch_unconsumed,
    mark_consumed,
    record_session_event,
    stream_new_events,
)
from tests.pg import migrated_db_or_skip


def test_tailer_yields_events_then_marks_them_consumed() -> None:
    """The tailer yields each fetched event once and marks the batch consumed (bounded by polls)."""
    batches = [
        [SessionEvent(event_id=1, session_id="s", kind="job_completed", payload={"job_id": "j1"})],
        [],
    ]
    marked: list[list[int]] = []

    async def _fetch(_session_id: str) -> list[SessionEvent]:
        return batches.pop(0) if batches else []

    async def _mark(ids: object) -> None:
        marked.append(list(ids))  # type: ignore[arg-type]

    async def _run() -> list[SessionEvent]:
        seen = []
        async for event in stream_new_events(
            "s", poll_seconds=0, max_polls=2, fetch=_fetch, mark=_mark
        ):
            seen.append(event)
        return seen

    seen = asyncio.run(_run())
    assert [e.payload["job_id"] for e in seen] == ["j1"]
    assert marked == [[1]]  # the delivered event was marked consumed, the empty poll marked nothing


def test_session_event_requires_session_and_kind() -> None:
    """The event model rejects an empty session id or kind (a push-back with no addressee)."""
    import pytest

    with pytest.raises(ValueError):
        SessionEvent(session_id="", kind="x")
    with pytest.raises(ValueError):
        SessionEvent(session_id="s", kind="")


def test_notify_activity_forwards_to_the_channel(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The Temporal notify activity forwards its typed input to the channel writer (no DB)."""
    import workflows.notify as notify
    from workflows.notify import SessionEventInput, record_session_event_activity

    captured: dict[str, object] = {}

    async def _fake_record(session_id: str, kind: str, payload: object = None, **_: object) -> None:
        captured.update(session_id=session_id, kind=kind, payload=payload)

    monkeypatch.setattr(notify, "record_session_event", _fake_record)

    asyncio.run(
        record_session_event_activity(
            SessionEventInput(session_id="s7", kind="job_completed", payload={"job_id": "j9"})
        )
    )
    assert captured == {"session_id": "s7", "kind": "job_completed", "payload": {"job_id": "j9"}}


def test_record_fetch_and_consume_round_trip() -> None:
    """Recording an event makes it fetchable; consuming it removes it from the unconsumed set."""

    async def _run() -> None:
        await migrated_db_or_skip()
        session_id = "sess-f3t2-roundtrip"
        # Clear any prior rows for this session by consuming them first.
        await mark_consumed([e.event_id for e in await fetch_unconsumed(session_id) if e.event_id])

        await record_session_event(session_id, "job_completed", {"job_id": "j-42"})
        pending = await fetch_unconsumed(session_id)
        assert [e.payload["job_id"] for e in pending] == ["j-42"]

        await mark_consumed([e.event_id for e in pending if e.event_id is not None])
        assert await fetch_unconsumed(session_id) == []

    asyncio.run(_run())
