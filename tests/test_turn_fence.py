"""A turn that lost its session stops, and a turn that merely could not ask does not.

`D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted`. The fence is consulted by the
heartbeat, the tool chain, the model chain, the end of `run_turn` and the checkpointer; these tests
drive each of them without a process boundary, so the cases a stalled process makes timing-dependent
are exact here. The same claims with real processes are `tests/test_turn_survives_pod.py`.
"""

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import NodeCancelledError

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.checkpointer import checkpointer, close_checkpointer
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_store import (
    PostgresHistoryProvider,
    SessionOwnerStore,
    SessionTurnClaims,
)
from chemclaw.agent.state import turn_config
from chemclaw.agent.tool_authz import refuse_when_claim_lost
from chemclaw.agent.turn_graph import build_turn_agent
from chemclaw.agent.turn_resume import ResumePoint, judge
from chemclaw.api.events import Event
from chemclaw.api.runner import run_turn
from chemclaw.api.state import _hold_turn_claim
from chemclaw.core import bookkeeping, db
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.turn_fence import (
    Claim,
    ClaimUnverifiable,
    TurnFence,
    TurnFenceLost,
    reset_turn_fence,
    set_turn_fence,
)
from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.replica_graph import ProbeModel, probe_tools

#: How a graph reports a node that ended the turn by cancelling: the graph wraps the cancellation,
#: and `run_turn` reads the lost fence to tell it from a failure.
_ENDED = (asyncio.CancelledError, NodeCancelledError)


def _fence(*answers: bool | Exception, session: str = "s", holder: str = "h") -> TurnFence:
    """A fence whose store answers `answers` in turn, then the last one for ever."""
    queue = list(answers)
    cancelled: list[int] = []
    asked: list[int] = []

    async def owns() -> bool:
        asked.append(1)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    fence = TurnFence(Claim(session, holder), owns, lambda: cancelled.append(1))
    fence.cancelled = cancelled  # type: ignore[attr-defined]
    fence.asked = asked  # type: ignore[attr-defined]
    return fence


async def test_a_claim_the_store_denies_is_lost_and_ends_the_turn() -> None:
    fence = _fence(False)

    assert await fence.hold() is False
    assert fence.lost and fence.cancelled == [1]  # type: ignore[attr-defined]
    assert await fence.hold() is False, "a lost claim is lost for good"


async def test_a_store_that_cannot_answer_twice_is_unverifiable_and_loses_nothing() -> None:
    """Fail closed for the effect, not for the turn: the caller refuses, the fence stays."""
    fence = _fence(RuntimeError("pool timeout"))

    with pytest.raises(ClaimUnverifiable):
        await fence.hold()

    assert not fence.lost and fence.cancelled == []  # type: ignore[attr-defined]


async def test_one_failed_question_is_asked_again_before_it_counts() -> None:
    fence = _fence(RuntimeError("blip"), True)

    assert await fence.hold() is True
    assert not fence.lost


def _request(name: str) -> Any:
    return SimpleNamespace(tool_call={"name": name, "args": {}, "id": "c1"})


async def _run_chain(fence: TurnFence | None, name: str) -> list[str]:
    ran: list[str] = []

    async def handler(request: Any) -> str:
        ran.append(request.tool_call["name"])
        return "done"

    token = set_turn_fence(fence)
    try:
        await refuse_when_claim_lost.awrap_tool_call(_request(name), handler)  # type: ignore[arg-type]
    finally:
        reset_turn_fence(token)
    return ran


async def test_a_call_that_may_act_is_refused_when_the_claim_is_lost() -> None:
    """Including a `task` helper, which is not on the repeatable list, and an unknown tool."""
    for name in ("record_knowledge_note", "task", "a_tool_nobody_classified"):
        fence = _fence(False)
        with pytest.raises(asyncio.CancelledError):
            await _run_chain(fence, name)
        assert fence.lost, name


async def test_a_call_that_cannot_be_confirmed_is_withheld_with_a_refusal_the_model_reads() -> None:
    fence = _fence(RuntimeError("pool timeout"))

    with pytest.raises(SubsystemUnavailableError):
        await _run_chain(fence, "record_knowledge_note")

    assert not fence.lost, "the turn goes on; only this effect did not happen"


