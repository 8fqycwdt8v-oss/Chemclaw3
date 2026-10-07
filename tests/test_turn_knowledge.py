"""What a turn looked at, cited and wrote back, recorded on `turn_costs`.

The event stream ends with the turn and `session_messages` holds prose rather than which tool ran,
so whether a turn consulted the knowledge record is only answerable if it is booked at the time.
That makes the system prompt's search-before-answering obligation measurable.
"""

import pytest

from chemclaw.agent.authz import (
    KNOWLEDGE_READ_TOOLS,
    KNOWLEDGE_WRITE_TOOLS,
    READ_ONLY_TOOLS,
    knowledge_read_tools,
    side_effecting_tools,
)
from chemclaw.api.events import (
    AnswerEvent,
    JobStartedEvent,
    ToolCallEvent,
    ToolFailedEvent,
)


def _ledger() -> object:
    """A fresh turn ledger, built the way the runner builds one."""
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner import _TurnLedger

    return _TurnLedger(correlation_id="c-1", usage=TurnUsage(), started=0.0)


def test_a_turn_that_searched_the_record_is_distinguishable_from_one_that_did_not() -> None:
    """A turn that searched the record is distinguishable from one that answered without looking.

    Counted off `ToolCallEvent` in `note_event`, so a subagent's search and a mid-turn resume are
    counted the same way as the supervisor's.
    """
    silent, searching = _ledger(), _ledger()
    searching.note_event(ToolCallEvent(tool="gather_evidence", arguments=""))  # type: ignore[attr-defined]
    searching.note_event(ToolCallEvent(tool="expand_note", arguments=""))  # type: ignore[attr-defined]

    assert silent.retrieval_calls == 0  # type: ignore[attr-defined]
    assert searching.retrieval_calls == 2  # type: ignore[attr-defined]


def test_a_write_is_counted_as_capture_rather_than_as_retrieval() -> None:
    """The two sides are separate questions and must not be summed into "tool calls"."""
    ledger = _ledger()
    ledger.note_event(ToolCallEvent(tool="record_knowledge_note", arguments=""))  # type: ignore[attr-defined]

    assert ledger.capture_calls == 1  # type: ignore[attr-defined]
    assert ledger.retrieval_calls == 0  # type: ignore[attr-defined]


def test_a_calculation_is_not_a_capture() -> None:
    """A calculation is not a capture.

    `capture_calls` answers "did this turn write anything back", not "did it change something", so
    it is not derived from `side_effecting_tools()`.
    """
    ledger = _ledger()
    ledger.note_event(ToolCallEvent(tool="compute_xtb_energy", arguments=""))  # type: ignore[attr-defined]
    ledger.note_event(ToolCallEvent(tool="record_failure", arguments=""))  # type: ignore[attr-defined]

    assert ledger.capture_calls == 1  # type: ignore[attr-defined]
    assert ledger.tool_calls == 2  # type: ignore[attr-defined]


def test_every_knowledge_write_tool_is_still_gated() -> None:
    """The guard the second hand-written subset needs, symmetric with the read one.

    A write that stops being state-changing would go on being counted as a capture while no
    longer passing the plan gate — two claims about the same tool, disagreeing silently.
    """
    assert KNOWLEDGE_WRITE_TOOLS <= side_effecting_tools(), (
        f"not gated any more: {sorted(KNOWLEDGE_WRITE_TOOLS - side_effecting_tools())}"
    )


def test_a_tool_that_is_neither_moves_neither_count() -> None:
    """`ask_clarifying_question` is read-only and consults nothing.

    `KNOWLEDGE_READ_TOOLS` is a stated subset rather than the authz partition, which answers a
    different question.
    """
    ledger = _ledger()
    ledger.note_event(ToolCallEvent(tool="ask_clarifying_question", arguments=""))  # type: ignore[attr-defined]
    ledger.note_event(JobStartedEvent(job_id="j-1", kind="calc"))  # type: ignore[attr-defined]

    assert ledger.retrieval_calls == 0  # type: ignore[attr-defined]
    assert ledger.capture_calls == 0  # type: ignore[attr-defined]
    assert ledger.tool_calls == 1  # type: ignore[attr-defined]


