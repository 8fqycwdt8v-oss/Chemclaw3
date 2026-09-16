"""A flagged answer goes back for another pass, in the same turn.

ADR: `D-2026-09-15-a-flagged-answer-that-goes-out-flagged-is-a-verdict-nobody-acted-on`.

`agent/verifier.py` has always *marked* an answer whose claims its evidence does not support, and
`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer` states in as many words what
happened next: "Nothing routes a flagged answer back for another pass." These tests drive the loop
that does, through the real `run_turn` over a real compiled graph (`tests/fakes_turn`), because the
thing under test is the *runner* and a stand-in for the graph would prove only that the stand-in was
called.

**The test that matters most is the one where revising does not help.** That is the shape
D-2026-08-16 found `RubricMiddleware` lacking — its `_finalize_evaluation` rewrites the result to
`max_iterations_reached` and mutates no message, so a grader outage ships every answer ungraded with
a log line nothing reads. Here, running out of rounds must leave the answer exactly as it would have
been with the loop off: shipped, and still marked.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

import chemclaw.agent.verifier as verifier_module
import chemclaw.api.runner as runner
from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.checkpointer import checkpointer, close_checkpointer
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
from chemclaw.agent.state import turn_config
from chemclaw.agent.verifier import ClaimCheck, VerificationResult
from chemclaw.api.events import AnswerEvent, ErrorEvent, ToolFailedEvent
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.fakes_langgraph import ScriptedChatModel
from tests.fakes_turn import Chunk, Piece, ScriptedTurn
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

#: The sentence `_revision_message` opens with. Matched rather than restated in full, so a reworded
#: prompt does not silently stop these fakes from recognising a revision.
_SENT_BACK = "not supported by it"

#: The session the thread-hygiene arm below drives, kept apart from `_drive`'s so the messages it
#: reads back belong to exactly one turn.
_THREAD_SESSION = "sess-revision-thread"

_FLAGGED = "Yield was 90% [[reaction-a]]."
_GROUNDED = "The evidence does not settle the yield [[reaction-a]]."


class _RevisingAgent(ScriptedTurn):
    """Answers with an unsupported claim, and answers differently when sent back."""

    def __init__(self) -> None:
        """Count the runs, so a test can assert how many times the graph was driven."""
        self.runs = 0

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """The flagged answer first; the grounded one on any revision pass."""
        self.runs += 1
        yield _GROUNDED if _SENT_BACK in message else _FLAGGED


class _StubbornAgent(ScriptedTurn):
    """Answers the same unsupported way however many times it is sent back."""

    def __init__(self) -> None:
        """Count the runs, so exhaustion can be told from a loop that never ran."""
        self.runs = 0

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """The same flagged prose every time."""
        self.runs += 1
        yield _FLAGGED


def _grades_by_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verifier that flags the unsupported answer and passes the grounded one.

    Keyed on the answer's own text rather than on a call counter, so it grades what it is given —
    a counter would pass the second call whatever the model said, which is the assertion inverted.
    """
    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    async def _verify(answer: str, *_: Any, **__: Any) -> VerificationResult:
        if answer.strip() == _GROUNDED:
            return VerificationResult(claims=[], confidence=1.0, verified_by="judge")
        return VerificationResult(
            claims=[ClaimCheck(text="Yield was 90%", supported=False)],
            confidence=0.2,
            verified_by="judge",
        )

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _verify)


def _drive(agent: ScriptedTurn) -> list[Any]:
    """One turn's events, with no connectors."""

    async def _collect() -> list[Any]:
        session = TurnSession(session_id="s-revise")
        return [
            event
            async for event in runner.run_turn(
                session, "what was the yield?", connectors=[], graph_factory=agent.graph_factory
            )
        ]

    return asyncio.run(_collect())


def _answer(events: list[Any]) -> AnswerEvent:
    """The turn's answer event."""
    return next(e for e in events if isinstance(e, AnswerEvent))