async def test_a_repeatable_call_is_not_checked_and_a_held_claim_lets_a_write_through() -> None:
    """Controls: the check costs nothing for a read and refuses nothing it holds."""
    asked = _fence(False)
    assert await _run_chain(asked, "find_notes") == ["find_notes"]
    assert not asked.lost

    assert await _run_chain(_fence(True), "record_knowledge_note") == ["record_knowledge_note"]
    assert await _run_chain(None, "record_knowledge_note") == ["record_knowledge_note"]


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The probe tools classified as a manifest would, marking into `tmp_path`."""
    from chemclaw.connectors import registry

    monkeypatch.setattr(registry, "state_changing_tool_names", lambda: ["probe_act"])
    monkeypatch.setattr(registry, "read_only_tool_names", lambda: ["probe_read"])
    side_effecting_tools.cache_clear()
    marks = tmp_path / "marks"
    marks.mkdir()
    monkeypatch.setenv("REPLICA_MARKS", str(marks))
    monkeypatch.setenv("REPLICA_GATE", str(tmp_path))
    yield marks
    side_effecting_tools.cache_clear()


def _executed(marks: Path) -> list[str]:
    return sorted(path.name.split(".")[1] for path in marks.iterdir())


async def _drive(plan: str, fence: TurnFence | None) -> BaseException | None:
    """One real graph turn of `plan` under `fence`; the exception that ended it, if any."""
    graph = build_turn_agent(
        ProbeModel(),
        connectors=probe_tools(),
        checkpointer=InMemorySaver(),
        audit_sink=NullAuditSink(),
    )
    token = set_turn_fence(fence)
    try:
        async for _ in graph.astream(
            {"messages": [HumanMessage(content=f"{plan} tag=fence")]},
            turn_config("fence-thread"),
            stream_mode=["updates"],
        ):
            pass
    except BaseException as exc:
        return exc
    finally:
        reset_turn_fence(token)
    return None


async def test_every_one_of_parallel_calls_is_checked_not_only_the_first(probes: Path) -> None:
    """Two state-changing calls in one assistant message: each asks, and the second is refused."""
    fence = _fence(True, False)

    ended = await _drive("q steps=acts", fence)

    assert isinstance(ended, _ENDED), ended
    assert len(fence.asked) == 2, "a call was not checked"  # type: ignore[attr-defined]
    assert fence.lost and _executed(probes).count("tool-probe_act-1") <= 1, "a call ran past it"


async def test_parallel_calls_all_run_while_the_claim_is_held(probes: Path) -> None:
    """The control for the test above: the same turn under a held claim runs both."""
    assert await _drive("q steps=acts", _fence(True)) is None
    assert _executed(probes).count("tool-probe_act-1") == 2


async def test_a_lost_claim_ends_a_turn_before_its_model_call(probes: Path) -> None:
    ended = await _drive("q steps=read", _fence(False))

    assert isinstance(ended, _ENDED), ended
    assert _executed(probes) == [], "the model was asked after the turn lost its session"


async def test_a_claim_that_cannot_be_confirmed_does_not_stop_the_model(probes: Path) -> None:
    assert await _drive("q steps=read", _fence(RuntimeError("pool timeout"))) is None
    assert "model-1" in _executed(probes)


class _Claims:
    """A claim store whose refresh does what a test tells it to."""

    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.calls = 0

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        self.calls += 1
        return bool(await self.behaviour(self.calls))


async def _beat(claims: _Claims, fence: TurnFence, lease: float, seconds: float) -> None:
    beat = asyncio.create_task(_hold_turn_claim(claims, "s", lease, "h", fence))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(asyncio.shield(beat), timeout=seconds)
    except TimeoutError:
        pass
    finally:
        beat.cancel()


async def test_a_refresh_that_matches_no_row_loses_the_turn_at_once() -> None:
    async def taken(_call: int) -> bool:
        return False

    fence = _fence(True)
    await _beat(_Claims(taken), fence, lease=0.3, seconds=1.0)

    assert fence.lost


async def test_refreshes_that_fail_for_a_whole_lease_lose_the_turn() -> None:
    async def down(_call: int) -> bool:
        raise ConnectionError("database down")

    fence = _fence(True)
    await _beat(_Claims(down), fence, lease=0.3, seconds=2.0)

    assert fence.lost, "a turn that cannot show it holds its session went on"


async def test_a_refresh_that_hangs_is_bounded_and_counts_as_a_failure() -> None:
    async def hang(_call: int) -> bool:
        await asyncio.sleep(60)
        return True

    fence = _fence(True)
    await _beat(_Claims(hang), fence, lease=0.3, seconds=2.0)

    assert fence.lost


async def test_one_failed_refresh_among_good_ones_loses_nothing() -> None:
    """Control: the timer counts from the last success, so a blip is only a log line."""

    async def blip(call: int) -> bool:
        if call == 2:
            raise ConnectionError("blip")
        return True

    fence = _fence(True)
    claims = _Claims(blip)
    await _beat(claims, fence, lease=0.3, seconds=1.0)

    assert claims.calls > 3 and not fence.lost


@pytest.fixture
def durable(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Postgres store, migrated, with the checkpointer's tables."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    asyncio.run(close_checkpointer())
    yield
    asyncio.run(close_checkpointer())


async def _held_session(holder: str, lease: float = 60) -> str:
    session_id = f"sess-fence-{uuid.uuid4().hex[:10]}"
    await SessionOwnerStore().record(session_id, "ana", None)
    assert await SessionTurnClaims().claim(session_id, holder, lease, actor="ana")
    return session_id


async def _checkpoint_count(session_id: str) -> int:
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (session_id,)
        )
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def test_a_checkpoint_is_written_only_by_the_holder_of_the_claim(durable: None) -> None:
    """The fenced write, with no timing in it: the claim is B's, and A's write finds none.

    Every control is the same call. Under A's fence the write is refused and the fence is lost;
    under B's fence, which is the holder, and under no fence, it lands.
    """
    session_id = await _held_session("pod-b:1")
    saver = await checkpointer()
    assert saver is not None
    config: Any = {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
    metadata: Any = {"source": "input", "step": 0, "parents": {}}

    async def write(fence: TurnFence | None) -> None:
        token = set_turn_fence(fence)
        try:
            await saver.aput(config, empty_checkpoint(), metadata, {})
        finally:
            reset_turn_fence(token)

    old_holder = _fence(True, session=session_id, holder="pod-a:1")
    with pytest.raises(TurnFenceLost):
        await write(old_holder)
    assert old_holder.lost and await _checkpoint_count(session_id) == 0

    await write(_fence(True, session=session_id, holder="pod-b:1"))
    assert await _checkpoint_count(session_id) == 1
    await write(None)
    assert await _checkpoint_count(session_id) == 2


async def test_a_pending_write_is_fenced_as_a_checkpoint_is(durable: None) -> None:
    session_id = await _held_session("pod-b:1")
    saver = await checkpointer()
    assert saver is not None
    checkpoint = empty_checkpoint()
    written: Any = await saver.aput(
        {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}},
        checkpoint,
        {"source": "input", "step": 0, "parents": {}},
        {},
    )

    old_holder = _fence(True, session=session_id, holder="pod-a:1")
    token = set_turn_fence(old_holder)
    try:
        with pytest.raises(TurnFenceLost):
            await saver.aput_writes(written, [("messages", [AIMessage(content="x")])], "task-1")
    finally:
        reset_turn_fence(token)

    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (session_id,)
        )
        row = await cursor.fetchone()
    assert row is not None and int(row[0]) == 0


async def test_the_ownership_check_wants_a_margin_of_the_lease_left(durable: None) -> None:
    """A claim a moment from its end is not trusted for an effect; the holder's usual is."""
    session_id = await _held_session("pod-a:1", lease=6)
    claims = SessionTurnClaims()

    assert await claims.owns(session_id, "pod-a:1")
    assert await claims.owns(session_id, "pod-a:1", margin_seconds=2.0), "two thirds are left"
    assert not await claims.owns(session_id, "pod-a:1", margin_seconds=30.0)
    assert not await claims.owns(session_id, "someone-else:1")


