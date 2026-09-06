"""Which of a turn's prose is the *answer*, when the model narrates before it calls a tool.

Driven against `_stream_into` — the one function both the model run and the mid-turn resume collect
through — because that is where the decision is made. The end-to-end shape it corresponds to was
measured on 2026-09-06 against a real OpenAI-compatible gateway that answers a first call with
`"Let me look that up."` plus a `find_notes` call, and a second call with
`"THE FINAL ANSWER IS 42."`: the `answer` event read
`'Let me look that up.THE FINAL ANSWER IS 42.'` — no separator — and
`GET /sessions/{id}/messages` held the preamble twice, once as its own assistant row and once
inside the answer row.

That is a correctness question rather than a cosmetic one: `AnswerEvent.text` is the string
`runner_answer.build_answer_event` grades, so `unsupported_claims` and `answer_confidence` were
computed over "Let me look that up", which nothing grounds.
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
    """A preamble the model wrote *before* it had any tool output is not part of the answer.

    Narrating before a tool call is ordinary behaviour for every model this ships against, so the
    concatenation was the common case rather than an edge: the chemist read a run-on sentence, the
    transcript held the preamble twice, and the verifier graded a sentence with nothing behind it.
    """
    ledger = _collect(
        [
            TokenEvent(text="Let me look that up."),
            ToolCallEvent(tool="find_notes", arguments="{}"),
            ToolResultEvent(tool="find_notes", result="matches=[…]"),
            TokenEvent(text="THE FINAL ANSWER IS 42."),
        ]
    )
    assert ledger.answer_text == "THE FINAL ANSWER IS 42."


def test_prose_the_turn_never_interrupted_is_the_answer_whole() -> None:
    """The ordinary turn is untouched: with no tool call, every token is still the answer."""
    ledger = _collect([TokenEvent(text="Two notes cover "), TokenEvent(text="this coupling.")])
    assert ledger.answer_text == "Two notes cover this coupling."


def test_a_helper_s_tool_call_does_not_discard_the_supervisor_s_prose() -> None:
    """Only the supervisor's own calls end the supervisor's paragraph.

    A helper runs its tools *inside* the `task` call the supervisor is waiting on, so its calls
    arrive attributed (`agent="subagent"`) and interleaved with the supervisor's own prose. Cutting
    on those would delete the answer of every turn that delegated. Same filter as the tokens, for
    the same reason `_stream_into` gives.
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
    """Discarding the preamble from the *answer* must not move what the chemist experienced.

    `ttft_seconds` is the latency question and it is answered by the first token of the turn, which
    is exactly the token this cut removes from the answer — so the two readers have to disagree
    about that string on purpose.
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
