"""A turn stops re-asking a tool the identical question it already answered.

Measured live (2026-08-04): `find_past_jobs` called 7-8 times in a single turn across three probes,
`load_skill` x6, `find_notes` x5 — same tool, same arguments, same answer. Nothing failed, which is
why nothing caught it; the cost was a median turn of 128-142 s against 16.9 s on the archived run,
plus every repeat's result spent back into the context the answer had to be built from.

The two properties that make the guard safe rather than merely fast are pinned here: it *refuses*
rather than serving a cached answer (so a legitimately-changing read like a job status is never
pinned stale), and it allows a real re-check before it starts refusing.
"""

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from chemclaw.agent.repeat_guard import (
    RepeatedCallRefusal,
    begin_call_watch,
    end_call_watch,
    refuse_repeated_calls,
)
from chemclaw.core.config import settings
from tests.middleware import run_middleware, tool_request


def _ctx(name: str, **arguments: Any) -> Any:
    """The call as the guard reads it: a name and its arguments, which together are its key.

    Carries the *registered* tool object, which is what `ToolNode` passes for a name the graph
    holds and what `metric_tool_name` resolves the metric label against — the measured loop was
    7-8 `find_past_jobs` calls, a real tool. The name the graph does *not* hold is the model's own
    string and must never reach a label; `tests/test_tool_label_bound.py` drives that case for
    every `tool`-labelled metric at once, so it is not restated here.
    """
    return tool_request(name, dict(arguments), tool=SimpleNamespace(name=name, metadata={}))


class _Tool:
    """A tool body that records how often it actually ran."""

    def __init__(self) -> None:
        self.runs = 0

    async def __call__(self) -> None:
        self.runs += 1


def _drive(ctx: Any, call_next: Callable[[], Awaitable[Any]]) -> None:
    """Run the guard over one call to completion."""

    async def _handler(_request: Any) -> Any:
        return await call_next()

    asyncio.run(run_middleware(refuse_repeated_calls, ctx, _handler))


@pytest.fixture
def watching() -> Any:
    """A turn's call counter, torn down as the runner tears it down."""
    token = begin_call_watch()
    yield
    end_call_watch(token)


def test_the_measured_loop_is_stopped_and_the_tool_stops_running(watching: None) -> None:
    """Seven identical `find_past_jobs` calls become two, and the fifth never reaches the tool."""
    tool = _Tool()
    for _ in range(settings.max_identical_tool_calls):
        _drive(_ctx("find_past_jobs", kind="reaction"), tool)
    for _ in range(5):
        with pytest.raises(RepeatedCallRefusal):
            _drive(_ctx("find_past_jobs", kind="reaction"), tool)
    assert tool.runs == settings.max_identical_tool_calls, (
        "the tool must not run again once the turn is repeating itself"
    )


def test_a_single_re_check_still_goes_through(watching: None) -> None:
    """One repeat is a real pattern — a job polled after a wait, a note re-read after a write.

    The boundary is deliberately not "never repeat": a guard that refused the second call would
    break correct behaviour to fix incorrect behaviour.
    """
    tool = _Tool()
    _drive(_ctx("get_durable_job_status", job_id="calc-1"), tool)
    _drive(_ctx("get_durable_job_status", job_id="calc-1"), tool)
    assert tool.runs == 2


def test_a_refusal_is_never_a_cached_answer(watching: None) -> None:
    """The reason this refuses instead of replaying the first result.

    `get_durable_job_status` is read-only and legitimately changes *within* a turn, so serving the
    first call's answer would pin a job at "running" for a model that was correctly re-checking.
    A refusal cannot go stale: it reports what happened and hands the decision back.
    """
    tool = _Tool()
    for _ in range(settings.max_identical_tool_calls):
        _drive(_ctx("get_durable_job_status", job_id="calc-1"), tool)
    ctx = _ctx("get_durable_job_status", job_id="calc-1")
    with pytest.raises(RepeatedCallRefusal):
        _drive(ctx, tool)
    assert getattr(ctx, "result", None) is None, "no answer is invented for a call that never ran"


def test_different_arguments_are_a_different_question(watching: None) -> None:
    """The guard keys on the call, not the tool — narrowing a query is the fix it asks for."""
    tool = _Tool()
    for index in range(10):
        _drive(_ctx("find_notes", query=f"query-{index}"), tool)
    assert tool.runs == 10


