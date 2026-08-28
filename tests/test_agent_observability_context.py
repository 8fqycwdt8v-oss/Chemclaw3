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
from chemclaw.core.metrics import METRICS


@pytest.fixture(autouse=True)
def _fresh_degradation_latch() -> Iterator[None]:
    """Start every case with nothing yet reported loudly.

    The guards below report the *first* failure of each kind in a process at ERROR and the rest at
    DEBUG — see `compaction._degrade_once` — so without this the level a case observes would depend
    on which case ran before it, which is a test that passes for a reason unrelated to its subject.
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


def test_both_edits_are_guarded_including_the_one_upstream_owns() -> None:
    """`ClearToolUsesEdit` is called with this repository's placeholder, triggers and `keep`.

    So it is exactly as capable of raising on an unexpected message shape as the first-party one,
    and guarding only what we wrote would leave the larger of the two edits able to end a turn.
    """
    editing = context_compaction_middleware()[0]
    assert [type(edit).__name__ for edit in editing.edits] == ["GuardedEdit", "GuardedEdit"]
    assert [type(edit.edit).__name__ for edit in editing.edits] == [
        "ClearToolUsesEdit",
        "KeepLastConversationGroupsEdit",
    ]


def test_a_standing_degradation_is_loud_once_and_counted_every_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One ERROR-with-traceback per model call, per turn, per pod is not a report — it is a flood.

    Both guards here are inside `wrap_model_call`, so the realistic failure — an upstream shape
    change, not a one-off — recurs on every model call the fleet makes until someone ships a fix.
    A 30-step turn wrote 30 identical tracebacks at ERROR, which is the level an operator pages on.
    `KeepLastConversationGroupsEdit` demotes its own per-call line to DEBUG on exactly this
    argument, and `agent/langgraph_agent.py::_log_narrowing` makes it again.

    **The count is deliberately not latched.** `chemclaw_degraded_total` is a rate, and a rate that
    reported once per process would understate the degradation exactly as the run got worse — the
    failure `metrics_bridge.degraded` exists to correct. So the assertion is asymmetric: one loud
    line, two increments.
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

    "Nothing here may end a turn" was the claim; `GuardedEdit` wraps `ContextEdit.apply` only, and
    upstream's own `deepcopy(list(request.messages))` and `count_tokens` closure run before the
    first `apply` is reached. Asserted against the installed source rather than restated in prose,
    because the whole defect was a docstring that outlived what it described.
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
