"""Which dead turns may be resumed, and the store rules that make a resume happen once.

`D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted`. The thread rules are a pure
function over messages; the store rules are checked on a migrated database. What a real killed
process does with them is `tests/test_turn_survives_pod.py`.
"""

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
from chemclaw.agent.state import turn_config
from chemclaw.agent.tool_authz import FAILED_CALL_MARK
from chemclaw.agent.turn_cost import TurnCost
from chemclaw.agent.turn_cost_store import PostgresTurnCostSink
from chemclaw.agent.turn_graph import build_turn_agent
from chemclaw.agent.turn_resume import judge
from chemclaw.agent.turn_usage import TurnUsage
from chemclaw.api.graph_stream import graph_events, replayed_events
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from tests.pg import migrated_db_or_skip
from tests.replica_graph import INPUT_TOKENS, OUTPUT_TOKENS, ProbeModel, probe_tools

QID = "question-id-unit-1"


def _question(question_id: str = QID) -> HumanMessage:
    """The chemist's message as a turn puts it in the thread."""
    return HumanMessage(content="what is the yield", id=question_id)


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
        ([_question(), _call("write_todos", "1", todos=[]), _result("1")], 2),
        ([_question(), _call("read_file", "1", file_path="/skills/a/SKILL.md")], 1),
        ([_question(), _call("write_file", "1", file_path="/scratch/a.md", content="x")], 1),
    ],
    ids=[
        "nothing-yet",
        "between-steps",
        "read-in-flight",
        "answer-written-not-delivered",
        "planning",
        "a-read-of-the-filesystem",
        "a-scratch-write",
    ],
)
def test_a_thread_of_reads_is_resumable_from_where_it_stands(
    thread: Sequence[BaseMessage], expected: int
) -> None:
    """The committed tail is what the resumed run replays, in any of these four places."""
    verdict = judge([HumanMessage(content="earlier"), *thread], QID)

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
        # Fail closed: only what is on the positive list repeats.
        ([_question(), _call("a_tool_nobody_classified", "1")], "acted"),
        ([_question(), _call("task", "1", description="look it up", subagent_type="x")], "acted"),
        ([_question(), _call("create_exhibit", "1", title="t")], "acted"),
        ([_question(), _call("revise_exhibit", "1", exhibit_id="e")], "acted"),
        ([_question(), _call("condense_protocols", "1")], "acted"),
        ([_question(), _call("a_job_launcher", "1")], "acted"),
        (
            [_question(), _call("find_notes", "1"), HumanMessage(content="revise")],
            "thread_moved_on",
        ),
        ([_question(), _call("find_notes", "1"), _call("find_notes", "2")], "unpaired_tool_calls"),
        ([HumanMessage(content="another question", id="other")], "question_not_in_thread"),
        ([], "question_not_in_thread"),
    ],
    ids=[
        "act-in-flight",
        "act-finished",
        "act-after-reads",
        "durable-memory-write",
        "handoff",
        "unknown-tool",
        "task-helper",
        "exhibit-writer",
        "exhibit-reviser",
        "tool-that-spends-model-calls",
        "tool-of-a-connector-this-process-does-not-have",
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
    assert judge(thread, QID) == reason


@pytest.fixture
def probes_classified(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The probe tools as a manifest would declare them, for this test only."""
    from chemclaw.connectors import registry

    monkeypatch.setattr(registry, "state_changing_tool_names", lambda: ["probe_act"])
    monkeypatch.setattr(registry, "read_only_tool_names", lambda: ["probe_read"])
    side_effecting_tools.cache_clear()
    yield
    side_effecting_tools.cache_clear()


def test_a_connector_read_resumes_only_where_a_manifest_here_classifies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive list is the enabled manifests': a process without the tool calls it acted."""
    from chemclaw.connectors import registry

    thread = [_question(), _call("lookup_property", "1"), _result("1")]
    monkeypatch.setattr(registry, "read_only_tool_names", list)
    assert judge(thread, QID) == "acted"

    monkeypatch.setattr(registry, "read_only_tool_names", lambda: ["lookup_property"])
    assert not isinstance(judge(thread, QID), str)


@pytest.fixture
def durable(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Postgres store, over a migrated database."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    asyncio.run(migrated_db_or_skip())
    yield


async def _question_row(
    session_id: str,
    correlation_id: str,
    *,
    dry_run: bool = False,
    question_id: str | None = None,
) -> int:
    """Write a question ahead, as a turn does, under `correlation_id` and the sender `ana`."""
    identity = set_current_identity("ana", frozenset())
    token = set_current_correlation_id(correlation_id)
    try:
        row = await PostgresHistoryProvider().begin_turn(
            session_id,
            HumanMessage(content="a question"),
            dry_run=dry_run,
            question_id=question_id if question_id is not None else f"qid-{correlation_id}",
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
    assert lapsed.question_id == "qid-resume-store-1"
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


async def test_a_shed_resume_is_given_back_and_the_turn_can_be_resumed_again(
    durable: None,
) -> None:
    """A resume that ran no step does not use up the turn's one resume."""
    session_id = await _new_session()
    history = PostgresHistoryProvider()
    row = await _question_row(session_id, "resume-shed-1")
    assert await history.claim_to_resume(session_id, row, "pod-b:1", 60, "ana")

    await history.give_back_resume(session_id, row, "pod-b:1")

    (lapsed,) = await history.lapsed_turns(session_id)
    assert lapsed.eligible, "a refusal before the first step cost the turn its resume"
    assert await history.claim_to_resume(session_id, row, "pod-c:1", 60, "ana")


async def test_a_noticer_that_waited_for_a_resume_does_not_mark_the_turn_it_resumed(
    durable: None,
) -> None:
    """The race of a Stop (or any reader) against an attach, on two real connections.

    The attach's transaction holds the question's row lock; the reader starts meanwhile and must
    see the claim once the lock is released. The control runs the bare mark statement through the
    same race: from its old snapshot it marks the turn the attach has just resumed.
    """
    from chemclaw.agent import session_store

    session_id = await _new_session()
    history = PostgresHistoryProvider()
    dsn = settings.session_store_dsn or settings.postgres_dsn

    async def race(mark: Any) -> Any:
        row = await _question_row(session_id, f"resume-race-{uuid.uuid4().hex[:6]}")
        async with db.connection(dsn) as resumer:
            await resumer.execute(session_store._TURN_CLAIM, (session_id, "pod-b:1", 60, "ana"))
            await resumer.execute(session_store._RESUME_MARK, (row, session_id))
            noticer = asyncio.create_task(mark())
            await asyncio.sleep(0.5)  # the noticer is now waiting on the question's row lock
            await resumer.commit()
        marked = await noticer
        async with db.connection(dsn) as conn:
            await conn.execute("DELETE FROM session_turns WHERE session_id = %s", (session_id,))
            await conn.commit()
        return marked

    # The statement alone cannot see the commit it waited for.
    async def bare() -> Any:
        async with db.connection(dsn) as conn:
            cursor = await conn.execute(session_store._MARK_INTERRUPTED, (session_id, []))
            rows = await cursor.fetchall()
            await conn.commit()
            return rows

    assert await race(bare), "the control did not show the race"
    await history.mark_interrupted(session_id)  # settle what the control left

    assert await race(lambda: history.mark_interrupted(session_id)) == []


async def test_a_question_the_previous_image_began_is_never_eligible(durable: None) -> None:
    """No recorded message id, no way to find the question in the thread: it ends `interrupted`."""
    session_id = await _new_session()
    history = PostgresHistoryProvider()
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        identity = set_current_identity("ana", frozenset())
        token = set_current_correlation_id("resume-old-1")
        try:
            await history.begin_turn(session_id, HumanMessage(content="old"))
        finally:
            reset_current_correlation_id(token)
            reset_current_identity(identity)
        await conn.commit()

    (lapsed,) = await history.lapsed_turns(session_id)

    assert lapsed.question_id == "" and lapsed.eligible is False


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


def _probe_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    """The real graph over the probe model and an in-memory saver, marking into `tmp_path`."""
    marks = tmp_path / "marks"
    marks.mkdir()
    monkeypatch.setenv("REPLICA_MARKS", str(marks))
    monkeypatch.setenv("REPLICA_GATE", str(tmp_path))
    graph = build_turn_agent(
        ProbeModel(),
        connectors=probe_tools(),
        checkpointer=InMemorySaver(),
        audit_sink=NullAuditSink(),
    )
    return graph, marks


async def test_a_finished_graph_resumed_runs_nothing_and_replays_its_trace_and_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes_classified: None
) -> None:
    """The pod died after the last model call and before the answer left: nothing is re-run.

    The committed tail goes into the trace, the exchanges and the spend of the turn and is not sent
    again; continuing the thread asks the model and the tools for nothing.
    """
    graph, marks = _probe_graph(tmp_path, monkeypatch)
    config = turn_config("resume-finished")
    first = HumanMessage(content="q steps=read tag=fin", id="fin-question")
    async for _ in graph.astream({"messages": [first]}, config, stream_mode=["updates"]):
        pass
    before = sorted(path.name.split(".")[1] for path in marks.iterdir())
    tail = judge((await graph.aget_state(config)).values["messages"], "fin-question")
    assert not isinstance(tail, str), tail

    usage = TurnUsage()
    trace = ToolCallTrace()
    exchanges: list[Any] = []
    replayed = [
        event
        async for event in replayed_events(tail, trace=trace, exchanges=exchanges, usage=usage)
    ]
    continued = [
        event
        async for event in graph_events(
            graph,
            "",
            config=config,
            trace=trace,
            on_signal=lambda _signal: None,
            usage=usage,
            continue_thread=True,
        )
    ]

    assert [event.type for event in replayed] == ["tool_call", "tool_result"]
    assert continued == []
    assert sorted(path.name.split(".")[1] for path in marks.iterdir()) == before
    assert (usage.input, usage.output) == (2 * INPUT_TOKENS, 2 * OUTPUT_TOKENS)
    assert trace.called_tools == ["probe_read"] and len(trace.outputs) == 1
    assert len(exchanges) == 2, "the transcript would lose the tool exchange"


async def test_a_call_the_dead_attempt_saw_fail_does_not_become_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes_classified: None
) -> None:
    """Failures are answered as results, so the thread carries a mark, and the replay honours it."""
    ok = ToolMessage(content="1.5 g", tool_call_id="1")
    refused = ToolMessage(
        content="no such compound", tool_call_id="2", additional_kwargs={FAILED_CALL_MARK: True}
    )
    tail = (
        _call("probe_read", "1"),
        ok,
        _call("probe_read", "2"),
        refused,
    )
    trace = ToolCallTrace()

    events = [
        event
        async for event in replayed_events(tail, trace=trace, exchanges=None, usage=TurnUsage())
    ]

    assert [event.type for event in events] == ["tool_call", "tool_result", "tool_call"]
    assert trace.outputs == ["1.5 g"], "a refused call became grounding evidence"


async def test_a_message_that_repeats_an_id_replaces_the_first_so_a_turn_never_names_one_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the overwrite: the thread replaces by id, so the id must not be the caller's.

    Two questions under one message id leave one; `run_turn` mints the id, so two turns that
    present one correlation id (a header any client sets) leave two.
    """
    graph, _ = _probe_graph(tmp_path, monkeypatch)
    config = turn_config("resume-overwrite")

    async def ask(text: str, message_id: str) -> None:
        async for _ in graph_events(
            graph,
            text,
            config=config,
            trace=ToolCallTrace(),
            on_signal=lambda _s: None,
            usage=TurnUsage(),
            message_id=message_id,
        ):
            pass

    async def questions() -> list[str]:
        values = (await graph.aget_state(config)).values["messages"]
        return [str(m.content) for m in values if isinstance(m, HumanMessage)]

    await ask("first steps= tag=ow", "the-same-id")
    await ask("second steps= tag=ow", "the-same-id")
    assert await questions() == ["second steps= tag=ow"], "the control no longer overwrites"

    config = turn_config("resume-no-overwrite")
    await ask("first steps= tag=ow", uuid.uuid4().hex)
    await ask("second steps= tag=ow", uuid.uuid4().hex)
    assert await questions() == ["first steps= tag=ow", "second steps= tag=ow"]
