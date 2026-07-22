"""The job→session push-back channel: tailer logic (unit) + Postgres round-trip (skips offline).

The tailer's loop is proven with an injected `claim` (no database), so its ordering and
consume-once behavior are verified deterministically; the DB claim — including its concurrent
no-double-claim guarantee — is proven against a real database when one is present (F3-T2, COR-4).
"""

import asyncio

from agents.session_events import (
    SessionEvent,
    claim_unconsumed,
    record_session_event,
    stream_new_events,
)
from tests.pg import migrated_db_or_skip


def test_tailer_yields_claimed_events_once() -> None:
    """The tailer yields each claimed event once, per poll, bounded by max_polls."""
    batches = [
        [SessionEvent(event_id=1, session_id="s", kind="job_completed", payload={"job_id": "j1"})],
        [],
    ]
    claimed_for: list[str] = []

    async def _claim(session_id: str) -> list[SessionEvent]:
        claimed_for.append(session_id)
        return batches.pop(0) if batches else []

    async def _run() -> list[SessionEvent]:
        seen = []
        async for event in stream_new_events("s", poll_seconds=0, max_polls=2, claim=_claim):
            seen.append(event)
        return seen

    seen = asyncio.run(_run())
    assert [e.payload["job_id"] for e in seen] == ["j1"]
    assert claimed_for == ["s", "s"]  # claimed once per poll (the claim is the consume step)


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


def test_record_and_claim_round_trip() -> None:
    """Recording an event makes it claimable exactly once; a second claim returns nothing."""

    async def _run() -> None:
        await migrated_db_or_skip()
        session_id = "sess-f3t2-roundtrip"
        await claim_unconsumed(session_id)  # clear any prior rows for this session

        await record_session_event(session_id, "job_completed", {"job_id": "j-42"})
        claimed = await claim_unconsumed(session_id)
        assert [e.payload["job_id"] for e in claimed] == ["j-42"]
        # The claim consumed it, so a second claim sees nothing (never re-delivered).
        assert await claim_unconsumed(session_id) == []

    asyncio.run(_run())


def test_concurrent_claims_never_double_deliver() -> None:
    """Two tailers claiming the same session concurrently split the rows, never overlap (COR-4)."""

    async def _run() -> None:
        await migrated_db_or_skip()
        session_id = "sess-f3t2-concurrent"
        await claim_unconsumed(session_id)  # start clean
        for i in range(20):
            await record_session_event(session_id, "job_completed", {"job_id": f"j-{i}"})

        # Two claimers race on the same session; SKIP LOCKED must partition the rows between them.
        first, second = await asyncio.gather(
            claim_unconsumed(session_id), claim_unconsumed(session_id)
        )
        ids_first = {e.event_id for e in first}
        ids_second = {e.event_id for e in second}
        assert ids_first.isdisjoint(ids_second)  # no row delivered to both tailers
        assert len(ids_first) + len(ids_second) == 20  # every row delivered exactly once
        assert await claim_unconsumed(session_id) == []  # all consumed

    asyncio.run(_run())
