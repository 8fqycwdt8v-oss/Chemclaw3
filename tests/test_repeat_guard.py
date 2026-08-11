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
from typing import Any

import pytest
from pydantic import BaseModel

from chemclaw.agent.repeat_guard import (
    RepeatedCallRefusal,
    begin_call_watch,
    end_call_watch,
    lg_refuse_repeated_calls,
)
from chemclaw.core.config import settings
from tests.middleware import run_middleware, tool_request


def _ctx(name: str, **arguments: Any) -> Any:
    """The call as the guard reads it: a name and its arguments, which together are its key."""
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

    asyncio.run(run_middleware(lg_refuse_repeated_calls, ctx, _handler))


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