def test_a_flagged_answer_is_sent_back_and_the_revision_is_what_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the answer a chemist receives is the grounded one.

    And it is the revision *alone*. `ledger.answer_text` joins the accumulated parts, so without
    clearing them the answer would be the flagged prose with the corrected prose stapled to its end
    — which is worse than either, and is what the mid-turn resume does on purpose because a resume
    continues an answer where a revision replaces one.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)
    agent = _RevisingAgent()

    answer = _answer(_drive(agent))

    assert answer.text == _GROUNDED
    assert _FLAGGED not in answer.text, "the revision was appended to the flagged answer"
    assert answer.review_required is False
    assert agent.runs == 2, "the graph was not driven a second time"


def test_a_revision_that_does_not_help_still_answers_and_stays_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhaustion is the case that must not silently ship, and must not swallow the turn either.

    A deployment that runs out of rounds ends up exactly where it would have been with the loop off:
    the answer goes out, carrying `review_required`. Nothing is lost that was not already lost — the
    second model call simply bought nothing, which is what the counter is for.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)
    agent = _StubbornAgent()

    before = METRICS.value("chemclaw_answer_review_exhausted_total")
    answer = _answer(_drive(agent))

    assert answer.text == _FLAGGED, "the turn must still answer"
    assert answer.review_required is True, "an exhausted loop must not clear the verdict"
    assert agent.runs == 3, "one original pass plus two revisions"
    assert METRICS.value("chemclaw_answer_review_exhausted_total") == before + 1


def test_the_loop_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 0 rounds the turn is byte-for-byte what it was before this loop existed.

    The shipped configuration, so this is the arm that says the feature costs nothing until a
    deployment asks for it.
    """
    _grades_by_text(monkeypatch)
    assert settings.answer_review_max_rounds == 0, "the loop must ship off"
    agent = _StubbornAgent()

    before = METRICS.value("chemclaw_answer_revisions_total")
    answer = _answer(_drive(agent))

    assert answer.text == _FLAGGED
    assert answer.review_required is True
    assert agent.runs == 1, "no revision may run with the loop off"
    assert METRICS.value("chemclaw_answer_revisions_total") == before


def test_a_grounded_answer_is_never_sent_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop triggers on the verdict, not on the setting being on.

    Without this, a revision loop that re-ran unconditionally would pass every other test here and
    double the model spend of every turn in the deployment.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)

    class _GoodAgent(ScriptedTurn):
        """Answers in a way the verifier accepts first time."""

        def __init__(self) -> None:
            self.runs = 0

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            self.runs += 1
            yield _GROUNDED

    agent = _GoodAgent()
    answer = _answer(_drive(agent))

    assert answer.review_required is False
    assert agent.runs == 1, "a grounded answer was sent back anyway"


def test_the_revision_names_the_claims_rather_than_saying_try_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revision prompt with no specifics licenses rewording instead of regrounding.

    Asserted on the message the graph is actually driven with, captured off the fake, because the
    text is the whole mechanism: `_revision_message` naming nothing would leave every other test
    here passing.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 1)

    seen: list[str] = []

    class _CapturingAgent(ScriptedTurn):
        """Records every message it is driven with."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            seen.append(message)
            yield _GROUNDED if _SENT_BACK in message else _FLAGGED

    _drive(_CapturingAgent())

    assert len(seen) == 2, "the revision did not run"
    assert "Yield was 90%" in seen[1], "the revision must name the claim it is about"
    assert "<retrieved-note" in seen[1] or "unsupported-claims" in seen[1], (
        "the model's own prose is quoted back at it, so it must arrive framed as data"
    )


class _EmptyRevisionAgent(ScriptedTurn):
    """Answers, is flagged, and then produces nothing at all when it is sent back."""

    def __init__(self) -> None:
        """Count the runs, so the break-out can be told from a loop that never started."""
        self.runs = 0

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """The flagged answer first; an empty reply on the revision."""
        self.runs += 1
        yield "" if _SENT_BACK in message else _FLAGGED


