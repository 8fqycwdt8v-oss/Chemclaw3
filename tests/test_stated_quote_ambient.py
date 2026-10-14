"""The chemist's own words in this thread, and what a `basis="stated"` quote may be checked against.

`core/turn_text.py` binds the ambient that `require_quotes_are_verbatim` grades against; it spans
the chemist's recent messages in the thread, not only the current one, since intake is iterative.
Two properties must survive that widening:

- only a person's words enter it: the model's own prose and mid-turn job push-backs do not,
  which is why the transcript, not the checkpointer, is the source (driven on a real graph);
- the window is bounded, and its refusal says what was checked.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import chemclaw.agent.protocol_design_tools as tools
from chemclaw.agent.message_migration import LANGCHAIN_SHAPE
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_store import (
    _SELECT_RECENT_USER_ROWS,
    DEGRADED_RENDER,
    InMemoryHistoryProvider,
    PostgresHistoryProvider,
    chemist_words,
)
from chemclaw.api import runner
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.turn_text import (
    get_current_user_texts,
    reset_current_user_texts,
    set_current_user_texts,
)
from chemclaw.protocols.models import ExperimentRequest, RequestField
from tests.fakes_turn import Piece, ScriptedTurn
from tests.pg import migrated_db_or_skip, planned_index

#: The chemist's turn 1 — a real constraint, stated in their own words, two turns before the
#: "ok go ahead" that triggers the intake.
_TURN_ONE = "24 wells, no DMF, by Friday please."


def _stated(quote: str, value: str = "24") -> ExperimentRequest:
    """An ask whose `max_runs` claims the chemist's own words."""
    return ExperimentRequest.model_validate(
        {
            "title": "SM-3 Suzuki",
            "goal": "couple the deactivated aryl chloride",
            "max_runs": RequestField(value=value, basis="stated", quote=quote),
        }
    )


@pytest.fixture(autouse=True)
def _no_ambient() -> Iterator[None]:
    """Leave no thread's words bound behind a test, whichever way it left."""
    token = set_current_user_texts(None)
    try:
        yield
    finally:
        reset_current_user_texts(token)


# --- the ambient itself ---------------------------------------------------------------------


def test_a_quote_from_an_earlier_turn_is_checkable() -> None:
    """The row's scenario, at the level the refusal was raised: turn 1's words, turn 3's ask."""
    token = set_current_user_texts([_TURN_ONE, "what about the base?", "ok go ahead"])
    try:
        tools.require_quotes_are_verbatim(_stated("24 wells"), get_current_user_texts())
    finally:
        reset_current_user_texts(token)


def test_words_the_chemist_never_wrote_are_still_refused() -> None:
    """The widening is a widening of the haystack, not a relaxation of the comparison."""
    token = set_current_user_texts([_TURN_ONE, "ok go ahead"])
    try:
        with pytest.raises(ChemclawError, match="not in anything the chemist has written"):
            tools.require_quotes_are_verbatim(
                _stated("48 wells", value="48"), get_current_user_texts()
            )
    finally:
        reset_current_user_texts(token)


def test_a_quote_spanning_two_messages_is_not_words_the_chemist_wrote() -> None:
    """Joining the thread into one haystack would accept an order nobody ever said it in.

    "no DMF" ends turn 1 and "by Friday" opens turn 2, so a concatenated haystack contains
    "no dmf by friday" and this passes. Each message is its own haystack, so it does not.
    """
    token = set_current_user_texts(["24 wells, no DMF", "by Friday please, use toluene"])
    try:
        with pytest.raises(ChemclawError, match="not in anything the chemist has written"):
            tools.require_quotes_are_verbatim(
                _stated("no DMF by Friday", value="toluene"), get_current_user_texts()
            )
    finally:
        reset_current_user_texts(token)


def test_the_turn_in_flight_survives_a_character_budget_it_alone_exceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound that could take this turn's message away would be stricter than the narrow check.

    The front door accepts 100,000 characters in one message, so this is reachable rather than
    theoretical — and the floor under the widening is that the turn in flight stays quotable.
    """
    monkeypatch.setattr(settings, "agent_stated_quote_chars", 10)
    token = set_current_user_texts(["something earlier", _TURN_ONE])
    try:
        assert get_current_user_texts() == (_TURN_ONE,)
        tools.require_quotes_are_verbatim(_stated("24 wells"), get_current_user_texts())
    finally:
        reset_current_user_texts(token)


def test_a_message_outside_the_window_is_refused_and_the_message_says_what_was_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quote from outside the window is refused, and the message names the window and the remedy.

    Otherwise the model's only move is to mislabel the quote `basis='inferred'`.
    """
    monkeypatch.setattr(settings, "agent_stated_quote_turns", 1)
    token = set_current_user_texts([_TURN_ONE, "what about the base?", "ok go ahead"])
    try:
        assert get_current_user_texts() == ("what about the base?", "ok go ahead")
        with pytest.raises(ChemclawError) as refusal:
            tools.require_quotes_are_verbatim(_stated("24 wells"), get_current_user_texts())
    finally:
        reset_current_user_texts(token)
    said = str(refusal.value)
    assert "2 of their messages checked" in said, said
    assert "older than the window this conversation keeps" in said, said
    assert "restate it" in said, said


