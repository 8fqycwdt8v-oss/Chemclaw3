"""A turn's question is written ahead of it, and a turn whose process died says so — once.

`D-2026-10-03-a-turn-is-written-ahead-and-an-interrupted-one-says-so`. Measured on the kind cluster
(K5 §1): a front-door pod killed mid-turn left the chemist's question in the LangGraph checkpoint
and out of `session_messages`, so the next turn's model read a question the chemist's transcript did
not show; the reattach answered a bare 404; and no `turn_costs` row said how the turn ended.

**A dead process is simulated by what it leaves behind, not by a stub of the noticer.** The turn is
the real `run_turn` over the real graph, the real Postgres checkpointer and the real transcript
store; its model call hangs, the way a pod killed mid-call looks from the thread's side. The task is
then cancelled with the two teardown writes a SIGKILL never runs (`_settle_after_teardown`,
`_book_turn_spend`) disabled, and the turn's claim — taken as the route takes it, under a holder
that is never refreshed again — is aged past its lease. Everything that notices is production code:
the next turn, the reattach route and the transcript route.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, cast

import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableConfig

from chemclaw.agent import turn_cost
from chemclaw.agent.checkpointer import checkpointer, close_checkpointer
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_store import (
    InMemoryHistoryProvider,
    PostgresHistoryProvider,
    SessionOwnerStore,
    SessionTurnClaims,
    stored_correlation_id,
    stored_turn_status,
)
from chemclaw.agent.state import turn_config
from chemclaw.api import runner
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.events import Event
from chemclaw.api.runner import run_turn
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    set_current_correlation_id,
)
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

_ALICE = Principal(oid="alice-write-ahead", upn="alice@corp", roles=frozenset())


class _Model(GenericFakeChatModel):
    """A model that answers from its script; `bind_tools` returns itself, as the graph requires."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Bind nothing — these turns call no tool."""
        return self


class _Hung(_Model):
    """A model call that never returns: what a pod killed mid-call looks like from the thread."""

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Wait for ever."""
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Wait for ever, streaming."""
        await asyncio.Event().wait()
        yield ChatGenerationChunk(message=AIMessageChunk(content=""))  # pragma: no cover


def _answering(text: str) -> _Model:
    """A model whose one reply is `text`."""
    return _Model(messages=iter([AIMessage(content=text)]))


def _factory(model: _Model) -> Callable[..., Any]:
    """The real compiled graph over `model` — the front door's `graph_factory` seam."""

    def _build(**kwargs: Any) -> Any:
        kwargs.pop("model", None)
        return build_langgraph_agent(model=model, **kwargs)

    return _build


@pytest.fixture
def durable(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Postgres store everywhere: transcript, checkpointer, claims and the cost ledger."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    yield


async def _session() -> str:
    """A fresh session Alice owns, over a migrated database with the checkpointer's tables."""
    await migrated_db_or_skip()
    await create_checkpoint_tables()
    # A saver a previous test published belongs to a closed loop; this test builds its own.
    await close_checkpointer()
    session_id = f"sess-write-ahead-{uuid.uuid4().hex[:10]}"
    await SessionOwnerStore().record(session_id, _ALICE.oid, None)
    return session_id


async def _turn(
    session_id: str, message: str, model: _Model, *, correlation_id: str, history: Any = None
) -> list[Event]:
    """One real `run_turn` under `correlation_id`, as the front door runs it; its events."""
    token = set_current_correlation_id(correlation_id)
    try:
        return [
            event
            async for event in run_turn(
                TurnSession(session_id=session_id),
                message,
                actor=_ALICE.oid,
                history=history if history is not None else PostgresHistoryProvider(),
                connectors=[],
                graph_factory=_factory(model),
            )
        ]
    finally:
        reset_current_correlation_id(token)


async def _until(predicate: Callable[[], Any], *, seconds: float = 20.0) -> None:
    """Poll an async predicate until it holds; fail loudly rather than hang."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.05)


async def _thread(session_id: str) -> list[str]:
    """The human messages the *model* will be built from next turn, in order."""
    saver = await checkpointer()
    stored = await saver.aget_tuple(cast("RunnableConfig", turn_config(session_id)))
    if stored is None:
        return []
    return [
        str(message.content)
        # The input checkpoint holds the message on the `__start__` channel before the first
        # node runs; from then on it is in `messages`. Either is "the model will read it".
        for message in [
            *stored.checkpoint["channel_values"].get("messages", []),
            *_started_with(stored.checkpoint["channel_values"].get("__start__")),
        ]
        if isinstance(message, HumanMessage)
    ]


def _started_with(start: Any) -> list[Any]:
    """The messages an input checkpoint carries on `__start__`, which no node has consumed yet."""
    return list(start.get("messages", [])) if isinstance(start, dict) else []


async def _transcript(session_id: str) -> list[BaseMessage]:
    """The stored transcript as the transcript route reads it."""
    return await PostgresHistoryProvider().get_messages(session_id)


async def _outcomes(correlation_id: str) -> list[str]:
    """Every `turn_costs` outcome booked for one turn, once the ledger's writes have landed."""
    await asyncio.gather(*list(turn_cost._PENDING), return_exceptions=True)
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT outcome FROM turn_costs WHERE correlation_id = %s", (correlation_id,)
        )
        return [str(row[0]) for row in await cursor.fetchall()]