class _RaisingRevisionAgent(ScriptedTurn):
    """Answers, is flagged, and then the gateway drops the revision."""

    def __init__(self) -> None:
        """Count the runs, so the failure can be told from a loop that never started."""
        self.runs = 0

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """The flagged answer first; a gateway failure on the revision."""
        self.runs += 1
        if _SENT_BACK in message:
            raise RuntimeError("gateway 503")
        yield _FLAGGED


def test_a_revision_that_produces_no_text_ships_the_answer_the_turn_already_had(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank round must not turn a usable flagged answer into a blank bubble.

    `_revise_answer` clears `answer_parts` before the round, because a revision *replaces* an
    answer; the emptiness guard had already run, against the *flagged* text. So a round that
    produced nothing shipped `AnswerEvent(text='')` booked `outcome='answered', completed=True` —
    strictly worse than the un-looped turn, and invisible, because
    `chemclaw_turn_empty_answers_total` had already declined to fire on the text that *was* there.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)
    agent = _EmptyRevisionAgent()

    before = METRICS.value("chemclaw_turn_empty_answers_total")
    events = _drive(agent)
    answer = _answer(events)

    assert answer.text == _FLAGGED, "the answer the turn already had was destroyed"
    assert answer.review_required is True, "an empty round must not clear the verdict"
    assert agent.runs == 2, "a round that bought nothing must stop the loop, not spend the rest"
    assert METRICS.value("chemclaw_turn_empty_answers_total") == before, (
        "the turn answered, so nothing may be counted as a silent turn"
    )


def test_a_revision_that_raises_ships_the_answer_the_turn_already_had(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway failure on the second call must not cost the chemist the first call's answer.

    The round was unwrapped, so a raise propagated to `run_turn`'s `except Exception` and the
    chemist got a generic internal error in place of a complete, already-graded answer the turn was
    holding — and `_record_review_rounds` never ran, so the exhaustion counter was blind to the
    entire class.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)
    agent = _RaisingRevisionAgent()

    before = METRICS.value("chemclaw_answer_review_exhausted_total")
    events = _drive(agent)

    assert not [e for e in events if isinstance(e, ErrorEvent) and e.code == "internal"], (
        "a failed revision sank the turn"
    )
    answer = _answer(events)
    assert answer.text == _FLAGGED
    assert answer.review_required is True
    assert agent.runs == 2, "the loop must stop at the failure rather than retry into it"
    assert METRICS.value("chemclaw_answer_review_exhausted_total") == before + 1, (
        "a revision that failed is a revision that bought nothing, and must be counted as one"
    )


def test_a_spend_cap_tripped_inside_a_revision_is_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cap that fires during the revision must reach the chemist, not just stop the graph.

    Both guards were evaluated once, above the loop, so a turn whose *second* model call tripped
    the spend cap emitted no `ErrorEvent` at all and booked `outcome='answered', completed=True`.
    The cap always enforced — it shares `cap_carry` across both invocations — it simply could not
    report. Driven through the real `agent/spend_cap.py` with a budget the first pass exceeds, so
    the revision's `before_model` is where it fires.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 50)

    class _ExpensiveAgent(ScriptedTurn):
        """One flagged answer that costs more than the whole turn's budget."""

        def __init__(self) -> None:
            self.runs = 0

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            self.runs += 1
            yield Chunk(text=_FLAGGED, output_tokens=200)

    agent = _ExpensiveAgent()
    events = _drive(agent)

    codes = [e.code for e in events if isinstance(e, ErrorEvent)]
    assert "spend_cap_reached" in codes, f"the cap fired and said nothing: {codes}"
    assert codes.count("spend_cap_reached") == 1, "one firing is one event"
    # The turn still answers, and with the text it had: the cap jumps the revision `to end`, so the
    # round produces nothing and the held answer is restored.
    assert _answer(events).text == _FLAGGED