def test_the_same_arguments_in_a_different_order_are_the_same_question(watching: None) -> None:
    """A model re-emitting one call is under no obligation to serialize its arguments the same way.

    Without canonicalization the guard would be trivially defeated by key order, which is not a
    difference any tool can observe.
    """
    tool = _Tool()
    for _ in range(settings.max_identical_tool_calls):
        _drive(_ctx("find_notes", query="q", kind="reaction"), tool)
    with pytest.raises(RepeatedCallRefusal):
        _drive(_ctx("find_notes", kind="reaction", query="q"), tool)


def test_the_refusal_tells_the_model_what_to_do_instead(watching: None) -> None:
    """A refusal the model cannot act on would just move the loop one step out.

    It names the tool (so the model knows which call was stopped) and states the three ways
    forward, including answering from what it has and saying so if that is not enough — the
    alternative being a turn that reaches the loop cap and answers nothing (`empty_answer`).
    """
    tool = _Tool()
    for _ in range(settings.max_identical_tool_calls):
        _drive(_ctx("find_past_jobs"), tool)
    with pytest.raises(RepeatedCallRefusal) as raised:
        _drive(_ctx("find_past_jobs"), tool)
    message = str(raised.value)
    assert "find_past_jobs" in message
    assert "change the arguments" in message
    assert "not enough" in message


def test_a_pydantic_argument_does_not_break_the_call_it_guards(watching: None) -> None:
    """A guard that can fail the call it is guarding is worse than the repetition it prevents.

    Half this system's tools take a pydantic model rather than a JSON object —
    `start_optimization_campaign(spec: CampaignSpec)` is the shape every generated connector job
    tool has — and `json.dumps` refuses one outright. A middleware that raised on that argument
    shape would break the calls it exists to protect.
    """

    class _Spec(BaseModel):
        query: str

    tool = _Tool()
    for _ in range(settings.max_identical_tool_calls):
        _drive(_ctx("start_optimization_campaign", spec=_Spec(query="q")), tool)
    assert tool.runs == settings.max_identical_tool_calls
    # And two equal-but-distinct models are one question, which a guard keyed on object identity
    # would miss entirely.
    with pytest.raises(RepeatedCallRefusal):
        _drive(_ctx("start_optimization_campaign", spec=_Spec(query="q")), tool)


def test_the_guard_is_a_no_op_off_the_request_path() -> None:
    """No counter, no limit. The CLI, the tests and the classic agent must be untouched.

    Deliberately outside the `watching` fixture: this is the state every non-request caller is in.
    """
    tool = _Tool()
    for _ in range(10):
        _drive(_ctx("find_past_jobs"), tool)
    assert tool.runs == 10


def test_ending_a_turn_puts_the_guard_back_to_where_it_found_it() -> None:
    """Teardown must restore, not merely stop counting — the runner reuses this process forever.

    A watch that left its counter behind would make the *second* chemist to ask a question in a
    worker's lifetime the one who gets refused, which is the worst possible failure for a guard
    whose whole purpose is to be invisible when the turn is behaving.
    """
    tool = _Tool()
    token = begin_call_watch()
    for _ in range(settings.max_identical_tool_calls):
        _drive(_ctx("find_past_jobs"), tool)
    end_call_watch(token)
    # Off the request path again: the guard must be a no-op, which it can only be if the reset
    # restored the *absence* of a counter rather than an empty one.
    for _ in range(10):
        _drive(_ctx("find_past_jobs"), tool)
    assert tool.runs == settings.max_identical_tool_calls + 10


def test_a_refused_repeat_is_counted_so_a_deployment_can_alert_on_it(watching: None) -> None:
    """The refusal itself is invisible — the turn still answers — so the counter is the only trace.

    The live run that found this had no signal at all beyond a median turn three times slower than
    the archived comparison, which is exactly the kind of thing nobody notices until they go
    looking. Labelled by tool, because "which call is the model looping on" is the first question.
    """
    from chemclaw.core.metrics import METRICS

    tool = _Tool()
    before = METRICS.value("chemclaw_repeated_tool_calls_total")
    for _ in range(settings.max_identical_tool_calls):
        _drive(_ctx("find_past_jobs"), tool)
    assert METRICS.value("chemclaw_repeated_tool_calls_total") == before, "no repeat, no sample"
    with pytest.raises(RepeatedCallRefusal):
        _drive(_ctx("find_past_jobs"), tool)
    assert METRICS.value("chemclaw_repeated_tool_calls_total") == before + 1
    assert 'tool="find_past_jobs"' in METRICS.render()


