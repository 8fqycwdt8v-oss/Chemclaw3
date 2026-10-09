"""Which dead turns may be resumed, and the store rules that make a resume happen once.

`D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted`. The thread rules are a pure
function over messages; the store rules are checked on a migrated database. What a real killed
process does with them is `tests/test_turn_survives_pod.py`.
"""

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
from chemclaw.agent.turn_cost import TurnCost
from chemclaw.agent.turn_cost_store import PostgresTurnCostSink
from chemclaw.agent.turn_resume import judge, question_message_id
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from tests.pg import migrated_db_or_skip

CID = "resume-unit-1"


def _question(correlation_id: str = CID) -> HumanMessage:
    """The chemist's message as a turn puts it in the thread."""
    return HumanMessage(content="what is the yield", id=question_message_id(correlation_id))


def _call(name: str, call_id: str, **arguments: Any) -> AIMessage:
    """An assistant message asking for one tool."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": call_id}])


def _result(call_id: str) -> ToolMessage:
    """The recorded result of one call."""
    return ToolMessage(content="ok", tool_call_id=call_id)


@pytest.mark.parametrize(
    ("thread", "expected"),
    [
        ([_question()], 0),
        ([_question(), _call("find_notes", "1"), _result("1")], 2),
        ([_question(), _call("find_notes", "1")], 1),
        ([_question(), _call("find_notes", "1"), _result("1"), AIMessage(content="done")], 3),
    ],
    ids=["nothing-yet", "between-steps", "read-in-flight", "answer-written-not-delivered"],
)
def test_a_thread_of_reads_is_resumable_from_where_it_stands(
    thread: Sequence[BaseMessage], expected: int
) -> None:
    """The committed tail is what the resumed run replays, in any of these four places."""
    verdict = judge([HumanMessage(content="earlier"), *thread], CID)

    assert not isinstance(verdict, str), verdict
    assert len(verdict) == expected


@pytest.mark.parametrize(
    ("thread", "reason"),
    [
        ([_question(), _call("record_knowledge_note", "1")], "acted"),
        ([_question(), _call("record_knowledge_note", "1"), _result("1")], "acted"),
        ([_question(), _call("find_notes", "1"), _result("1"), _call("watch_for", "2")], "acted"),
        (
            [_question(), _call("write_file", "1", file_path="/memories/a.md", content="x")],
            "acted",
        ),
        ([_question(), _call("transfer_to_reviewer", "1")], "acted"),
        (
            [_question(), _call("find_notes", "1"), HumanMessage(content="revise")],
            "thread_moved_on",
        ),
        ([_question(), _call("find_notes", "1"), _call("find_notes", "2")], "unpaired_tool_calls"),
        ([HumanMessage(content="another question", id="q:other")], "question_not_in_thread"),
        ([], "question_not_in_thread"),
    ],
    ids=[
        "act-in-flight",
        "act-finished",
        "act-after-reads",
        "durable-memory-write",
        "handoff",
        "revision-prompt",
        "a-call-nobody-answered-and-the-model-went-on",
        "the-dead-turn-never-reached-the-thread",
        "empty-thread",
    ],
)
def test_a_thread_that_acted_or_moved_on_is_left_to_end_interrupted(
    thread: Sequence[BaseMessage], reason: str
) -> None:
    """Control for the rule: each of these is refused, and says which part of it did."""
    assert judge(thread, CID) == reason


@pytest.fixture
def durable(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Postgres store, over a migrated database."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    asyncio.run(migrated_db_or_skip())
    yield


async def _question_row(session_id: str, correlation_id: str, *, dry_run: bool = False) -> int:
    """Write a question ahead, as a turn does, under `correlation_id` and the sender `ana`."""
    identity = set_current_identity("ana", frozenset())
    token = set_current_correlation_id(correlation_id)
    try:
        row = await PostgresHistoryProvider().begin_turn(
            session_id, HumanMessage(content="a question"), dry_run=dry_run
        )
    finally:
        reset_current_correlation_id(token)
        reset_current_identity(identity)
    assert row is not None
    return row


async def _new_session() -> str:
    session_id = f"sess-resume-{uuid.uuid4().hex[:10]}"
    await SessionOwnerStore().record(session_id, "ana", None)
    return session_id


async def _expire(session_id: str) -> None:
    """Age every claim on the session past its lease."""
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE session_turns SET expires_at = now() - interval '1 second' "
            "WHERE session_id = %s",
            (session_id,),
        )
        await conn.commit()


async def test_a_dead_turn_is_taken_over_once_and_the_new_claim_covers_its_question(
    durable: None,
) -> None:
    """The claim and the mark are one decision: a noticer never calls the resumed turn dead."""
    session_id = await _new_session()
    history = PostgresHistoryProvider()
    row = await _question_row(session_id, "resume-store-1", dry_run=True)

    (lapsed,) = await history.lapsed_turns(session_id)
    assert (lapsed.row_id, lapsed.actor, lapsed.eligible) == (row, "ana", True)
    assert lapsed.dry_run is True, "a dry run resumed as a live one is a turn nobody asked for"

    assert await history.claim_to_resume(session_id, row, "pod-b:1", 60, "ana")
    assert not await history.claim_to_resume(session_id, row, "pod-c:1", 60, "ana"), (
        "two replicas resumed one turn"
    )
    assert await history.lapsed_turns(session_id) == [], "the resumer's claim does not cover it"
    assert await history.mark_interrupted(session_id) == [], "a running turn was called dead"

    # It dies again: the question is no longer eligible, so the next noticer ends it.
    await _expire(session_id)
    (again,) = await history.lapsed_turns(session_id)
    assert again.eligible is False
    assert [turn.correlation_id for turn in await history.mark_interrupted(session_id)] == [
        "resume-store-1"
    ]


async def test_the_rows_a_caller_spares_are_left_running_and_the_rest_are_marked(
    durable: None,
) -> None:
    """`spare` is how the noticers leave a resumable turn alone; without it the turn ends."""
    session_id = await _new_session()
    history = PostgresHistoryProvider()
    row = await _question_row(session_id, "resume-spare-1")

    assert await history.mark_interrupted(session_id, spare=[row]) == []
    assert await history.latest_turn_status(session_id) == "running"
    assert len(await history.mark_interrupted(session_id)) == 1
    assert await history.latest_turn_status(session_id) == "interrupted"


async def test_a_turn_past_the_window_or_already_booked_is_not_eligible(
    durable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the claim above: the same dead question, ineligible for each of two reasons."""
    session_id = await _new_session()
    history = PostgresHistoryProvider()
    await _question_row(session_id, "resume-late-1")
    (fresh,) = await history.lapsed_turns(session_id)
    assert fresh.eligible

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.001)
    await asyncio.sleep(0.05)
    (late,) = await history.lapsed_turns(session_id)
    assert late.eligible is False, "a turn older than a turn may run was offered"

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 600.0)
    await PostgresTurnCostSink().record(
        TurnCost(correlation_id="resume-late-1", session_id=session_id, outcome="errored")
    )
    (booked,) = await history.lapsed_turns(session_id)
    assert booked.eligible is False, "a turn whose outcome is booked was offered"
