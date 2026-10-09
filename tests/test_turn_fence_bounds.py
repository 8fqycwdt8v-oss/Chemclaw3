"""What a fenced write and a takeover each wait for, and what an unconfirmed claim does.

`D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted`. A checkpoint write under a claim
holds a lock on the claim row until it commits; a writer that stalls inside its transaction must be
cut off by the database, not by TCP, and a takeover must answer "held" rather than raise while it
waits. A claim that is still the holder's but short of its margin is neither owned nor lost.
Each claim has its control beside it.
"""

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from chemclaw.agent import checkpointer as checkpointer_module
from chemclaw.agent.checkpointer import SchemaStampedSaver, checkpointer, close_checkpointer
from chemclaw.agent.session_store import SessionOwnerStore, SessionTurnClaims
from chemclaw.api.state import _owned_or_renewed
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.turn_fence import (
    Claim,
    ClaimUnverifiable,
    TurnFence,
    reset_turn_fence,
    set_turn_fence,
)
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

_INSERT_WRITE = (
    "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, task_path,"
    " idx, channel, type, blob) VALUES (%s, '', 'c1', 't', '', 0, 'x', 't', %s)"
)


@pytest.fixture
def durable(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Postgres store, migrated, with the checkpointer's tables."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    asyncio.run(close_checkpointer())
    yield
    asyncio.run(close_checkpointer())


async def _held_session(holder: str, lease: float) -> str:
    session_id = f"sess-bound-{uuid.uuid4().hex[:10]}"
    await SessionOwnerStore().record(session_id, "ana", None)
    assert await SessionTurnClaims().claim(session_id, holder, lease, actor="ana")
    return session_id


async def _set_expiry(session_id: str, seconds_from_now: float) -> None:
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE session_turns SET expires_at = now() + make_interval(secs => %s) "
            "WHERE session_id = %s",
            (seconds_from_now, session_id),
        )
        await conn.commit()


