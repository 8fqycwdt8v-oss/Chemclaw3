"""What a turn looked at, cited and wrote back — the dimensions `turn_costs` lacked.

That row has recorded a turn's spend since migration 033 and how it *ended* since 060. It could not
say whether the turn consulted the knowledge record at all. Two separate reviews of this system's
knowledge loop each had to answer that with a bespoke script, and the answer is not recoverable
after the fact: the event stream ends with the turn, and `session_messages` holds the prose rather
than which tool ran.

The one that motivated it: `retrieval_calls == 0` on a turn that made a claim about this
programme's own chemistry. `D-2026-09-04-a-ranker-that-sorts-alphabetically-is-not-a-ranker` put an
obligation to search before answering into the system prompt, and this column is the only way to
find out whether that obligation moved anything. A prompt rule nobody can measure is a hope.
"""

import pytest

from chemclaw.agent.authz import (
    KNOWLEDGE_READ_TOOLS,
    KNOWLEDGE_WRITE_TOOLS,
    READ_ONLY_TOOLS,
    knowledge_read_tools,
    side_effecting_tools,
)
from chemclaw.api.events import AnswerEvent, JobStartedEvent, ToolCallEvent


def _ledger() -> object:
    """A fresh turn ledger, built the way the runner builds one."""
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner import _TurnLedger

    return _TurnLedger(correlation_id="c-1", usage=TurnUsage(), started=0.0)


def test_a_turn_that_searched_the_record_is_distinguishable_from_one_that_did_not() -> None:
    """The zero that matters: a turn that answered without looking.

    Counted off `ToolCallEvent` in `note_event`, which the module calls "the one place the counts
    are taken" — so a subagent's search and a mid-turn resume are counted the same way as the
    supervisor's, because all three pass through it.
    """
    silent, searching = _ledger(), _ledger()
    searching.note_event(ToolCallEvent(tool="gather_evidence", arguments=""))  # type: ignore[attr-defined]
    searching.note_event(ToolCallEvent(tool="expand_note", arguments=""))  # type: ignore[attr-defined]

    assert silent.retrieval_calls == 0  # type: ignore[attr-defined]
    assert searching.retrieval_calls == 2  # type: ignore[attr-defined]


def test_a_write_is_counted_as_capture_rather_than_as_retrieval() -> None:
    """The two sides are separate questions and must not be summed into "tool calls"."""
    ledger = _ledger()
    ledger.note_event(ToolCallEvent(tool="propose_knowledge_note", arguments=""))  # type: ignore[attr-defined]

    assert ledger.capture_calls == 1  # type: ignore[attr-defined]
    assert ledger.retrieval_calls == 0  # type: ignore[attr-defined]


def test_a_calculation_is_not_a_capture() -> None:
    """The column answers "did this turn write anything back", not "did it change something".

    `capture_calls` was derived from `side_effecting_tools()`, which spans 49 tools — so a turn
    that computed one xTB energy and wrote nothing to the record booked a capture. That is a
    different question, and one `tool_calls` minus the reads already approximates.
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

    This is why `KNOWLEDGE_READ_TOOLS` is a stated subset rather than the authz partition: authz
    answers "may this run without approval", and counting by it would book a turn that asked the
    chemist a question as a turn that searched the record.
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
    """The guard the hand-written subset needs.

    `KNOWLEDGE_READ_TOOLS` is written out rather than derived, because "did this turn consult the
    record" is not the question authz partitions on. The risk that creates is a tool becoming
    state-changing while still being counted as a read — this is what fails when that happens.
    """
    assert KNOWLEDGE_READ_TOOLS <= READ_ONLY_TOOLS, (
        f"not read-only any more: {sorted(KNOWLEDGE_READ_TOOLS - READ_ONLY_TOOLS)}"
    )


def test_the_row_carries_every_dimension_the_ledger_counted() -> None:
    """The store's column list and the model must not drift apart.

    A field added to `TurnCost` and forgotten in `_COLUMNS` is written nowhere and raises nothing —
    the row is simply narrower than the code believes, which is the silent half of this kind of
    change.
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
    """The end-to-end arm, and the reason this file needed one.

    Every other test here calls `note_event` directly, and all of them passed while the three
    columns read off the `AnswerEvent` were `NULL/false/0` on every row a deployment has ever
    written — because that event is *built* in `run_turn` rather than streamed, so it never
    reached the one place the counts are taken. A reader with no caller passes its own unit tests
    by construction; this one drives `run_turn` and asserts what `record_turn_cost` was handed.

    It asserts the booked row *against the yielded event* rather than against literals, so the
    test says "these two agree" — which is the actual invariant — and stays true under a
    deployment where the verifier runs and fills `confidence` in.
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
    """The half core cannot name, and the reason the set is a union.

    `rxnfp` searches the reaction corpus and `molfp` the fingerprint index — both are the record a
    turn consults before answering, and both were invisible to this column, so a turn that answered
    entirely from `substrate_precedent` booked `retrieval_calls` zero. Counting them by listing
    them in core would be the copy of somebody else's classification D-118 forbids; each bundle
    declares its own `knowledge_read`.
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