def _factory(model: Any = None, **extra: Any) -> Any:
    def build(**kwargs: Any) -> Any:
        kwargs["connectors"] = [*(kwargs.get("connectors") or []), *probe_tools()]
        return build_turn_agent(model or ProbeModel(), **kwargs)

    return build


async def _turn(
    session_id: str, message: str, fence: TurnFence | None, **extra: Any
) -> tuple[list[Event], BaseException | None]:
    events: list[Event] = []
    ended: BaseException | None = None
    try:
        async for event in run_turn(
            TurnSession(session_id=session_id),
            message,
            actor="ana",
            history=PostgresHistoryProvider(),
            connectors=[],
            graph_factory=extra.pop("graph_factory", _factory()),
            fence=fence,
            **extra,
        ):
            events.append(event)
    except BaseException as exc:
        ended = exc
    await asyncio.gather(*bookkeeping.pending(), return_exceptions=True)
    return events, ended


async def _costs(session_id: str) -> list[str]:
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT outcome FROM turn_costs WHERE session_id = %s", (session_id,)
        )
        return [str(row[0]) for row in await cursor.fetchall()]


async def _status(session_id: str) -> str | None:
    return await PostgresHistoryProvider().latest_turn_status(session_id)


async def test_a_finished_turn_whose_claim_cannot_be_confirmed_is_answered_and_booked(
    durable: None, probes: Path
) -> None:
    """A store error at the last look is not a takeover: the answer ships and is booked."""
    session_id = await _held_session("pod-a:1")
    fence = _fence(RuntimeError("pool timeout"), session=session_id, holder="pod-a:1")

    events, ended = await _turn(session_id, "q steps= tag=a", fence)

    assert ended is None and events[-1].type == "answer", events
    assert await _costs(session_id) == ["answered"]
    assert await _status(session_id) == "done"