async def _expire_claim(session_id: str) -> None:
    """Age the session's claim past its lease — what a holder nobody refreshes reaches."""
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE session_turns SET expires_at = now() - interval '1 second' "
            "WHERE session_id = %s",
            (session_id,),
        )
        await conn.commit()


async def _a_turn_whose_process_dies(session_id: str, question: str, correlation_id: str) -> None:
    """Start a real turn under a claim, let it reach the model, then kill it with no teardown."""
    assert await SessionTurnClaims().claim(session_id, "pod-killed:turn-1", 60)
    patch = pytest.MonkeyPatch()
    # The two writes a teardown makes and a SIGKILL never does. Everything else is the real turn.
    patch.setattr(runner, "_settle_after_teardown", lambda *args, **kwargs: None)
    patch.setattr(runner, "_book_turn_spend", lambda *args, **kwargs: None)
    try:
        task = asyncio.create_task(
            _turn(session_id, question, _Hung(messages=iter([])), correlation_id=correlation_id)
        )

        async def _in_both_records() -> bool:
            return question in await _thread(session_id) and any(
                stored_turn_status(m) == "running" for m in await _transcript(session_id)
            )

        try:
            await _until(_in_both_records)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        patch.undo()


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """No connectors: nothing here runs a turn through the front door."""
    return []


def _client(session_id: str) -> httpx.AsyncClient:
    """The front door over the durable stores, as Alice; nothing here runs a turn through it."""
    app = create_app(
        owner_store=SessionOwnerStore(),
        turn_claims=SessionTurnClaims(),
        connector_factory=_no_connectors,
        graph_factory=lambda *args, **kwargs: None,
    )
    app.dependency_overrides[require_principal] = lambda: _ALICE
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://front")


async def test_an_answered_turn_reads_back_as_it_always_did_with_its_question_settled(
    durable: None,
) -> None:
    """The normal path: question then answer, one turn's id on both, the question `done`."""
    session_id = await _session()
    try:
        events = await _turn(session_id, "what is 2+2?", _answering("4"), correlation_id="wa-ok-1")
        assert events[-1].type == "answer", events
        transcript = await _transcript(session_id)
        async with _client(session_id) as client:
            wire = (await client.get(f"/sessions/{session_id}/messages")).json()
    finally:
        await close_checkpointer()

    assert [(type(m).__name__, str(m.content)) for m in transcript] == [
        ("HumanMessage", "what is 2+2?"),
        ("AIMessage", "4"),
    ], "the question was written twice, or the exchange lost its shape"
    assert [stored_correlation_id(m) for m in transcript] == ["wa-ok-1", "wa-ok-1"]
    assert [(row["role"], row["text"], row["turn_status"]) for row in wire] == [
        ("user", "what is 2+2?", "done"),
        ("assistant", "4", None),
    ]


