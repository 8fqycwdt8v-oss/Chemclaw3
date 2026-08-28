"""One compaction counter answered neither compaction question, and a failing edit killed the turn.

The decision is `D-2026-08-27-a-refusal-is-not-a-crash`. `chemclaw_context_compactions_total` is
unlabelled, and the middleware composes two edits with opposite consequences: `ClearToolUsesEdit` is
lossless (the `tool_use` record survives and the model can re-fetch) while
`KeepLastConversationGroupsEdit` is destructive (conversation turns are deleted from what the model
sees). "The agent forgot what I told it three turns ago" and "the agent re-ran a tool it already
ran" *are* those two edits.

And there was no `try` in `agent/compaction.py` at all, so a raising edit ended the turn as a
generic internal error — losing the answer, the tokens already spent and every tool the turn had
run, in order to save tokens.
"""

import logging
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.context_editing import ContextEdit
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from chemclaw.agent.compaction import (
    GuardedEdit,
    RecordContextCompaction,
    context_compaction_middleware,
)
from chemclaw.core.metrics import METRICS


def _thread(groups: int) -> list[AnyMessage]:
    """`groups` conversation groups, each a human turn answered through one tool call."""
    messages: list[AnyMessage] = []
    for index in range(groups):
        call_id = f"call-{index}"
        messages += [
            HumanMessage(content=f"question {index} " + "x" * 200),
            AIMessage(
                content="",
                tool_calls=[{"name": "predict_pka", "args": {"i": index}, "id": call_id}],
            ),
            ToolMessage(
                content="the pKa is 4.2 " + "y" * 400,
                tool_call_id=call_id,
                response_metadata={"context_editing": {"cleared": True}},
            ),
            AIMessage(content=f"answer {index}"),
        ]
    return messages


def _request(state: list[AnyMessage], sent: list[AnyMessage]) -> ModelRequest[Any]:
    """A request whose state holds the whole thread and whose messages are the reduced list."""
    return ModelRequest(
        model=None,  # type: ignore[arg-type]
        system_prompt=None,
        messages=sent,
        tool_choice=None,
        tools=[],
        response_format=None,
        state={"messages": state},
        runtime=None,
    )


class _RaisingEdit(ContextEdit):
    """A context edit that fails the way an unfamiliar message shape would make one fail."""

    def apply(self, messages: list[AnyMessage], *, count_tokens: Any) -> None:
        """Raise, so the guard around it is the thing under test."""
        raise RuntimeError("this shape was not anticipated")


def test_the_record_names_the_tools_whose_results_were_cleared_and_the_groups_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The distinction one counter could not carry, as one structured record per turn.

    Counts and names only: which tool's answer the model lost is the actionable half, and the
    arguments and payloads `_cleared_calls` also holds are not — a log line is not a place to
    re-publish a chemist's question or a corpus excerpt.
    """
    thread = _thread(6)
    # The model is sent the last two groups only: the window dropped four, and the tool results
    # that survive are marked cleared by upstream's own metadata key.
    sent = thread[-8:]

    with caplog.at_level(logging.INFO):
        RecordContextCompaction().wrap_model_call(_request(thread, sent), lambda request: None)

    assert "context.compacted" in caplog.text
    assert "predict_pka" in caplog.text
    # Six groups in state, two sent — four conversation turns the model can no longer see.
    assert "dropped 4 conversation group(s)" in caplog.text
    assert "cleared 2 tool result(s)" in caplog.text
    # The content of what was cleared never appears.
    assert "y" * 400 not in caplog.text


def test_a_call_that_needed_no_reduction_says_nothing_and_counts_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A call needing no reduction stays distinguishable from compaction not being wired at all.

    That guard predates this change and is what the whole module exists to protect; the record
    added beside the counter must not weaken it.
    """
    before = METRICS.value("chemclaw_context_compactions_total")
    thread = _thread(2)

    with caplog.at_level(logging.INFO):
        RecordContextCompaction().wrap_model_call(_request(thread, thread), lambda request: None)

    assert "context.compacted" not in caplog.text
    assert METRICS.value("chemclaw_context_compactions_total") == before


def test_a_raising_edit_costs_the_reduction_rather_than_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Continuing uncompacted is the safe direction, and the messages are left as they were.

    A request over budget still has a chance of being answered — both triggers sit well below the
    provider's hard ceiling, and if it does fail the provider's context-length error is now
    classified and told to the chemist as such. A failed turn has no such chance.
    """
    before = METRICS.value("chemclaw_degraded_total")
    messages = _thread(3)
    unchanged = list(messages)

    with caplog.at_level(logging.ERROR):
        GuardedEdit(_RaisingEdit()).apply(messages, count_tokens=lambda _messages: 10)

    assert messages == unchanged
    assert METRICS.value("chemclaw_degraded_total") == before + 1
    assert 'chemclaw_degraded_total{subsystem="compaction"}' in METRICS.render()
    assert "_RaisingEdit" in caplog.text


def test_a_raising_observer_costs_only_the_observation(caplog: pytest.LogCaptureFixture) -> None:
    """The observer is guarded separately, because it reads shapes this module does not own.

    A raising *observer* ending a turn would be the worst trade available: removing it entirely
    changes nothing a chemist receives.
    """
    ran = False

    def _handler(_request: ModelRequest[Any]) -> str:
        nonlocal ran
        ran = True
        return "the model answered"

    # `state` is not a mapping, so reading the thread off it raises inside the observer.
    broken = _request(_thread(2), _thread(2))
    object.__setattr__(broken, "state", "not a mapping")

    with caplog.at_level(logging.ERROR):
        answer = RecordContextCompaction().wrap_model_call(broken, _handler)

    assert ran and answer == "the model answered"
    assert "degraded[compaction]" in caplog.text


def test_both_edits_are_guarded_including_the_one_that_wraps_upstream() -> None:
    """Both edits construct upstream code with this repository's arguments, so both are guarded.

    `ClearOlderToolResultsEdit` builds a `ClearToolUsesEdit` per apply — with the batch-aware
    `keep` and the overshoot as `clear_at_least` — so upstream's strategy still runs and is still
    exactly as capable of raising on an unexpected message shape as the first-party window is.
    Guarding only what we wrote would leave the larger of the two able to end a turn.
    """
    editing = context_compaction_middleware()[1]
    assert [type(edit).__name__ for edit in editing.edits] == ["GuardedEdit", "GuardedEdit"]
    assert [type(edit.edit).__name__ for edit in editing.edits] == [
        "ClearOlderToolResultsEdit",
        "KeepLastConversationGroupsEdit",
    ]