def test_a_verdict_with_no_claim_to_act_on_is_never_sent_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Low confidence with every claim supported is a flag, not a thing to re-answer.

    `_revision_message` frames whatever it is given, so an empty `unsupported` list produced
    exactly the "just try again" prompt the wording exists to avoid — `answer_review_max_rounds`
    times, for every such turn in the deployment.
    """
    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)

    async def _unsure(answer: str, *_: Any, **__: Any) -> VerificationResult:
        """Every claim supported, and the judge still unsure of the whole."""
        return VerificationResult(
            claims=[ClaimCheck(text="Yield was 90%", supported=True)],
            confidence=0.2,
            verified_by="judge",
        )

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _unsure)
    agent = _StubbornAgent()

    before = METRICS.value("chemclaw_answer_revisions_total")
    answer = _answer(_drive(agent))

    assert answer.review_required is True, "the verdict still stands"
    assert answer.unsupported_claims == [], "the fixture's premise"
    assert agent.runs == 1, "a prompt naming nothing was sent anyway"
    assert METRICS.value("chemclaw_answer_revisions_total") == before


def test_a_judge_outage_does_not_multiply_every_flagged_turn_s_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status a crashed check leaves behind is not a claim the model can drop.

    `score_answer` flags a crashed verifier, and that status used to land in `unsupported` — so the
    loop quoted "verification did not run" back at the model as prose to re-ground. The judge keeps
    failing, so every round exhausted: a judge outage multiplied every flagged turn's model spend
    by `answer_review_max_rounds + 1`, fleet-wide, for nothing. The wire is unchanged, which is the
    second assertion: a reviewer still reads the reason.
    """
    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)

    async def _boom(answer: str, *_: Any, **__: Any) -> VerificationResult:
        """The judge this deployment cannot reach."""
        raise RuntimeError("judge down")

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _boom)
    agent = _StubbornAgent()

    before = METRICS.value("chemclaw_answer_revisions_total")
    answer = _answer(_drive(agent))

    assert answer.review_required is True
    assert answer.unsupported_claims == ["verification did not run"], (
        "the reviewer's reason must still reach the wire"
    )
    assert agent.runs == 1, "a status string was sent back as a claim to drop"
    assert METRICS.value("chemclaw_answer_revisions_total") == before


def test_the_turn_counter_moves_once_for_a_turn_that_revised_and_never_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The denominator the exhaustion alert divides by, in the unit the numerator is in.

    `chemclaw_answer_revisions_total` counts *passes*, so dividing exhausted turns by it compared
    two units: with every flagged turn exhausting, the ratio read 1.00 at one allowed round and
    0.20 at five — silent at total failure for every setting but the smallest, with its own advice
    (allow more rounds) pushing it further below threshold. Both directions are asserted, because a
    counter that also moves on the off path is a denominator that never shrinks.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)

    before = METRICS.value("chemclaw_answer_review_turns_total")
    _drive(_StubbornAgent())
    exhausted = METRICS.value("chemclaw_answer_review_turns_total")
    assert exhausted == before + 1, "a turn that exhausted its rounds still entered the loop"

    _drive(_RevisingAgent())
    assert METRICS.value("chemclaw_answer_review_turns_total") == exhausted + 1, (
        "a turn that revised successfully entered the loop too"
    )

    monkeypatch.setattr(settings, "answer_review_max_rounds", 0)
    _drive(_StubbornAgent())
    assert METRICS.value("chemclaw_answer_review_turns_total") == exhausted + 1, (
        "the off path must stay a complete no-op"
    )