async def test_a_turn_that_lost_its_claim_books_nothing_and_settles_nothing(
    durable: None, probes: Path
) -> None:
    """Control for the test above: a denied claim ends the turn with no trace in the ledger."""
    session_id = await _held_session("pod-a:1")
    fence = _fence(False, session=session_id, holder="pod-a:1")

    events, ended = await _turn(session_id, "q steps= tag=b", fence)

    assert isinstance(ended, asyncio.CancelledError) and not events
    assert await _costs(session_id) == []
    assert await _status(session_id) == "running", "the new owner's question was settled by the old"


async def _dead_turn_thread(session_id: str, question_id: str, *, acted: bool) -> None:
    """A checkpointed thread as a killed pod leaves it: the question and one finished read."""
    saver = await checkpointer()
    graph = build_turn_agent(
        ProbeModel(), connectors=probe_tools(), checkpointer=saver, audit_sink=NullAuditSink()
    )
    read = {"name": "probe_read", "args": {"n": 1, "tag": "dead"}, "id": "call-1-0"}
    messages: list[Any] = [
        HumanMessage(content="q steps=read,read tag=dead", id=question_id),
        AIMessage(content="", tool_calls=[read]),
        ToolMessage(content="probe_read 1 ok", tool_call_id="call-1-0"),
    ]
    if acted:
        act = {"name": "probe_act", "args": {"n": 2, "tag": "dead"}, "id": "call-2-0"}
        messages.append(AIMessage(content="", tool_calls=[act]))
    await graph.aupdate_state(turn_config(session_id), {"messages": messages}, as_node="model")


async def _resumable(session_id: str, question_id: str) -> ResumePoint:
    saver = await checkpointer()
    assert saver is not None
    found = await saver.aget_tuple(turn_config(session_id))  # type: ignore[arg-type]
    assert found is not None
    tail = judge(found.checkpoint["channel_values"]["messages"], question_id)
    assert not isinstance(tail, str), tail
    return ResumePoint(1, "dead-turn", question_id, "q", False, tail)


async def test_a_resume_reads_the_thread_again_after_its_claim_and_refuses_what_moved(
    durable: None, probes: Path
) -> None:
    """The old holder wrote an act between the judgement and the claim: it is not repeated.

    The point was judged on a thread that held only a read. By the time the resume holds the
    claim the thread ends on a state-changing call. Nothing runs, nothing is sent, and the resume
    is given back (`on_started` never fires); the control below resumes the unchanged thread.
    """
    session_id = await _held_session("pod-b:1")
    question_id = "q-dead"
    await _dead_turn_thread(session_id, question_id, acted=False)
    point = await _resumable(session_id, question_id)
    await _dead_turn_thread(session_id, question_id, acted=True)
    started: list[bool] = []

    events, ended = await _turn(
        session_id, "q", None, resume=point, on_started=lambda: started.append(True)
    )

    assert ended is None and events == [] and started == []
    assert _executed(probes) == [], "the model or a tool ran on a thread that had acted"


async def test_a_resume_of_the_thread_it_judged_runs_to_its_answer(
    durable: None, probes: Path
) -> None:
    session_id = await _held_session("pod-b:1")
    question_id = "q-dead-2"
    await _dead_turn_thread(session_id, question_id, acted=False)
    point = await _resumable(session_id, question_id)
    started: list[bool] = []

    events, ended = await _turn(
        session_id, "q", None, resume=point, on_started=lambda: started.append(True)
    )

    assert ended is None and events[-1].type == "answer", events
    assert started == [True]


async def test_a_resume_that_fails_before_the_thread_is_touched_is_not_settled_failed(
    durable: None, probes: Path
) -> None:
    """The route gives such a resume back, so its question stays `running` and nothing is booked."""
    session_id = await _held_session("pod-b:1")
    question_id = "q-dead-3"
    await _dead_turn_thread(session_id, question_id, acted=False)
    point = await _resumable(session_id, question_id)
    history = PostgresHistoryProvider()
    identity_row = await history.begin_turn(
        session_id, HumanMessage(content="q"), question_id=question_id
    )
    assert identity_row is not None
    point = point._replace(row_id=identity_row)

    def broken(**_kwargs: Any) -> Any:
        raise RuntimeError("the graph could not be built")

    events, ended = await _turn(session_id, "q", None, resume=point, graph_factory=broken)

    assert ended is None and [event.type for event in events] == ["error"]
    assert await _status(session_id) == "running"
    assert await _costs(session_id) == []

    # Control: the same failure in a first attempt is a failed turn, settled and booked.
    fresh = f"sess-fence-{uuid.uuid4().hex[:10]}"
    await SessionOwnerStore().record(fresh, "ana", None)
    _, ended = await _turn(fresh, "q", None, graph_factory=broken)
    assert await _status(fresh) == "failed"
    assert await _costs(fresh) == ["errored"]