async def test_a_turn_whose_process_died_is_marked_interrupted_once_and_the_next_turn_runs(
    durable: None,
) -> None:
    """The K5 §1 scenario, end to end, with every noticer production code.

    While the dead turn's lease runs nothing may call it interrupted — that is indistinguishable
    from a slow turn on another replica. Once it lapses: the reattach answers 410
    `turn_interrupted` (not a bare 404), the transcript shows the question marked `interrupted`, the
    outcome is booked exactly once however many readers noticed, and the next turn runs and leaves
    the model's record and the chemist's agreeing about every question asked.
    """
    session_id = await _session()
    try:
        await _a_turn_whose_process_dies(session_id, "resilience one", "wa-dead-1")

        async with _client(session_id) as client:
            # Still inside the lease: a slow turn elsewhere, not a dead one.
            early = await client.get(f"/sessions/{session_id}/turn/stream")
            assert early.status_code == 404, early.text
            listed = (await client.get(f"/sessions/{session_id}/messages")).json()
            assert [row["turn_status"] for row in listed] == ["running"], listed

            await _expire_claim(session_id)

            reattach = await client.get(f"/sessions/{session_id}/turn/stream")
            assert reattach.status_code == 410, reattach.text
            assert reattach.json()["detail"]["code"] == "turn_interrupted"
            # Two more readers notice the same turn; neither books it again.
            again = await client.get(f"/sessions/{session_id}/turn/stream")
            assert again.status_code == 410, again.text
            wire = (await client.get(f"/sessions/{session_id}/messages")).json()
        assert [(row["text"], row["turn_status"], row["correlation_id"]) for row in wire] == [
            ("resilience one", "interrupted", "wa-dead-1")
        ]
        assert await _outcomes("wa-dead-1") == ["interrupted"]

        # The retry: a new turn, under a claim of its own, answering normally.
        assert await SessionTurnClaims().claim(session_id, "pod-alive:turn-2", 60)
        events = await _turn(
            session_id, "resilience one, again", _answering("an answer"), correlation_id="wa-ok-2"
        )
        assert events[-1].type == "answer", events
        thread = await _thread(session_id)
        transcript = await _transcript(session_id)
    finally:
        await close_checkpointer()

    asked = [str(m.content) for m in transcript if isinstance(m, HumanMessage)]
    assert asked == thread == ["resilience one", "resilience one, again"], (
        f"the model will read {thread} and the chemist sees {asked}"
    )
    assert [stored_turn_status(m) for m in transcript] == ["interrupted", "done", None]
    assert await _outcomes("wa-dead-1") == ["interrupted"], "the next turn booked it a second time"
    assert await _outcomes("wa-ok-2") == ["answered"]


async def test_the_next_turn_alone_is_enough_to_notice(durable: None) -> None:
    """Nobody reattached and nobody reloaded: the session's next turn marks and books it."""
    session_id = await _session()
    try:
        await _a_turn_whose_process_dies(session_id, "first question", "wa-dead-2")
        await _expire_claim(session_id)
        # The route takes the claim before the turn runs; a successor's claim post-dates the
        # question, which is exactly what tells it apart from the dead turn's own.
        assert await SessionTurnClaims().claim(session_id, "pod-alive:turn-3", 60)
        await _turn(session_id, "second question", _answering("ok"), correlation_id="wa-ok-3")
        transcript = await _transcript(session_id)
    finally:
        await close_checkpointer()

    assert [(str(m.content), stored_turn_status(m)) for m in transcript] == [
        ("first question", "interrupted"),
        ("second question", "done"),
        ("ok", None),
    ]
    assert await _outcomes("wa-dead-2") == ["interrupted"]


async def test_a_question_under_a_live_claim_is_never_marked_and_a_successors_claim_is_not_one(
    durable: None,
) -> None:
    """The predicate, both ways: the owner's own claim protects it; a later one does not."""
    await migrated_db_or_skip()
    session_id = f"sess-write-ahead-{uuid.uuid4().hex[:10]}"
    await SessionOwnerStore().record(session_id, _ALICE.oid, None)
    history = PostgresHistoryProvider()
    claims = SessionTurnClaims()
    assert await claims.claim(session_id, "owner", 60)
    token = set_current_correlation_id("wa-live-1")
    try:
        turn = await history.begin_turn(session_id, HumanMessage(content="slow but alive"))
    finally:
        reset_current_correlation_id(token)
    assert turn is not None

    assert await history.mark_interrupted(session_id) == [], "a live turn was called dead"
    # The owner refreshes, as `_hold_turn_claim` does: still alive.
    assert await claims.refresh(session_id, "owner", 60)
    assert await history.mark_interrupted(session_id) == []

    # Its lease lapses and a successor claims the session: the question is now nobody's.
    await _expire_claim(session_id)
    assert await claims.claim(session_id, "successor", 60)
    assert await history.mark_interrupted(session_id) == [("wa-live-1", None)]
    assert await history.mark_interrupted(session_id) == [], "marked — and booked — twice"
    assert await history.latest_turn_status(session_id) == "interrupted"


