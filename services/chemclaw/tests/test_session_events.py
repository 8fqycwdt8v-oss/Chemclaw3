"""The job→session push-back channel: tailer logic (unit) + Postgres round-trip (skips offline).

The tailer's loop is proven with an injected `claim` (no database), so its ordering and
consume-once behavior are verified deterministically; the DB claim — including its concurrent
no-double-claim guarantee — is proven against a real database when one is present (F3-T2, COR-4).
"""

import asyncio
from uuid import uuid4

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


def test_kind_scoped_claim_leaves_other_kinds_unconsumed() -> None:
    """A `kinds`-scoped claim consumes only matching rows.

    The claim is destructive, so a kind-selective consumer must filter in the claim itself or it
    would silently destroy other consumers' events (the front door claims only `job_completed`
    this way).
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        session_id = "sess-f3t2-kinds"
        await claim_unconsumed(session_id)  # start clean

        await record_session_event(session_id, "job_completed", {"job_id": "j-1"})
        await record_session_event(session_id, "eval_drift", {"metric": "faithfulness"})

        claimed = await claim_unconsumed(session_id, kinds=["job_completed"])
        assert [e.kind for e in claimed] == ["job_completed"]
        # The other kind was NOT destructively claimed — its own consumer can still get it.
        leftover = await claim_unconsumed(session_id)
        assert [e.kind for e in leftover] == ["eval_drift"]

    asyncio.run(_run())


def test_tailer_reuses_one_connection_across_polls(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The default (database) tailer opens one connection for the stream's lifetime.

    Per-poll connects would churn a fresh Postgres connection every interval for every stream,
    an exhaustion vector on the shared session store.
    """
    from chemclaw import db

    connects: list[str] = []
    real_connect = db.connect

    async def _counting_connect(dsn: str, **kwargs: object) -> object:
        connects.append(dsn)
        return await real_connect(dsn, **kwargs)  # type: ignore[arg-type]

    async def _run() -> None:
        await migrated_db_or_skip()
        session_id = "sess-f3t2-connection-reuse"
        await claim_unconsumed(session_id)  # start clean
        for i in range(3):
            await record_session_event(session_id, "job_completed", {"job_id": f"j-{i}"})

        monkeypatch.setattr(db, "connect", _counting_connect)
        seen = [event async for event in stream_new_events(session_id, poll_seconds=0, max_polls=3)]
        assert len(seen) == 3
        assert len(connects) == 1  # one connection for the whole 3-poll stream

    asyncio.run(_run())


def test_duplicate_dedupe_key_inserts_once() -> None:
    """A retried insert with the same dedupe key is a no-op — one notification, not two.

    This is the at-least-once activity retry scenario: the first insert committed but the worker
    died before acking, so Temporal re-runs the activity with the identical input. The unique
    index on `dedupe_key` must absorb the retry; a distinct key (a genuinely different event)
    still appends.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        session_id = "sess-f3t2-dedupe"
        await claim_unconsumed(session_id)  # start clean

        # Unique per test run: the index is global and permanent, exactly like a real
        # workflow run id — a previous run's keys must not absorb this run's inserts.
        run = uuid4().hex
        key = f"wf-qm-1:{run}:job_completed:abc"
        await record_session_event(session_id, "job_completed", {"job_id": "j-1"}, dedupe_key=key)
        await record_session_event(session_id, "job_completed", {"job_id": "j-1"}, dedupe_key=key)
        other_key = f"wf-qm-1:{run}:job_completed:def"
        await record_session_event(
            session_id, "job_completed", {"job_id": "j-2"}, dedupe_key=other_key
        )
        claimed = await claim_unconsumed(session_id)
        assert [e.payload["job_id"] for e in claimed] == ["j-1", "j-2"]  # the retry deduped

    asyncio.run(_run())


def test_null_dedupe_key_keeps_plain_append() -> None:
    """Writers without retry semantics (no key) still append unconditionally."""

    async def _run() -> None:
        await migrated_db_or_skip()
        session_id = "sess-f3t2-nokey"
        await claim_unconsumed(session_id)

        await record_session_event(session_id, "job_completed", {"job_id": "j-1"})
        await record_session_event(session_id, "job_completed", {"job_id": "j-1"})
        assert len(await claim_unconsumed(session_id)) == 2

    asyncio.run(_run())


def test_dedupe_key_derivation_is_stable_and_event_specific() -> None:
    """The workflow-side key is retry-stable but distinguishes runs, kinds, and payloads.

    Same inputs → same key (an activity retry must land on the unique index); a different run of
    the same workflow id, a different kind, or a different payload (one drift alert per metric in
    one run) → different keys, so genuinely distinct events never dedupe each other.
    """
    from workflows.notify import _dedupe_key

    base = _dedupe_key("wf-1", "run-1", "job_completed", {"job_id": "j", "energy": -1.5})
    assert base == _dedupe_key("wf-1", "run-1", "job_completed", {"energy": -1.5, "job_id": "j"})
    assert base != _dedupe_key("wf-1", "run-2", "job_completed", {"job_id": "j", "energy": -1.5})
    assert base != _dedupe_key("wf-1", "run-1", "eval_drift", {"job_id": "j", "energy": -1.5})
    assert base != _dedupe_key("wf-1", "run-1", "job_completed", {"job_id": "k", "energy": -1.5})
