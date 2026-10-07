"""Compaction reports which edit fired, and a failing edit costs the reduction, not the turn.

Decision: `D-2026-08-27-a-refusal-is-not-a-crash`. `ClearToolUsesEdit` is lossless (the model can
re-fetch) while `KeepLastConversationGroupsEdit` deletes turns from view, so the record names
both. Each edit is guarded so a raising edit leaves the request uncompacted instead of failing.
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.context_editing import ContextEdit
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from chemclaw.agent.compaction import (
    _REPORTED,
    GuardedEdit,
    RecordContextCompaction,
    context_compaction_middleware,
)
from chemclaw.agent.context_budget import begin_context_watch, end_context_watch
from chemclaw.core.metrics import METRICS


@pytest.fixture(autouse=True)
def _fresh_degradation_latch() -> Iterator[None]:
    """Start every case with nothing yet reported loudly.

    The guards log the first failure of each kind per process at ERROR and the rest at DEBUG
    (`compaction._degrade_once`), so the latch must be reset for each case to be order-independent.
    """
    _REPORTED.clear()
    yield
    _REPORTED.clear()


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

    Counts and tool names only — never arguments or payloads, which would republish a chemist's
    question or corpus text into logs.
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

    An over-budget request may still be answered (triggers sit below the provider ceiling, and a
    context-length error is classified for the chemist); a failed turn cannot.
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

    `ClearOlderToolResultsEdit` builds a `ClearToolUsesEdit` per apply, which can raise on an
    unexpected message shape as readily as the first-party window.
    """
    editing = context_compaction_middleware()[1]
    assert [type(edit).__name__ for edit in editing.edits] == ["GuardedEdit", "GuardedEdit"]
    assert [type(edit.edit).__name__ for edit in editing.edits] == [
        "ClearOlderToolResultsEdit",
        "KeepLastConversationGroupsEdit",
    ]


def test_a_standing_degradation_is_loud_once_and_counted_every_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A standing degradation is loud once and counted every time.

    Both guards run inside `wrap_model_call`, so a real failure recurs on every model call; one
    ERROR per call would flood. The count is not latched, because `chemclaw_degraded_total` is a
    rate: one loud line, two increments.
    """
    before = METRICS.value("chemclaw_degraded_total")
    edit = GuardedEdit(_RaisingEdit())

    with caplog.at_level(logging.DEBUG):
        for _call in range(2):
            edit.apply(_thread(3), count_tokens=lambda _messages: 10)

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    debugs = [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG and "_RaisingEdit" in record.getMessage()
    ]
    assert len(errors) == 1, "the same standing failure was reported at ERROR more than once"
    assert errors[0].exc_info is not None, "the one loud line is the one that carries the traceback"
    assert len(debugs) == 1 and not debugs[0].exc_info, (
        "the quiet line must not carry a traceback either — the traceback is the expensive half"
    )
    assert METRICS.value("chemclaw_degraded_total") == before + 2, "the counter must not latch"


def test_the_observer_latches_separately_from_the_edits() -> None:
    """A failing observer must not silence a failing edit, or the pod reports whichever came first.

    They are different faults with different remedies — one loses the reduction, the other loses
    only the measurement — so the latch is keyed per kind rather than per module.
    """

    def _handler(_request: ModelRequest[Any]) -> str:
        return "answered"

    broken = _request(_thread(2), _thread(2))
    object.__setattr__(broken, "state", "not a mapping")
    RecordContextCompaction().wrap_model_call(broken, _handler)
    GuardedEdit(_RaisingEdit()).apply(_thread(2), count_tokens=lambda _messages: 10)

    assert _REPORTED == {"reduction", "_RaisingEdit"}


def test_the_module_does_not_claim_a_guard_over_upstreams_own_copy_and_count() -> None:
    """`ContextEditingMiddleware.wrap_model_call` deep-copies and counts *outside* any `apply`.

    `GuardedEdit` wraps `ContextEdit.apply` only, so upstream's own copy and token count are not
    guarded; asserted against the installed source so the module cannot claim otherwise.
    """
    import inspect

    from langchain.agents.middleware.context_editing import ContextEditingMiddleware

    source = inspect.getsource(ContextEditingMiddleware.awrap_model_call)
    copied = source.index("deepcopy(list(request.messages))")
    applied = source.index("edit.apply(")
    assert copied < applied, (
        "upstream now copies inside the loop; the narrowing in this module's docstring should be "
        "re-derived against the new shape"
    )


def test_a_later_call_that_drops_a_conversation_group_is_announced_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The record follows the high-water reduction, so a later destructive drop is announced too.

    Tool-result clearing usually fires first, so announcing only a turn's first reduction would hide
    a later conversation-group drop. A third call that re-derives the same standing reduction stays
    silent, since the edits are non-destructive and re-applied on every call.
    """
    thread = _thread(6)
    # The lossless edit alone: every tool result replaced, every conversation group still sent.
    cleared = [
        ToolMessage(content="cleared", tool_call_id=message.tool_call_id)
        if isinstance(message, ToolMessage)
        else message
        for message in thread
    ]
    # The destructive edit on top of it: the window keeps the last two groups.
    dropped = cleared[-8:]

    token = begin_context_watch()
    try:
        with caplog.at_level(logging.INFO):
            record = RecordContextCompaction()
            record.wrap_model_call(_request(thread, cleared), lambda request: None)
            first = [line for line in caplog.messages if "reclaimed ~" in line]
            record.wrap_model_call(_request(thread, dropped), lambda request: None)
            record.wrap_model_call(_request(thread, dropped), lambda request: None)
    finally:
        end_context_watch(token)

    announced = [line for line in caplog.messages if "reclaimed ~" in line]
    assert len(first) == 1, first
    assert "dropped 0 conversation group(s)" in first[0]
    assert len(announced) == 2, (
        f"the destructive edit was never announced (or was announced per call): {announced}"
    )
    assert "dropped 4 conversation group(s)" in announced[1]