async def test_an_answer_overrides_a_mark_and_an_unanswered_ending_never_demotes(
    durable: None,
) -> None:
    """The settle rule: the answer a chemist received is the record; a teardown cannot undo one."""
    await migrated_db_or_skip()
    session_id = f"sess-write-ahead-{uuid.uuid4().hex[:10]}"
    await SessionOwnerStore().record(session_id, _ALICE.oid, None)
    history = PostgresHistoryProvider()

    # A turn wrongly marked (its owner was alive but missed its refreshes) still answers.
    marked = await history.begin_turn(session_id, HumanMessage(content="one"))
    assert marked is not None
    assert len(await history.mark_interrupted(session_id)) == 1
    await history.finish_turn(session_id, marked, [AIMessage(content="answer one")], "done")
    # A turn that answered, whose teardown then arrives late with `stopped`.
    answered = await history.begin_turn(session_id, HumanMessage(content="two"))
    assert answered is not None
    await history.finish_turn(session_id, answered, [AIMessage(content="answer two")], "done")
    await history.finish_turn(session_id, answered, [], "stopped")
    # A turn that failed: settled once, and a mark afterwards does not find it running.
    failed = await history.begin_turn(session_id, HumanMessage(content="three"))
    assert failed is not None
    await history.finish_turn(session_id, failed, [], "failed")
    assert await history.mark_interrupted(session_id) == []

    assert [
        (str(m.content), stored_turn_status(m)) for m in await history.get_messages(session_id)
    ] == [
        ("one", "done"),
        ("answer one", None),
        ("two", "done"),
        ("answer two", None),
        ("three", "failed"),
    ]


async def test_the_in_memory_store_writes_ahead_and_settles_the_same_way() -> None:
    """Both providers answer one call the same way, so a dev process reads what production does."""
    history = InMemoryHistoryProvider()
    session = TurnSession(session_id="sess-write-ahead-memory")

    answered = await _turn(
        session.session_id, "q one", _answering("a one"), correlation_id="wa-mem-1", history=history
    )
    assert answered[-1].type == "answer"
    # The provider keeps its thread in the session's state, which `_turn` built fresh; so drive it
    # directly for the read-back, with the same primitives `run_turn` calls.
    state: dict[str, Any] = {}
    turn = await history.begin_turn(session.session_id, HumanMessage(content="q"), state=state)
    assert turn == 0
    assert await history.latest_turn_status(session.session_id, state=state) == "running"
    await history.finish_turn(session.session_id, turn, [], "stopped", state=state)
    await history.finish_turn(session.session_id, turn, [], "failed", state=state)
    assert await history.latest_turn_status(session.session_id, state=state) == "stopped"
    assert await history.mark_interrupted(session.session_id, state=state) == []
    # A rolled-back state no longer holds the question; settling it touches nothing.
    rolled_back: dict[str, Any] = {}
    await history.finish_turn(session.session_id, 0, [], "failed", state=rolled_back)
    assert rolled_back == {"chemclaw_transcript": []}


async def test_a_failed_turn_leaves_its_question_marked_failed(durable: None) -> None:
    """A turn that raised: the chemist sees the question the model saw, and that it failed."""
    session_id = await _session()

    class _Broken(_Model):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
            raise ValueError("the gateway refused")

        async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
            raise ValueError("the gateway refused")
            yield  # pragma: no cover

    try:
        events = await _turn(
            session_id, "a doomed question", _Broken(messages=iter([])), correlation_id="wa-err-1"
        )
        transcript = await _transcript(session_id)
        thread = await _thread(session_id)
    finally:
        await close_checkpointer()

    assert events[-1].type == "error", events
    assert [(str(m.content), stored_turn_status(m)) for m in transcript] == [
        ("a doomed question", "failed")
    ]
    assert thread == ["a doomed question"]
