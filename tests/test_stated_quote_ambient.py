"""The chemist's own words in this thread, and what a `basis="stated"` quote may be checked against.

`core/turn_text.py` binds the ambient and `agent/protocol_design_tools.require_quotes_are_verbatim`
grades against it. This file is about the *widening* — the ambient carried exactly the message that
started the turn in flight, while `structure_experiment_request` tells the model to call it "first …
while correcting it is still cheap", i.e. iteratively across turns. Measured before the widening: a
chemist who wrote "24 wells, no DMF, by Friday please." on turn 1 and "ok go ahead" on turn 3 had
the intake refused, because `'24 wells'` is not in "ok go ahead" — so an honest `stated` was
unrepresentable, and the remedy the refusal prescribed recorded a real chemist constraint as a model
inference.

Widening a check is where a check quietly stops checking, so the two properties that must survive it
are asserted here rather than assumed:

- **only a person's words enter it.** The model's own earlier prose quoted back as `stated` is
  exactly the fabrication `core/turn_text` exists to refuse, and a mid-turn job-result push-back
  enters the *graph* under the chemist's role — which is why the transcript, not the checkpointer,
  is the source. Both are driven end to end here, on a real compiled graph.
- **the window is bounded, and its refusal says so**, because the one thing worse than a refusal is
  one a chemist cannot act on.
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
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.turn_text import (
    get_current_user_texts,
    reset_current_user_texts,
    set_current_user_texts,
)
from chemclaw.protocols.models import ExperimentRequest, RequestField
from tests.fakes_turn import Piece, ScriptedTurn
from tests.pg import migrated_db_or_skip

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
    """The bound is real, and its refusal has to be something a chemist can act on.

    A quote from a message the window no longer reaches is indistinguishable, to the check, from
    one nobody ever wrote — so the refusal must name both possibilities and the remedy, or the
    model's only move is the mislabelling (`basis='inferred'`) this whole check exists to prevent.
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


def test_the_ambient_read_is_bounded_by_the_session_and_not_by_the_table() -> None:
    """The read on the answer path plans through the partial index, visiting no other session.

    **A plan assertion, because the wall clock is not the finding.** Two independent measurements of
    this statement agreed on the row counts and disagreed on the milliseconds by 50x; what is stable
    is which index the planner picks and how many rows it throws away, and those are what decide
    whether the read costs O(session) or O(table).

    Measured on a replica of this table — one 12,000-row session plus 120,000 newer rows across 300
    others — the shipped statement planned `Index Scan Backward using session_messages_pkey` and
    discarded **120,020** rows to return 20, **on every turn**, because Postgres has no statistics
    for the expression `message->>'type'` and takes the ordered primary-key walk expecting to stop
    early. Migration `098` makes the human rows directly addressable: 0 rows discarded, 14.8 ms to
    0.036 ms. Two other candidates were driven and are no-ops — `CREATE STATISTICS` on the
    expression, and hoisting the type test into Python — so this index is the fix rather than one of
    three bets.

    Sequential scans are taken away for the reason `tests/test_reaction_records.py::_index_behind`
    gives: a fixture table fits in one page, so the planner is right to scan it and the choice would
    say nothing about which indexes exist.
    """

    async def _run() -> str:
        await migrated_db_or_skip()
        async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
            await conn.execute("SET LOCAL enable_seqscan = off")
            cursor = await conn.execute(
                f"EXPLAIN (FORMAT JSON) {_SELECT_RECENT_USER_ROWS}",
                ("any-session", LANGCHAIN_SHAPE, 20),
            )
            row = await cursor.fetchone()
        plan = row[0][0]["Plan"] if row else {}
        nodes = [plan]
        while nodes:
            node = nodes.pop()
            if "Index Name" in node:
                return str(node["Index Name"])
            nodes.extend(node.get("Plans", []))
        return ""

    assert asyncio.run(_run()) == "session_messages_ambient_human_idx", (
        "the ambient read no longer plans through the partial index migration 098 added, so it is "
        "back to walking the primary key and discarding every other session's rows — O(table) on "
        "the answer path, once per turn"
    )


def test_the_window_the_runner_reads_is_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knob reaches the query, and `0` is exactly the behaviour this widening replaced.

    Asserted through the runner rather than against the provider, because the setting has two jobs
    there and a default of 20 hides both from every other case in this file. It **bounds** the read
    — the number the query is given is the configured one, not a constant the runner picked — and
    it makes the read **skippable**: at 0 the turn does not go to the store at all. That second
    half is the one nothing would notice otherwise, because both providers refuse a non-positive
    limit themselves, so dropping the runner's guard buys a per-turn round trip for a feature the
    deployment turned off and changes no answer.
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
    """A model quoting itself back as `stated` is the fabrication the check exists to refuse.

    The assistant says "let us run 96 wells" on turn 1 and the intake quotes it on turn 2. Nothing
    about the words distinguishes them from a chemist's; what does is who is recorded as having
    said them, which is why `chemist_words` filters the transcript rather than the check filtering
    the prose.
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
    """The degraded direction is the strict one — an outage must not open the check.

    A transcript is a rendering and no rendering fails an answered turn, so the read is
    best-effort. What it degrades *to* is what matters: no earlier words, which refuses a quote
    the turn could otherwise have accepted, rather than a permissive ambient.
    """
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