def test_the_answers_own_grade_is_kept_instead_of_being_streamed_and_dropped() -> None:
    """`score_answer` runs on every production turn; nothing stored what it decided."""
    ledger = _ledger()
    ledger.note_event(  # type: ignore[attr-defined]
        AnswerEvent(
            text="We used [[playbook-degassing]] and [[rxn-suzuki-biaryl]] for this.",
            confidence=0.42,
            review_required=True,
        )
    )

    assert ledger.answer_confidence == 0.42  # type: ignore[attr-defined]
    assert ledger.review_required is True  # type: ignore[attr-defined]
    assert ledger.notes_cited == 2  # type: ignore[attr-defined]


def test_an_ungraded_turn_records_no_confidence_rather_than_a_zero() -> None:
    """`None` is not a low score, and storing 0 would say the answer was graded and graded terrible.

    `review_required` can be True while `confidence is None` — the deterministic answer-shape gate
    found something, and that is not a score. The column is nullable for exactly this row.
    """
    ledger = _ledger()
    ledger.note_event(AnswerEvent(text="no citations here", review_required=True))  # type: ignore[attr-defined]

    assert ledger.answer_confidence is None  # type: ignore[attr-defined]
    assert ledger.review_required is True  # type: ignore[attr-defined]
    assert ledger.notes_cited == 0  # type: ignore[attr-defined]


def test_every_knowledge_read_tool_is_still_a_read() -> None:
    """Every knowledge-read tool is still read-only.

    The subset is written out by hand, so this fails if one of its tools becomes state-changing.
    """
    assert KNOWLEDGE_READ_TOOLS <= READ_ONLY_TOOLS, (
        f"not read-only any more: {sorted(KNOWLEDGE_READ_TOOLS - READ_ONLY_TOOLS)}"
    )


def test_the_row_carries_every_dimension_the_ledger_counted() -> None:
    """The store's column list and the `TurnCost` model do not drift apart.

    A field missing from `_COLUMNS` would be written nowhere and raise nothing.
    """
    from chemclaw.agent.turn_cost_store import _COLUMNS
    from chemclaw.core.turn_cost import TurnCost

    for field in (
        "retrieval_calls",
        "capture_calls",
        "answer_confidence",
        "review_required",
        "notes_cited",
    ):
        assert field in TurnCost.model_fields, f"{field} is not on the model"
        assert field in _COLUMNS, f"{field} is on the model and not written to the row"


def test_the_answers_grade_reaches_the_booked_row_and_not_only_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer's grade reaches the booked row, end to end.

    `AnswerEvent` is built in `run_turn` rather than streamed, so unit tests of `note_event` cannot
    show it is counted. This drives `run_turn` and asserts the booked row against the yielded event,
    so it stays true when the verifier fills `confidence` in.
    """
    import asyncio
    from collections.abc import AsyncIterator

    from chemclaw.agent.session import TurnSession
    from chemclaw.api import runner
    from chemclaw.core.turn_cost import TurnCost
    from tests.fakes_turn import Piece, ScriptedTurn

    class _CitingAgent(ScriptedTurn):
        """A turn whose answer cites two notes, which is what makes `notes_cited` non-trivial."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield "We reused [[playbook-degassing]] and [[rxn-suzuki-biaryl]] here."

    booked: list[TurnCost] = []
    answers: list[AnswerEvent] = []

    async def _drive() -> None:
        async for event in runner.run_turn(
            TurnSession(session_id="s-knowledge-e2e"),
            "hi",
            connectors=[],
            graph_factory=_CitingAgent().graph_factory,
        ):
            if isinstance(event, AnswerEvent):
                answers.append(event)

    monkeypatch.setattr(runner, "record_turn_cost", booked.append)
    asyncio.run(_drive())

    assert len(answers) == 1, "the turn did not answer, so this proves nothing about the row"
    assert len(booked) == 1
    row, answer = booked[0], answers[0]
    assert row.notes_cited == 2, "the citations the chemist can see never reached the row"
    assert row.answer_confidence == answer.confidence
    assert row.review_required == answer.review_required