def test_the_character_budget_bounds_a_window_a_turn_count_would_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One message may be `service_max_message_chars` long, so a turn count bounds no memory."""
    monkeypatch.setattr(settings, "agent_stated_quote_chars", 100)
    token = set_current_user_texts(["x" * 200, "24 wells earlier", "ok go ahead"])
    try:
        assert get_current_user_texts() == ("24 wells earlier", "ok go ahead")
    finally:
        reset_current_user_texts(token)


def test_no_turn_is_still_a_refusal_rather_than_a_waiver() -> None:
    """`require_actor`'s reject-if-absent rule, and an empty thread says the same as no thread."""
    absent: tuple[Sequence[str] | None, ...] = (None, [])
    for absent_thread in absent:
        token = set_current_user_texts(absent_thread)
        try:
            assert get_current_user_texts() is None
            with pytest.raises(ChemclawError, match="no chemist message"):
                tools.require_quotes_are_verbatim(_stated("24 wells"), get_current_user_texts())
        finally:
            reset_current_user_texts(token)


def test_one_string_is_refused_rather_than_bound_as_one_haystack_per_character() -> None:
    """`str` is a `Sequence[str]`, so `mypy --strict` cannot catch this and the check would rot.

    Bound as characters, a one-character quote matches anything and every longer one is refused —
    a check that silently stops checking, which is the failure mode this module exists to prevent.
    """
    # No `type: ignore` here, and its absence is the assertion: `mypy --strict` accepts this call
    # (it is checked in this suite, so an ignore would be reported as unused), which is precisely
    # why the refusal has to be at runtime.
    with pytest.raises(TypeError, match="not one string"):
        set_current_user_texts(_TURN_ONE)


# --- what may become the chemist's words ----------------------------------------------------


def test_only_a_persons_own_typed_words_pass_the_filter() -> None:
    """`chemist_words` is where the anti-spoofing property survives the widening."""
    recovered = HumanMessage(content="24 wells", additional_kwargs={DEGRADED_RENDER: "maf"})
    assert chemist_words(
        [
            HumanMessage(content=_TURN_ONE),
            AIMessage(content="I will use 96 wells then."),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "t", "args": {}}]),
            ToolMessage(content="the plate holds 384 wells", tool_call_id="c1"),
            recovered,
            HumanMessage(content=[{"type": "text", "text": "48 wells"}]),
            HumanMessage(content="ok go ahead"),
        ]
    ) == [_TURN_ONE, "ok go ahead"]


# --- end to end, through the runner ----------------------------------------------------------


class _Quiet(ScriptedTurn):
    """A turn that answers and asks nothing of the ambient."""

    def __init__(self, answer: str = "ok.") -> None:
        self._answer = answer

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield self._answer


class _Intake(ScriptedTurn):
    """A turn that structures the ask, quoting words from earlier in the conversation.

    The check runs inside the model's own stream, which runs inside `_turn_ambient` — so this is
    the ambient a real tool call sees, not one the test built.
    """

    def __init__(self, quote: str, value: str = "24") -> None:
        self.quote = quote
        self.value = value
        self.refusal: str | None = None
        self.saw: tuple[str, ...] | None = None

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        self.saw = get_current_user_texts()
        try:
            tools.require_quotes_are_verbatim(_stated(self.quote, self.value), self.saw)
        except ChemclawError as exc:
            self.refusal = str(exc)
        yield "done."


async def _drive(
    session: TurnSession, history: Any, script: list[tuple[str, ScriptedTurn]]
) -> None:
    """Run each (message, behaviour) as a whole turn through the real runner."""
    for message, agent in script:
        async for _event in runner.run_turn(
            session, message, connectors=[], history=history, graph_factory=agent.graph_factory
        ):
            pass


async def test_a_constraint_stated_three_turns_ago_reaches_the_intake() -> None:
    """The row's scenario end to end: the words on turn 1, the intake on turn 3."""
    intake = _Intake("24 wells")

    session = TurnSession(session_id="s-earlier-turn")
    await _drive(
        session,
        InMemoryHistoryProvider(),
        [
            (_TURN_ONE, _Quiet()),
            ("what about the base?", _Quiet()),
            ("ok go ahead", intake),
        ],
    )

    assert intake.saw == (_TURN_ONE, "what about the base?", "ok go ahead")
    assert intake.refusal is None, intake.refusal


class _CountingHistory(InMemoryHistoryProvider):
    """The in-memory provider, plus how many times and with what bound it was read."""

    def __init__(self) -> None:
        self.reads: list[int] = []

    async def recent_user_texts(
        self, session_id: str | None, *, limit: int, state: Any = None
    ) -> list[str]:
        self.reads.append(limit)
        return await super().recent_user_texts(session_id, limit=limit, state=state)