# --------------------------------------------------------------------------------------------
# The coupling to compaction, which both modules documented and neither tested.
# --------------------------------------------------------------------------------------------


def test_a_call_whose_result_was_cleared_is_forgiven() -> None:
    """A cleared answer makes the next identical call a re-read rather than a repeat.

    The premise the guard rests on is "the model already has the first answer", and compaction
    takes that away — the whole reason `forget_calls` exists. Until now nothing exercised it from
    either side.
    """
    from chemclaw.agent.repeat_guard import count_call, forget_calls

    token = begin_call_watch()
    try:
        assert count_call("find_past_jobs", {"q": "suzuki"}) is None
        assert count_call("find_past_jobs", {"q": "suzuki"}) is None
        # Third identical call, with the answers still in context: refused.
        assert isinstance(count_call("find_past_jobs", {"q": "suzuki"}), RepeatedCallRefusal)

        forget_calls([("call-a", "find_past_jobs", {"q": "suzuki"})])

        # The answers are gone, so asking again is a re-read rather than a repeat.
        assert count_call("find_past_jobs", {"q": "suzuki"}) is None
    finally:
        end_call_watch(token)


def test_a_cleared_result_forgives_exactly_once_per_turn() -> None:
    """The same cleared result, re-sighted on every model call, must not keep resetting the guard.

    The compaction edits are non-destructive, so the observer re-derives the *same* standing
    reduction on every model call of the turn — and forgiving it each time popped the counter as
    fast as repeats accumulated. Measured shape: past the 30k clearing trigger, the guard that was
    built to stop a 7-8-identical-call loop never fired again for the rest of the turn. The call
    id is what identifies "this exact cleared result", so the second sighting is a no-op and the
    repeats accumulate to a refusal exactly as they would in an uncompacted turn.
    """
    from chemclaw.agent.repeat_guard import count_call, forget_calls

    token = begin_call_watch()
    try:
        assert forget_calls([("call-a", "find_past_jobs", {"q": "suzuki"})]) == 1

        assert count_call("find_past_jobs", {"q": "suzuki"}) is None
        # The next model call re-derives the same cleared result. Sighted already: no forgiveness.
        assert forget_calls([("call-a", "find_past_jobs", {"q": "suzuki"})]) == 0
        assert count_call("find_past_jobs", {"q": "suzuki"}) is None
        assert forget_calls([("call-a", "find_past_jobs", {"q": "suzuki"})]) == 0
        assert isinstance(count_call("find_past_jobs", {"q": "suzuki"}), RepeatedCallRefusal), (
            "re-sighting the same cleared result kept resetting the counter; the guard is disarmed"
        )
    finally:
        end_call_watch(token)


def test_a_call_whose_result_survived_the_clearing_is_still_guarded() -> None:
    """The precision that a blanket reset did not have, and the reason it was worth adding.

    `ClearToolUsesEdit` preserves the newest `agent_keep_last_tool_groups` results, so after a
    reduction the model is still holding some of its answers. `forget_calls()` used to wipe every
    counter, which forgave those too — once per reduction, and a long turn reduces on many model
    calls. The guard's strength was therefore a function of `agent_tool_result_clear_trigger`,
    which is a token threshold and has nothing to do with whether a repeat is useful.
    """
    from chemclaw.agent.repeat_guard import count_call, forget_calls

    token = begin_call_watch()
    try:
        for _ in range(3):
            count_call("find_notes", {"q": "cleared"})
            count_call("get_durable_job_status", {"id": "kept"})

        # Only the first tool's results were replaced by a placeholder.
        forget_calls([("call-b", "find_notes", {"q": "cleared"})])

        assert count_call("find_notes", {"q": "cleared"}) is None
        assert isinstance(count_call("get_durable_job_status", {"id": "kept"}), RepeatedCallRefusal)
    finally:
        end_call_watch(token)


def test_forgetting_is_a_no_op_off_the_request_path() -> None:
    """Like every other function in the module — the CLI and the classic agent take this branch."""
    from chemclaw.agent.repeat_guard import forget_calls

    forget_calls([("call-c", "find_notes", {"q": "anything"})])