def test_a_revision_still_reaches_the_turn_s_connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pass that exists to re-ground an answer must still have the evidence tools.

    **Every other arm here passes `connectors=[]`, which is exactly why this was invisible.**
    `_open_turn_surface` enters one `HeldConnectorSession` per bundle on the turn's
    `AsyncExitStack`, and the revision loop used to sit *below* that block — so the stack had
    already unwound and every MCP tool was dead during the revision. In-process tools kept working,
    which made it silent rather than obvious. Probed on this fixture before the fix:
    `session OPENED -> TokenEvent -> session CLOSED -> [revision] -> call on a closed session ->
    ToolFailedEvent`.

    The fake session is entered on the runner's own stack through the seam the runner uses, so what
    is asserted is the stack's lifetime rather than a stand-in's.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 1)

    open_when_called: list[bool] = []
    session_open = False

    @tool
    def connector_probe(query: str) -> str:
        """Look the reaction up in the connector this turn opened."""
        open_when_called.append(session_open)
        return f"{query}: the evidence [[reaction-a]]"

    @asynccontextmanager
    async def _held_session() -> AsyncIterator[None]:
        """Stand in for `HeldConnectorSession`: live while entered, dead once the stack unwinds."""
        nonlocal session_open
        session_open = True
        try:
            yield
        finally:
            session_open = False

    async def _open(stack: AsyncExitStack, _specs: Any) -> tuple[list[Any], list[str]]:
        """The seam `_open_turn_surface` calls, entering the session on the turn's own stack."""
        await stack.enter_async_context(_held_session())
        return [connector_probe], []

    monkeypatch.setattr(runner, "open_connector_specs", _open)

    def _factory(**kwargs: Any) -> Any:
        """The real compiled graph, over a model that calls the connector on the revision."""
        kwargs["audit_sink"] = NullAuditSink()
        return build_langgraph_agent(
            model=ScriptedChatModel(
                [_FLAGGED, {"name": "connector_probe", "args": {"query": "yield"}}, _GROUNDED]
            ),
            **kwargs,
        )

    async def _collect() -> list[Any]:
        return [
            event
            async for event in runner.run_turn(
                TurnSession(session_id="s-revise-connector"),
                "what was the yield?",
                connectors=[],
                graph_factory=_factory,
            )
        ]

    events = asyncio.run(_collect())

    assert open_when_called == [True], (
        "the revision's connector call ran against a torn-down session"
    )
    failed = [e for e in events if isinstance(e, ToolFailedEvent)]
    assert not failed, [e.message for e in failed]
    assert _answer(events).text == _GROUNDED