async def _expiry_in(session_id: str) -> float:
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT extract(epoch FROM expires_at - now()) FROM session_turns "
            "WHERE session_id = %s",
            (session_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return float(row[0])


class _Cancelled:
    """A turn's cancel hook that remembers it was called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _fence_over(claims: Any, session_id: str, holder: str, cancel: _Cancelled) -> TurnFence:
    lease, margin = 6.0, 2.0
    return TurnFence(
        Claim(session_id, holder),
        lambda: _owned_or_renewed(claims, session_id, holder, lease, margin),
        cancel,
    )


async def test_a_claim_short_of_its_margin_is_renewed_and_the_turn_goes_on(durable: None) -> None:
    """Two refreshes failed, so the claim is short; the next check renews it and nothing is lost."""
    session_id = await _held_session("pod-a:1", lease=6)
    claims = SessionTurnClaims()
    await _set_expiry(session_id, 1.0)  # under the 2 s margin, as after two missed refreshes
    assert await claims.owns(session_id, "pod-a:1", 2.0) is None
    cancel = _Cancelled()
    fence = _fence_over(claims, session_id, "pod-a:1", cancel)

    assert await fence.hold() is True

    assert not fence.lost and cancel.calls == 0
    assert await _expiry_in(session_id) > 4.0, "the check did not renew the claim"


async def test_a_claim_short_of_its_margin_that_cannot_be_renewed_is_unconfirmed_not_lost(
    durable: None,
) -> None:
    """The store is down for the renewal too: the effect is refused, the turn is not discarded."""
    session_id = await _held_session("pod-a:1", lease=6)
    real = SessionTurnClaims()
    await _set_expiry(session_id, 1.0)

    class Down:
        owns = staticmethod(real.owns)

        async def refresh(self, *_args: Any) -> bool:
            raise ConnectionError("database blip")

    cancel = _Cancelled()
    fence = _fence_over(Down(), session_id, "pod-a:1", cancel)

    with pytest.raises(ClaimUnverifiable):
        await fence.hold()

    assert not fence.lost and cancel.calls == 0, "a failed refresh was read as a takeover"


async def test_a_claim_that_lapsed_or_is_another_holders_is_lost(durable: None) -> None:
    """The control for the two tests above: the same check, answered by the database as lost."""
    claims = SessionTurnClaims()
    lapsed = await _held_session("pod-a:1", lease=6)
    await _set_expiry(lapsed, -1.0)
    cancel = _Cancelled()
    fence = _fence_over(claims, lapsed, "pod-a:1", cancel)

    assert await fence.hold() is False
    assert fence.lost and cancel.calls == 1

    taken = await _held_session("pod-b:1", lease=6)
    stale = _fence_over(claims, taken, "pod-a:1", _Cancelled())
    assert await stale.hold() is False and stale.lost


async def test_a_lapsed_lease_cannot_be_refreshed_by_its_holder(durable: None) -> None:
    """Nobody took the claim, but a holder that woke after its lease ran out cannot extend it."""
    session_id = await _held_session("pod-a:1", lease=6)
    claims = SessionTurnClaims()

    assert await claims.refresh(session_id, "pod-a:1", 6.0) is True, "a live claim refreshes"

    await _set_expiry(session_id, -1.0)
    assert await claims.refresh(session_id, "pod-a:1", 6.0) is False
    assert await _expiry_in(session_id) < 0, "the lapsed lease was extended"


async def test_a_takeover_waiting_on_a_write_answers_held_instead_of_raising(
    durable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait is bounded to half a lease and maps to the answer a held slot gives."""
    monkeypatch.setattr(settings, "service_turn_claim_lease_seconds", 0.6)
    session_id = await _held_session("pod-a:1", lease=60)
    await _set_expiry(session_id, -1.0)
    dsn = settings.session_store_dsn or settings.postgres_dsn
    claims = SessionTurnClaims()

    async with await psycopg.AsyncConnection.connect(dsn) as writer:
        await writer.execute(
            "SELECT 1 FROM session_turns WHERE session_id = %s FOR SHARE", (session_id,)
        )
        started = time.monotonic()
        assert await claims.claim(session_id, "pod-b:1", 60.0) is False
        assert time.monotonic() - started < 2.0, "the takeover waited out its statement timeout"
        await writer.rollback()

    assert await claims.claim(session_id, "pod-b:1", 60.0) is True, (
        "the control: nothing in the way"
    )


async def _lapsed(claims: SessionTurnClaims, session_id: str, holder: str) -> None:
    while await claims.owns(session_id, holder) is not False:
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("bounded", [True, False], ids=["bounded", "unbounded-control"])
async def test_a_writer_stalled_in_its_transaction_is_cut_off_and_the_takeover_proceeds(
    durable: None, monkeypatch: pytest.MonkeyPatch, bounded: bool
) -> None:
    """The database ends a stalled write after a third of the lease; a takeover waits for that.

    The writer holds the claim row, goes quiet inside its transaction (as a stopped process does)
    and the claim lapses under it. The takeover must complete within the bound, cleanly, and the
    stalled writer must find its transaction gone and commit nothing. Without the bound (the
    control) the takeover gives up at its own wait and the writer is still holding the row.
    """
    monkeypatch.setattr(settings, "service_turn_claim_lease_seconds", 6.0)  # bound 2 s, wait 3 s
    if not bounded:
        monkeypatch.setattr(checkpointer_module, "fenced_write_bound", lambda lease: 600.0)
    session_id = await _held_session("pod-a:1", lease=1.0)
    saver = await checkpointer()
    assert saver is not None
    claims = SessionTurnClaims()
    inside = asyncio.Event()
    outcome: list[BaseException | None] = []

    async def stalled_writer() -> None:
        token = set_turn_fence(TurnFence(Claim(session_id, "pod-a:1"), _always(True), lambda: None))
        try:
            async with saver._cursor(pipeline=True) as cur:
                await cur.execute("SELECT 1")  # the transaction is open and holds the claim row
                inside.set()
                await asyncio.sleep(8)  # the process is stopped here
                await cur.execute(_INSERT_WRITE, (session_id, b"late"))
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)
            raise
        finally:
            reset_turn_fence(token)

    writer = asyncio.create_task(stalled_writer())
    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        await _lapsed(claims, session_id, "pod-a:1")

        started = time.monotonic()
        taken = await claims.claim(session_id, "pod-b:1", 60.0)
        waited = time.monotonic() - started

        if bounded:
            assert taken is True, "the takeover did not get past the stalled writer"
            assert waited < 3.0, f"the takeover waited {waited:.1f}s, longer than its own bound"
            with pytest.raises(psycopg.errors.IdleInTransactionSessionTimeout):
                await asyncio.wait_for(writer, timeout=20)
            assert await _write_count(session_id) == 0, "the stalled write was committed"
            assert await claims.refresh(session_id, "pod-a:1", 60.0) is False
        else:
            assert taken is False, "the control: the takeover passed a writer still holding the row"
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        await close_checkpointer()


def _always(answer: bool) -> Any:
    async def owns() -> bool:
        return answer

    return owns


async def _write_count(session_id: str) -> int:
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (session_id,)
        )
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def test_a_second_failed_ask_is_not_asked_again_for_a_while() -> None:
    """An outage costs one wait, not one per model call; the control asks every time."""
    asked = 0

    async def down() -> bool:
        nonlocal asked
        asked += 1
        raise ConnectionError("pool timeout")

    quiet = TurnFence(Claim("s", "h"), down, lambda: None, quiet_seconds=60.0)
    for _ in range(3):
        with pytest.raises(ClaimUnverifiable):
            await quiet.hold()
    assert asked == 2, "the outage was asked about again inside its quiet window"

    asked = 0
    eager = TurnFence(Claim("s", "h"), down, lambda: None)
    for _ in range(3):
        with pytest.raises(ClaimUnverifiable):
            await eager.hold()
    assert asked == 6


async def test_a_claim_short_of_its_margin_is_unverifiable_to_the_fence() -> None:
    async def short() -> bool | None:
        return None

    cancel = _Cancelled()
    fence = TurnFence(Claim("s", "h"), short, cancel)

    with pytest.raises(ClaimUnverifiable):
        await fence.hold()

    assert not fence.lost and cancel.calls == 0


def test_a_saver_over_a_single_connection_is_refused_when_it_is_built() -> None:
    """A fenced write needs a transaction of its own; the error is at start-up, not mid-turn."""
    with pytest.raises(ChemclawError, match="AsyncConnectionPool"):
        SchemaStampedSaver(object())