def test_a_bundles_search_over_the_record_counts_as_retrieval() -> None:
    """A bundle's search over the record counts as retrieval.

    Each bundle declares its own `knowledge_read` tools, so core does not copy another bundle's
    classification; the counted set is the union.
    """
    ledger = _ledger()
    ledger.note_event(ToolCallEvent(tool="substrate_precedent", arguments=""))  # type: ignore[attr-defined]
    ledger.note_event(ToolCallEvent(tool="similar_molecules", arguments=""))  # type: ignore[attr-defined]

    assert ledger.retrieval_calls == 2  # type: ignore[attr-defined]


def test_a_declared_knowledge_read_must_be_one_of_the_endpoints_own_reads() -> None:
    """The one pair that cannot both be true: a write counted as a look."""
    from chemclaw.connectors.manifest import HttpEndpoint

    with pytest.raises(ValueError, match="knowledge_read"):
        HttpEndpoint(
            url="http://127.0.0.1:8899/mcp",
            tools=["look", "write"],
            read_only=["look"],
            state_changing=["write"],
            knowledge_read=["write"],
        )


def test_the_declared_reads_of_every_enabled_bundle_reach_the_counted_set() -> None:
    """The union, end to end: a manifest declaration with no reader is a claim, not a control."""
    from chemclaw.connectors.registry import knowledge_read_tool_names

    counted = knowledge_read_tools()
    assert KNOWLEDGE_READ_TOOLS <= counted
    assert frozenset(knowledge_read_tool_names()) <= counted


def test_a_refused_or_failed_call_is_not_a_consultation() -> None:
    """A refused or failed call is not a consultation.

    The knowledge counts measure what was read, so a refused or failed call is taken back.
    `tool_calls`, `tool_failures` and `tool_refusals` are attempt counts and must not move: a
    refusal is the control working, not a call that never occurred.
    """
    ledger = _ledger()
    ledger.note_event(ToolCallEvent(tool="find_notes", arguments=""))  # type: ignore[attr-defined]
    ledger.note_event(ToolCallEvent(tool="find_notes", arguments=""))  # type: ignore[attr-defined]
    ledger.note_event(  # type: ignore[attr-defined]
        ToolFailedEvent(tool="find_notes", message="already asked", reason="repeat")
    )
    ledger.note_event(ToolCallEvent(tool="expand_note", arguments=""))  # type: ignore[attr-defined]
    ledger.note_event(  # type: ignore[attr-defined]
        ToolFailedEvent(tool="expand_note", message="no such note")
    )
    assert ledger.retrieval_calls == 1, (  # type: ignore[attr-defined]
        "a refused and a raised call were counted as consultations of the record"
    )
    assert ledger.tool_calls == 3  # type: ignore[attr-defined]
    assert ledger.tool_refusals == 1  # type: ignore[attr-defined]
    assert ledger.tool_failures == 1  # type: ignore[attr-defined]


def test_a_write_that_was_refused_is_not_a_capture_either() -> None:
    """A refused write is not a capture either, and the count never goes negative.

    A failure event may arrive for a call this ledger never saw start (a subagent's, a resumed
    run's).
    """
    ledger = _ledger()
    ledger.note_event(  # type: ignore[attr-defined]
        ToolCallEvent(tool="record_knowledge_note", arguments="")
    )
    ledger.note_event(  # type: ignore[attr-defined]
        ToolFailedEvent(tool="record_knowledge_note", message="not approved", reason="plan_gate")
    )
    assert ledger.capture_calls == 0  # type: ignore[attr-defined]
    ledger.note_event(  # type: ignore[attr-defined]
        ToolFailedEvent(tool="record_knowledge_note", message="orphan", reason="plan_gate")
    )
    assert ledger.capture_calls == 0, "an unpaired failure drove the count below zero"  # type: ignore[attr-defined]