def test_the_dev_page_replaces_the_answer_bubble_rather_than_only_filling_it() -> None:
    """This repository's own reference client must not render the answer the loop retracted.

    `case "token"` appends to the bubble, so by the time the `answer` event arrives the element
    holds the *flagged* prose the first pass streamed. Filling only an empty element therefore
    displayed flagged-text + revised-text concatenated and dropped `AnswerEvent.text`, which is the
    authoritative answer. Asserted against the source because the page is plain script with no
    harness to execute it — the same way `test_dev_page_events.py` checks its `switch`.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "api" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    body = source.split('case "answer":', 1)[1].split("case ", 1)[0]

    assert "answerEl.textContent = evt.text" in body, (
        "the answer event must replace the bubble's text, not only create it"
    )
    assert 'add("assistant", evt.text)' not in body, (
        "filling only an empty element leaves the retracted prose on screen"
    )


def _turn_on_a_real_thread(agent: ScriptedTurn, session_id: str) -> tuple[list[Any], list[Any]]:
    """Drive one turn against a real `AsyncPostgresSaver` and read the thread back.

    The in-memory default keeps no second record, so nothing about what survives a turn can be
    asserted off it — which is why every other arm in this file cannot see the thread at all.
    """

    async def _run() -> tuple[list[Any], list[Any]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        # Any saver a previous test published belongs to a closed loop; this turn builds its own.
        await close_checkpointer()
        await SessionOwnerStore().record(session_id, "oid-revision", None)
        try:
            events = [
                event
                async for event in runner.run_turn(
                    TurnSession(session_id=session_id),
                    "what was the yield?",
                    history=PostgresHistoryProvider(),
                    connectors=[],
                    graph_factory=agent.graph_factory,
                )
            ]
            # `turn_config` is typed `dict[str, Any]` — the checkpointer wants a `RunnableConfig`,
            # which that dict satisfies structurally but not nominally.
            config = cast(RunnableConfig, turn_config(session_id))
            saved = await (await checkpointer()).aget_tuple(config)
            assert saved is not None, "the turn stored no thread at all"
            return events, list(saved.checkpoint["channel_values"]["messages"])
        finally:
            await close_checkpointer()

    return asyncio.run(_run())


def test_neither_the_revision_prompt_nor_the_retracted_answer_stays_on_the_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's own record of the conversation must not carry this system talking to itself.

    `turn_input` makes `_revision_message` a `("user", …)` message and the Postgres checkpointer
    persists it, so the chemist's *next* turn opened on the retracted `ai` claim, then a `human`
    message they never wrote, then their real question. `ledger.exchanges` collects only
    tool-bearing messages, so `session_messages` had neither — the transcript and the thread
    disagreed, uncounted, and the claim this system had just rejected stayed restatable for the
    rest of the conversation.

    Driven on a real `AsyncPostgresSaver` rather than the in-memory default, because the whole
    subject is what survives the turn.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 1)
    # `_turn_checkpointer` builds a saver only on the Postgres store; off it there is no second
    # record to diverge from and nothing to withdraw.
    monkeypatch.setattr(settings, "session_store", "postgres")
    agent = _RevisingAgent()

    events, thread = _turn_on_a_real_thread(agent, _THREAD_SESSION)

    assert _answer(events).text == _GROUNDED, "the fixture's premise: the turn revised"
    assert agent.runs == 2
    kinds = [(message.type, str(message.content)) for message in thread]
    assert not [text for kind, text in kinds if kind == "human" and _SENT_BACK in text], (
        f"a message the chemist never wrote outlived the turn: {kinds}"
    )
    assert not [text for kind, text in kinds if kind == "ai" and _FLAGGED in text], (
        f"the retracted claim is still restatable next turn: {kinds}"
    )
    # The control: withdrawing two messages must not withdraw the conversation.
    assert [text for kind, text in kinds if kind == "human"] == ["what was the yield?"]
    assert [text for kind, text in kinds if kind == "ai"] == [_GROUNDED]


@pytest.mark.parametrize(
    ("label", "agent_factory", "session_id"),
    [
        ("the model returned nothing", _EmptyRevisionAgent, "sess-revision-thread-empty"),
        ("the gateway dropped the call", _RaisingRevisionAgent, "sess-revision-thread-raised"),
    ],
)
def test_a_round_that_bought_nothing_leaves_the_thread_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    agent_factory: Any,
    session_id: str,
) -> None:
    """The withdrawal is by outcome, and the outcome here is that the *previous* answer ships.

    The symmetric half of the arm above, and the one a single rule gets wrong in both directions.
    Withdrawing the retracted answer unconditionally would leave the model's record ending on the
    round's empty reply while the chemist holds the flagged answer; withdrawing nothing would leave
    the fabricated prompt on the thread, which is the whole defect. What must be true on either
    break-out path is that the thread is byte-for-byte what it was before the round: the chemist's
    question, then the answer they were actually given.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 1)
    monkeypatch.setattr(settings, "session_store", "postgres")

    events, thread = _turn_on_a_real_thread(agent_factory(), session_id)

    assert _answer(events).text == _FLAGGED, f"the fixture's premise ({label})"
    kinds = [(message.type, str(message.content)) for message in thread]
    assert kinds == [("human", "what was the yield?"), ("ai", _FLAGGED)], (
        f"the thread does not end on the answer that shipped ({label}): {kinds}"
    )
