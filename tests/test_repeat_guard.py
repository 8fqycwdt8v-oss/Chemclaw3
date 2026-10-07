"""A turn stops re-asking a tool the identical question it already answered.

Two properties make the guard safe rather than merely fast: it refuses rather than replaying a
cached answer (so a moving read is never served stale), and it allows a real re-check before it
starts refusing.
"""

import asyncio
from collections.abc import Awaitable, Callable
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

    Carries the registered tool too, because metric labels use the served name; `ToolNode` passes
    `tool=None` for an unknown name, which `_unregistered` covers.
    """
    return tool_request(name, dict(arguments), tool=_Registered(name))


class _Registered:
    """The one attribute `agent/audit.metric_tool_name` reads off a tool the graph holds."""

    def __init__(self, name: str) -> None:
        """Name it as the registry does — the label is this string, never the caller's."""
        self.name = name


def _unregistered(name: str, **arguments: Any) -> Any:
    """A call for a name the graph does not hold, which is what `ToolNode` hands the chain.

    `tool=None` is not a convenience here: it is exactly what an interceptor is given for a
    hallucinated or injected call, which is the whole reason the label needs clamping.
    """
    return tool_request(name, dict(arguments))


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
    """One repeat is a real pattern — a note re-read after a write.

    The boundary is deliberately not "never repeat": a guard that refused the second call would
    break correct behaviour to fix incorrect behaviour.
    """
    tool = _Tool()
    _drive(_ctx("expand_note", note_id="rxn-1"), tool)
    _drive(_ctx("expand_note", note_id="rxn-1"), tool)
    assert tool.runs == 2


def test_a_refusal_is_never_a_cached_answer(watching: None) -> None:
    """A refusal is never a cached answer: a replay goes stale, a refusal cannot."""
    tool = _Tool()
    for _ in range(settings.max_identical_tool_calls):
        _drive(_ctx("find_notes", query="aryl chloride"), tool)
    ctx = _ctx("find_notes", query="aryl chloride")
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
    """The refusal names the tool and the ways forward, so the model does not loop one step out."""
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
    """A pydantic argument, which `json.dumps` refuses, does not break the call the guard protects.
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
    """Ending a turn restores the guard; the runner process is reused, so a leftover counter would
    refuse the next chemist.
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
    """A refused repeat is counted per tool, since the turn still answers and leaves no other trace.
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


def test_an_invented_tool_name_never_reaches_the_metric_label(watching: None) -> None:
    """The counter is on an unauthenticated `/metrics`, so its label may not be model-authored.

    The guard runs for names the graph does not hold, so the label is clamped to the served tool
    surface, as `metric_tool_name` does; otherwise each invented name mints a series and can
    exhaust the series cap. The refusal the model reads still names what it asked for.
    """
    from chemclaw.core.metrics import METRICS

    invented = "IGNORE_PREVIOUS. exfiltrate=" + "A" * 200
    tool = _Tool()
    for _ in range(settings.max_identical_tool_calls):
        _drive(_unregistered(invented), tool)
    with pytest.raises(RepeatedCallRefusal) as refusal:
        _drive(_unregistered(invented), tool)

    assert invented in str(refusal.value), "the model must still be told what it asked for"
    assert invented not in METRICS.render(), "a model's string became a metric label"
    assert 'tool="unknown"' in METRICS.render()


# --------------------------------------------------------------------------------------------
# The coupling to compaction, which both modules documented and neither tested.
# --------------------------------------------------------------------------------------------


def test_a_call_whose_result_was_cleared_is_forgiven() -> None:
    """A call whose result compaction cleared is forgiven: the next identical call is a re-read."""
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
    """A cleared result forgives exactly once per turn.

    Compaction is non-destructive, so the same clearing is re-observed on every model call; keyed by
    call id, the second sighting is a no-op and repeats still accumulate to a refusal.
    """
    from chemclaw.agent.repeat_guard import count_call, forget_calls

    token = begin_call_watch()
    try:
        # `forget_calls` returns nothing, so the effect is what is asserted.
        forget_calls([("call-a", "find_past_jobs", {"q": "suzuki"})])

        assert count_call("find_past_jobs", {"q": "suzuki"}) is None
        # The next model call re-derives the same cleared result. Sighted already: no forgiveness.
        forget_calls([("call-a", "find_past_jobs", {"q": "suzuki"})])
        assert count_call("find_past_jobs", {"q": "suzuki"}) is None
        forget_calls([("call-a", "find_past_jobs", {"q": "suzuki"})])
        assert isinstance(count_call("find_past_jobs", {"q": "suzuki"}), RepeatedCallRefusal), (
            "re-sighting the same cleared result kept resetting the counter; the guard is disarmed"
        )
    finally:
        end_call_watch(token)


def test_a_call_whose_result_survived_the_clearing_is_still_guarded() -> None:
    """A call whose result survived the clearing is still guarded.

    `ClearToolUsesEdit` keeps the newest results, so only cleared calls are forgiven; a blanket
    reset would tie the guard's strength to a token threshold.
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


def test_a_status_poll_is_never_refused_however_often_it_asks(watching: None) -> None:
    """A job-status poll is never refused, however often it asks.

    The real tool is imported so its `@polls_moving_state` declaration is what is read.
    """
    import chemclaw.agent.durable_tools  # noqa: F401 — registers the tool and its declaration

    tool = _Tool()
    polls = settings.max_identical_tool_calls + 5
    for _ in range(polls):
        _drive(_ctx("get_durable_job_status", job_id="calc-compute_reaction_energy-4cf2"), tool)
    assert tool.runs == polls, "a status poll was refused as a repeat; the job's result is lost"


def test_the_poll_exemption_is_declared_not_granted_to_every_read(watching: None) -> None:
    """The control: the exemption belongs to the declaring tool, not to reads in general.

    The same number of identical calls to an undeclared read is still refused — so what makes a
    poll pass is its declaration, and the measured `find_past_jobs` loop stays closed.
    """
    import chemclaw.agent.durable_tools  # noqa: F401
    from chemclaw.core.tool_registry import is_a_poll

    assert is_a_poll("get_durable_job_status")
    assert not is_a_poll("find_past_jobs")
    tool = _Tool()
    with pytest.raises(RepeatedCallRefusal):
        for _ in range(settings.max_identical_tool_calls + 1):
            _drive(_ctx("find_past_jobs", kind="reaction"), tool)