#: Fifty sessions of two hundred rows, half of them a person's words: a table in which one session
#: is a fiftieth of it, so reaching only that session is visibly cheaper than any other path.
_AMBIENT_SEED = (
    "INSERT INTO session_messages (session_id, message, message_shape) "
    "SELECT 'seed-' || (n % 50), "
    "jsonb_build_object('type', CASE WHEN n % 2 = 0 THEN 'human' ELSE 'ai' END), "
    f"'{LANGCHAIN_SHAPE}' "
    "FROM generate_series(1, 10000) AS n"
)


async def _ambient_read_reaches(without: str | None = None) -> str:
    """The index the ambient read plans through over a table of known size and shape."""
    return await planned_index(
        "session_messages",
        _AMBIENT_SEED,
        _SELECT_RECENT_USER_ROWS,
        ("seed-7", LANGCHAIN_SHAPE, 20),
        without=without,
    )


def test_the_ambient_read_is_bounded_by_the_session_and_not_by_the_table() -> None:
    """The ambient read plans through the partial index, visiting no other session.

    A plan assertion, since the row counts are stable and timings are not: the read must cost
    O(session), not O(table). The plan is asked of a copy of the table this test fills and analyses
    itself (`tests.pg.planned_index`), since a shared table's statistics are whatever other tests
    left. Without the partial index the plan names another, so the assertion can fail.
    """
    assert asyncio.run(_ambient_read_reaches()) == "session_messages_ambient_human_idx", (
        "the ambient read no longer plans through the partial index migration 098 added, so it is "
        "back to walking the primary key and discarding every other session's rows — O(table) on "
        "the answer path, once per turn"
    )
    assert (
        asyncio.run(_ambient_read_reaches(without="session_messages_ambient_human_idx"))
        != "session_messages_ambient_human_idx"
    )


def test_the_window_the_runner_reads_is_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner reads the configured window, and at `0` skips the store entirely.

    Asserted through the runner: the bound must be the configured value, and the skip saves a
    round trip that the providers' own non-positive refusal would otherwise hide.
    """
    monkeypatch.setattr(settings, "agent_stated_quote_turns", 3)
    history = _CountingHistory()
    read_at_three = _Intake("24 wells")

    async def _body(intake: _Intake) -> None:
        session = TurnSession(session_id="s-window")
        await _drive(session, history, [(_TURN_ONE, _Quiet()), ("ok go ahead", intake)])

    asyncio.run(_body(read_at_three))
    assert history.reads == [3, 3], "the query was not bounded by the configured window"
    assert read_at_three.refusal is None, read_at_three.refusal

    monkeypatch.setattr(settings, "agent_stated_quote_turns", 0)
    history.reads.clear()
    off = _Intake("24 wells")
    asyncio.run(_body(off))
    assert history.reads == [], "the store was read for a window the deployment turned off"
    assert off.saw == ("ok go ahead",)
    assert off.refusal is not None


async def test_the_agents_own_earlier_prose_is_not_quotable_as_the_chemist() -> None:
    """The agent's own earlier prose is not quotable as the chemist.

    The words are indistinguishable; who said them is recorded, so `chemist_words` filters the
    transcript by role.
    """
    intake = _Intake("96 wells", value="96")

    session = TurnSession(session_id="s-model-prose")
    await _drive(
        session,
        InMemoryHistoryProvider(),
        [
            ("what should we run?", _Quiet("Let us run 96 wells on the deactivated chloride.")),
            ("ok go ahead", intake),
        ],
    )

    assert intake.saw == ("what should we run?", "ok go ahead")
    assert intake.refusal is not None
    assert "not in anything the chemist has written" in intake.refusal


async def test_a_store_that_cannot_be_read_refuses_rather_than_waives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that cannot be read refuses rather than waives: the degraded ambient is empty."""
    intake = _Intake("24 wells")

    class _Broken(InMemoryHistoryProvider):
        async def recent_user_texts(self, session_id: Any, *, limit: int, state: Any = None) -> Any:
            raise ConnectionError("the session store is unreachable")

    session = TurnSession(session_id="s-broken-store")
    await _drive(session, _Broken(), [(_TURN_ONE, _Quiet()), ("ok go ahead", intake)])

    assert intake.saw == ("ok go ahead",)
    assert intake.refusal is not None


async def test_the_durable_transcript_serves_the_same_window() -> None:
    """The deployed path: `session_store="postgres"`, over rows a previous turn actually wrote."""
    intake = _Intake("24 wells")

    await migrated_db_or_skip()
    history = PostgresHistoryProvider()
    session = TurnSession(session_id="s-durable-stated-quote")
    try:
        await _drive(
            session,
            history,
            [
                (_TURN_ONE, _Quiet()),
                ("what about the base?", _Quiet()),
                ("ok go ahead", intake),
            ],
        )
    finally:
        from chemclaw.core import db

        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM session_messages WHERE session_id = %s", (session.session_id,)
                )

    assert intake.saw == (_TURN_ONE, "what about the base?", "ok go ahead")
    assert intake.refusal is None, intake.refusal
