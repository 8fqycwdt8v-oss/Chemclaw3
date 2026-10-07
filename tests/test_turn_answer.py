"""Which of a turn's prose is the answer when the model narrates before calling a tool.

Driven against `_stream_into`, which both the model run and the mid-turn resume collect through.
`AnswerEvent.text` is what `build_answer_event` grades, so a preamble left in it would be graded
and stored as part of the answer.
"""

import asyncio
from collections.abc import AsyncIterator

from chemclaw.agent.turn_usage import TurnUsage
from chemclaw.api.events import Event, TokenEvent, ToolCallEvent, ToolResultEvent
from chemclaw.api.runner import _stream_into, _TurnLedger


def _collect(events: list[Event]) -> _TurnLedger:
    """Drive `_stream_into` over `events` and hand back the ledger it filled."""
    ledger = _TurnLedger(correlation_id="c1", usage=TurnUsage())

    async def _drive() -> None:
        async def _stream() -> AsyncIterator[Event]:
            for event in events:
                yield event

        async for _event in _stream_into(_stream(), ledger):
            pass

    asyncio.run(_drive())
    return ledger


def test_the_answer_is_the_prose_of_the_last_model_call() -> None:
    """A preamble written before any tool output is not part of the answer."""
    ledger = _collect(
        [
            TokenEvent(text="Let me look that up."),
            ToolCallEvent(tool="find_notes", arguments="{}"),
            ToolResultEvent(tool="find_notes", preview="matches=[…]"),
            TokenEvent(text="THE FINAL ANSWER IS 42."),
        ]
    )
    assert ledger.answer_text == "THE FINAL ANSWER IS 42."


def test_prose_the_turn_never_interrupted_is_the_answer_whole() -> None:
    """The ordinary turn is untouched: with no tool call, every token is still the answer."""
    ledger = _collect([TokenEvent(text="Two notes cover "), TokenEvent(text="this coupling.")])
    assert ledger.answer_text == "Two notes cover this coupling."


def test_a_helper_s_tool_call_does_not_discard_the_supervisor_s_prose() -> None:
    """Only the supervisor's own tool calls end the supervisor's paragraph.

    A helper's calls arrive attributed (`agent="subagent"`) inside the `task` call; cutting on them
    would delete the answer of every turn that delegated.
    """
    ledger = _collect(
        [
            TokenEvent(text="Here is what I found: "),
            ToolCallEvent(tool="find_notes", arguments="{}", agent="subagent"),
            TokenEvent(text="two notes."),
        ]
    )
    assert ledger.answer_text == "Here is what I found: two notes."


def test_the_time_to_first_token_is_still_the_turn_s_first_token() -> None:
    """Discarding the preamble from the answer does not move the time to first token.

    `ttft_seconds` measures what the chemist experienced, which is the turn's first token.
    """
    ledger = _collect(
        [
            TokenEvent(text="Let me look that up."),
            ToolCallEvent(tool="find_notes", arguments="{}"),
            TokenEvent(text="42."),
        ]
    )
    assert ledger.ttft_seconds is not None
    assert ledger.answer_text == "42."
