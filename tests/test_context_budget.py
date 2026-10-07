"""The budget's unit, its ceiling, and the request prefix it charges.

1. The unit: chars/4 is close on prose and schemas and about half the truth on structured tool
   results, so the billed/estimated ratio is learned from the provider's `input_tokens` in
   `note_model_call`.
2. The ceiling: a declared context window bounds the budget.
3. The prefix: charged unconditionally, so `agent_context_token_budget` bounds the whole request.
   A `ContextEdit` cannot see the prefix; a middleware can, and a contextvar is the seam.
"""

import asyncio
import itertools
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

from chemclaw.agent.context_budget import (
    _MAX_REPORTED_FLOORS,
    _SCHEMA_TOKENS,
    MeasureRequestPrefix,
    _baked_cache_dir,
    _Calibration,
    _encoding,
    _message_tokens,
    _prefix,
    begin_context_watch,
    current_context,
    effective_trigger,
    end_context_watch,
    estimate_tool_schemas,
    estimator_ratio,
    note_model_call,
    prefix_tokens,
    reset_calibration,
    reset_encoding,
    reset_floor_reports,
)
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS


@pytest.fixture(autouse=True)
def _clean_calibration() -> Any:
    """No test inherits another's traffic — the ratio is process-wide by design."""
    reset_calibration()
    yield
    reset_calibration()


def _observe(ratio: float, calls: int = 40) -> None:
    """Feed `calls` model calls that were all billed `ratio` times their estimate."""
    for _ in range(calls):
        note_model_call(10_000, int(10_000 * ratio))


def test_a_process_with_no_sample_yet_changes_nothing() -> None:
    """Before anything has been observed the trigger is exactly the configured number.

    A deployment that observes nothing keeps its behaviour; this is a claim about zero samples.
    """
    assert estimator_ratio() == 1.0
    assert effective_trigger(100_000) == 100_000


def test_the_first_sample_is_believed_and_is_the_sample() -> None:
    """One observation calibrates the budget, to exactly what was observed.

    `agent_context_calibration_min_calls` is small and the EWMA's 1.0 seed is divided back out (bias
    correction), so the first calls do not budget at the loose, uncalibrated end. Asserted as an
    identity: after one sample the ratio is that sample.
    """
    note_model_call(10_000, 16_000)

    assert estimator_ratio() == pytest.approx(1.6, abs=1e-9), (
        "one observation of a request billed 1.6x its estimate did not reach the budget as 1.6 — "
        "either the sample floor is above 1 again, or the EWMA is still answering with its seed"
    )
    # 100,000 billed tokens converted at 1.6 — a band rather than 62,500 exactly, because the
    # reconstructed ratio carries a last-bit residue and `effective_trigger` truncates.
    assert 62_499 <= effective_trigger(100_000) <= 62_500

    # And it is still an average, not a latch: a second, cheaper observation moves it back toward
    # what has now been seen twice, weighted by `_ALPHA`.
    note_model_call(10_000, 10_000)

    assert 1.0 < estimator_ratio() < 1.6, (
        f"a second sample of 1.0 left the ratio at {estimator_ratio()}; bias correction must not "
        "turn the average into a high-water mark"
    )


def test_a_fresh_calibration_answers_with_its_first_sample() -> None:
    """A fresh calibration answers with its first sample.

    Constructed directly rather than via the module singleton, because the autouse fixture re-seeds
    the singleton through `reset()` and would hide a wrong seed in `__init__`.
    """
    fresh = _Calibration()

    assert fresh.ratio() == 1.0, "an uncalibrated process must change nothing"

    fresh.note(10_000, 25_000)

    assert fresh.ratio() == pytest.approx(2.5, abs=1e-9), (
        f"one sample of 2.5 came back as {fresh.ratio()}: the bias correction divides out a seed "
        "of 1.0, so a constructor that seeds anything else answers with the seed instead"
    )


def test_the_plausible_band_includes_its_own_endpoints() -> None:
    """`_SANE` is a closed interval: its endpoints are believed.

    The band drops measurement faults, not tokenizer differences. Driven downward from a calibrated
    ratio, because `ratio()` clamps at 1.0 and a low sample is visible only as pulling a high
    average down.
    """
    _observe(4.0)
    for _ in range(40):
        note_model_call(10_000, 2_000)  # sample 0.2 — the lower endpoint, and accepted

    assert estimator_ratio() == 1.0, (
        "a run of samples at the band's lower endpoint left the ratio high: the endpoint was "
        "dropped as a fault"
    )

    reset_calibration()
    _observe(4.0)
    for _ in range(40):
        note_model_call(10_000, 1_990)  # sample 0.199 — outside, and dropped

    assert estimator_ratio() == pytest.approx(4.0, abs=0.05), (
        "a sample below the band moved the ratio; the fault filter is not filtering"
    )

    reset_calibration()
    for _ in range(40):
        note_model_call(10_000, 80_000)  # sample 8.0 — the upper endpoint, and accepted

    assert estimator_ratio() == settings.agent_context_calibration_max_factor, (
        "the band's upper endpoint was dropped, so a genuinely expensive tokenizer is unlearnable"
    )

    reset_calibration()
    for _ in range(40):
        note_model_call(10_000, 80_010)  # sample 8.001 — outside, and dropped

    assert estimator_ratio() == 1.0, "a sample above the band moved the ratio"


def test_a_one_token_estimate_is_a_sample_rather_than_a_fault() -> None:
    """A one-token estimate is a sample rather than a fault.

    The guard rejects non-positive estimates only; `<= 1` would silently drop real degenerate calls.
    """
    for _ in range(40):
        note_model_call(1, 4)

    assert estimator_ratio() == pytest.approx(4.0, abs=0.05)


def test_a_measured_underestimate_tightens_the_trigger() -> None:
    """A measured underestimate tightens the trigger.

    At a 2.2x ratio (typical of connector JSON results) a 100,000-token budget is ~45,000 estimated
    tokens, the line the edits compare against.
    """
    _observe(2.2)

    assert estimator_ratio() == pytest.approx(2.2, abs=0.15)
    assert 40_000 < effective_trigger(100_000) < 50_000


def test_it_never_loosens_a_budget() -> None:
    """An overestimate leaves the trigger alone rather than raising it.

    Clamped at 1.0, the worst a mismeasurement can do is compact early; a ratio below 1 would let a
    thread outgrow what the deployment asked to spend.
    """
    _observe(0.5)

    assert estimator_ratio() == 1.0
    assert effective_trigger(100_000) == 100_000


def test_the_factor_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pathological ratio cannot collapse the budget to nothing."""
    monkeypatch.setattr(settings, "agent_context_calibration_max_factor", 4.0)
    _observe(7.9)

    assert estimator_ratio() == 4.0
    assert effective_trigger(100_000) == 25_000


def test_a_nonsense_sample_is_dropped() -> None:
    """A usage block for a different request teaches nothing rather than moving the budget."""
    for _ in range(40):
        note_model_call(10_000, 1)
        note_model_call(10_000, 0)
        note_model_call(0, 50_000)

    assert estimator_ratio() == 1.0


def test_calibration_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """One knob, because a feedback loop on every model call is a decision a site may decline."""
    _observe(2.2)
    monkeypatch.setattr(settings, "agent_context_calibration_enabled", False)

    assert estimator_ratio() == 1.0
    assert effective_trigger(100_000) == 100_000


def test_a_declared_window_bounds_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared window bounds the budget.

    Undeclared, the configured budget is the only bound. Run off the request path (prefix 0) to
    isolate the window arm; the prefix arm is
    `test_the_prefix_reaches_the_edits_with_no_window_declared`.
    """
    monkeypatch.setattr(settings, "llm_max_tokens", 4_096)
    monkeypatch.setattr(settings, "llm_context_window_tokens", 0)
    assert effective_trigger(100_000) == 100_000

    monkeypatch.setattr(settings, "llm_context_window_tokens", 128_000)
    # No request in flight, so the prefix is 0: the window still bounds, by the output reservation.
    assert effective_trigger(100_000) == 100_000, "a large window must not raise a smaller budget"

    monkeypatch.setattr(settings, "llm_context_window_tokens", 60_000)
    assert effective_trigger(100_000) == 60_000 - 4_096


def test_a_declared_window_subtracts_the_measured_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared window subtracts the measured prefix.

    Driven through the middleware, because the claim is that a request's prefix reaches the edits;
    setting the contextvar by hand would not test the seam.
    """
    monkeypatch.setattr(settings, "llm_max_tokens", 1_000)
    monkeypatch.setattr(settings, "llm_context_window_tokens", 50_000)
    measured: list[int] = []

    def handler(request: Any) -> str:
        measured.append(prefix_tokens())
        measured.append(effective_trigger(100_000))
        return "done"

    request = _request(system="you are a process chemist. " * 100)
    MeasureRequestPrefix().wrap_model_call(request, handler)

    prefix, trigger = measured
    assert prefix > 0, "the system message contributed nothing to the prefix"
    assert trigger == 50_000 - prefix - 1_000
    assert prefix_tokens() == 0, "the ambient outlived the call it describes"


def test_the_prefix_reaches_the_edits_with_no_window_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefix reaches the edits with no window declared.

    `llm_context_window_tokens` defaults to 0, so the prefix must be charged without a window: the
    trigger is the configured budget minus what this request costs before any conversation. Driven
    through the middleware, as above.
    """
    monkeypatch.setattr(settings, "llm_context_window_tokens", 0)
    measured: list[int] = []

    def handler(request: Any) -> str:
        measured.append(prefix_tokens())
        measured.append(effective_trigger(100_000))
        return "done"

    request = _request(system="you are a process chemist. " * 100)
    MeasureRequestPrefix().wrap_model_call(request, handler)

    prefix, trigger = measured
    assert prefix > 0, "the system message contributed nothing to the prefix"
    assert trigger == 100_000 - prefix, (
        f"the trigger is {trigger} against a 100,000 budget and a {prefix}-token prefix: the "
        "prefix was not charged, which is what an undeclared window used to mean"
    )


def test_a_budget_the_prefix_exhausts_floors_at_one_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A budget the prefix exhausts floors at one and says so.

    A trigger of 1 means reducing on every call. Raising is not an option inside a middleware, so
    the floor stands and is reported, once per distinct `(configured, prefix, window)` since the
    condition is static. Any budget below the prefix reaches it;
    `tests/test_compaction.py::test_the_shipped_clear_trigger_clears_the_prefix_it_is_charged`
    asserts the shipped defaults do not.
    """
    monkeypatch.setattr(settings, "llm_context_window_tokens", 0)
    reset_floor_reports()
    token = _prefix.set(50_000)
    try:
        with caplog.at_level(logging.WARNING, logger="chemclaw.agent.context_budget"):
            assert effective_trigger(30_000) == 1
            first = len(caplog.records)
            assert effective_trigger(30_000) == 1
            assert effective_trigger(50_000) == 1, "budget == prefix leaves nothing and must floor"
        # A budget above the prefix is a budget, and must not be reported as a floor.
        assert effective_trigger(60_000) == 10_000
    finally:
        _prefix.reset(token)

    assert first == 1, f"the floor was reported {first} times for one model call"
    assert len(caplog.records) == 2, (
        "the same floor was reported twice, or a second distinct one was not reported at all: "
        f"{[r.message for r in caplog.records]}"
    )
    said = caplog.records[0].message
    assert "30000" in said and "50000" in said, (
        f"the warning does not name the budget and the prefix an operator has to reconcile: {said}"
    )
    assert getattr(caplog.records[0], "event", None) == "context.trigger_floored"


def test_a_trigger_of_exactly_one_is_a_budget_and_not_a_floor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A trigger of exactly one is a budget and not a floor.

    Both return 1, so the WARNING is the only difference. One prefix, two budgets one token apart,
    so the boundary is driven and the line is asserted absent at exactly 1.
    """
    monkeypatch.setattr(settings, "llm_context_window_tokens", 0)
    reset_floor_reports()
    token = _prefix.set(50_000)
    try:
        with caplog.at_level(logging.WARNING, logger="chemclaw.agent.context_budget"):
            assert effective_trigger(50_001) == 1, "the smallest real budget is 1, not the floor"
            assert caplog.records == [], (
                "a budget leaving the thread one token was reported as floored: "
                f"{[r.message for r in caplog.records]}"
            )

            assert effective_trigger(50_000) == 1, "one token less leaves nothing and must floor"
            assert len(caplog.records) == 1, "the floor one token down was not reported"
    finally:
        _prefix.reset(token)

    said = caplog.records[0]
    assert getattr(said, "configured_tokens", None) == 50_000
    assert getattr(said, "prefix_tokens", None) == 50_000
    assert getattr(said, "window_tokens", None) == 0
    assert getattr(said, "estimator_ratio", None) == 1.0


def test_the_floor_report_stops_at_its_own_cap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The floor report stops at its own cap.

    The de-duplication key includes the prefix, which moves with the tool surface, so
    `_MAX_REPORTED_FLOORS` bounds the set.
    """
    monkeypatch.setattr(settings, "llm_context_window_tokens", 0)
    reset_floor_reports()
    token = _prefix.set(50_000)
    try:
        with caplog.at_level(logging.WARNING, logger="chemclaw.agent.context_budget"):
            for offset in range(_MAX_REPORTED_FLOORS):
                assert effective_trigger(1_000 + offset) == 1
            assert len(caplog.records) == _MAX_REPORTED_FLOORS, (
                "a distinct floor below the cap went unreported"
            )

            assert effective_trigger(1_000 + _MAX_REPORTED_FLOORS) == 1
            assert len(caplog.records) == _MAX_REPORTED_FLOORS, (
                "the cap is off by one: a key past it was still reported"
            )
    finally:
        _prefix.reset(token)
        reset_floor_reports()


def test_a_prefix_past_the_basis_is_paid_in_spend_and_said_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A prefix past the basis is paid in spend and said once.

    The budget is a prefix basis plus a thread allowance; a larger real prefix must not come out of
    the thread. At the basis nothing changes, past it the thread keeps what the basis left, and a
    declared window still charges the whole prefix.
    """
    monkeypatch.setattr(settings, "llm_context_window_tokens", 0)
    monkeypatch.setattr(settings, "agent_context_prefix_basis", 80_000)
    reset_floor_reports()
    try:
        with caplog.at_level(logging.WARNING, logger="chemclaw.agent.context_budget"):
            for prefix in (80_000, 110_000, 110_000):
                token = _prefix.set(prefix)
                try:
                    assert effective_trigger(120_000) == 40_000, (
                        f"a {prefix}-token prefix against an 80,000 basis left the thread "
                        f"{effective_trigger(120_000)}: what a deployment binds past the basis "
                        "must not come out of the thread"
                    )
                finally:
                    _prefix.reset(token)
            excess = [
                r
                for r in caplog.records
                if getattr(r, "event", None) == "context.prefix_over_basis"
            ]
            assert len(excess) == 1, (
                "a prefix at the basis was reported, or one past it was reported per call: "
                f"{[r.message for r in caplog.records]}"
            )
            assert getattr(excess[0], "excess_tokens", None) == 30_000

            # The window is the provider's limit, so no basis buys room in it.
            monkeypatch.setattr(settings, "llm_context_window_tokens", 128_000)
            token = _prefix.set(110_000)
            try:
                assert effective_trigger(120_000) == 128_000 - settings.llm_max_tokens - 110_000
            finally:
                _prefix.reset(token)
    finally:
        reset_floor_reports()


def test_the_ambient_prefix_is_put_back_after_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ambient prefix is put back after the call.

    Otherwise one turn's prefix budgets whatever runs next in that context. All three exits: return,
    raise, and a failed measurement that set nothing.
    """
    outer = _prefix.set(4_321)
    try:
        request = _request(system="you are a process chemist. " * 100)
        middleware = MeasureRequestPrefix()
        inside: list[int] = []

        def handler(_: Any) -> str:
            inside.append(prefix_tokens())
            return "done"

        middleware.wrap_model_call(request, handler)

        assert inside[0] != 4_321, "the fixture never published a prefix, so it asserts nothing"
        assert prefix_tokens() == 4_321, "the ambient prefix was not restored after a normal return"

        def raising(_: Any) -> str:
            raise RuntimeError("the model call failed")

        with pytest.raises(RuntimeError):
            middleware.wrap_model_call(request, raising)

        assert prefix_tokens() == 4_321, "the ambient prefix was not restored after a raise"

        assert asyncio.run(_ambient_after_an_async_call(middleware, request)) == 4_321, (
            "the ambient prefix was not restored on the async path — the one a turn takes"
        )

        monkeypatch.setattr(
            MeasureRequestPrefix, "_measure", lambda *_: (_ for _ in ()).throw(ValueError("nope"))
        )
        middleware.wrap_model_call(request, handler)

        assert inside[-1] == 4_321, (
            "an unmeasurable prefix replaced the ambient rather than left it"
        )
        assert prefix_tokens() == 4_321, (
            "an unmeasurable prefix disturbed the ambient on the way out"
        )
    finally:
        _prefix.reset(outer)


async def _ambient_after_an_async_call(middleware: Any, request: Any) -> int:
    """The ambient prefix after `awrap_model_call`, read in the context the call ran in.

    `asyncio.run` copies the context, so a leak would be invisible to an assertion made after it
    returns.
    """
    seeded = _prefix.set(4_321)
    try:
        inside: list[int] = []

        async def ahandler(_: Any) -> str:
            inside.append(prefix_tokens())
            return "done"

        await middleware.awrap_model_call(request, ahandler)
        assert inside[0] != 4_321, "the async fixture never published a prefix"
        return prefix_tokens()
    finally:
        _prefix.reset(seeded)


def _request(*, system: str) -> Any:
    """A minimal `ModelRequest` — enough for the middleware, with no graph to build."""
    from langchain.agents.middleware import ModelRequest

    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="x")])),
        messages=[HumanMessage(content="hello")],
        system_message=SystemMessage(content=system),
        tools=[],
        state={"messages": []},
        runtime=None,
    )


def test_tool_schemas_are_measured_the_way_a_provider_is_sent_them() -> None:
    """Tool schemas are measured through `convert_to_openai_tool`, as LangChain binds them.

    Reading attributes off a plain decorated callable measures almost nothing
    (`tests/test_context_floor.py`).
    """
    from chemclaw.agent.chemclaw_agent import _capability_tools
    from chemclaw.agent.profile_discovery import load_profiles
    from chemclaw.agent.profiles import get_profile

    load_profiles()
    tools = _capability_tools(get_profile("default"))

    tokens = estimate_tool_schemas(tools)

    assert tokens > 10_000, f"the default profile's tool schemas measured {tokens} tokens"
    assert estimate_tool_schemas([]) == 0
    assert estimate_tool_schemas([object()]) == 0, "an unmeasurable tool must not raise"


def test_the_ratio_is_learned_from_a_real_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The observer feeds the calibration from the response of the call it wrapped.

    A fake model reporting usage proves the wiring.
    """
    monkeypatch.setattr(settings, "agent_context_calibration_min_calls", 1)
    # Comfortably above what one short turn estimates — the static prefix alone is ~43,000 tokens
    # — so a single observation moves the smoothed ratio past the 1.0 clamp and the wiring is
    # visible through the public surface rather than through the average's internals.
    billed = 200_000

    class _Usage(GenericFakeChatModel):
        """A fake model that reports usage, which is the only thing this test needs of it."""

        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            result = super()._generate(*args, **kwargs)
            message = cast(Any, result.generations[0].message)
            message.usage_metadata = {
                "input_tokens": billed,
                "output_tokens": 1,
                "total_tokens": billed + 1,
            }
            return result

    model = _Usage(messages=iter([AIMessage(content="done")]))
    monkeypatch.setattr(type(model), "bind_tools", lambda self, tools, **kw: self, raising=False)
    graph = build_langgraph_agent(model=model)

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="hello")]}))

    assert estimator_ratio() > 1.0, (
        "the model call's billed size never reached the calibration — the two numbers are in one "
        "function and comparing them is the whole fix"
    )
    # Through the exposition, because a gauge is bound to a live source rather than accumulated —
    # and because the exposition is what an operator actually reads.
    exposed = [
        float(line.split()[-1])
        for line in METRICS.render().splitlines()
        if line.startswith("chemclaw_context_estimator_ratio ")
    ]
    assert exposed == [pytest.approx(estimator_ratio(), abs=1e-4)], (
        f"the gauge does not publish what the budget is dividing by: {exposed}"
    )


def test_the_turn_record_says_whether_the_policy_acted() -> None:
    """`turn_costs` can only carry the two flags if something sets them on the turn's own record."""
    token = begin_context_watch()
    try:
        turn = current_context()
        assert turn is not None
        assert not turn.compacted and not turn.unreducible
        turn.compacted = True
    finally:
        end_context_watch(token)

    assert current_context() is None, "the record outlived the turn it describes"


def test_a_clean_overrun_reading_means_the_request_fits_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean overrun reading means the request fits its budget.

    `compaction._record_overrun` compares the sent thread with `effective_trigger(budget)`. Swept, a
    clean reading implies:

    - always: `(min(prefix, basis) + sent) * ratio <= budget`, a bound on the request with no window
      declared. The ratio applies to prefix and thread together; this is the unit-level half of
      `tests/test_compaction.py::test_a_calibrated_process_does_not_bill_past_its_budget`. A prefix
      past `agent_context_prefix_basis` is paid in spend
      (`D-2026-10-02-a-prefix-beyond-the-derivation-basis-is-paid-in-spend-not-thread`).
    - with a window: also `prefix + sent + llm_max_tokens <= window`.

    Exception: when the prefix alone exhausts the budget the trigger floors at 1, which
    `effective_trigger` reports. The prefix is set on the contextvar directly, since the claim is
    the arithmetic.
    """
    unsound: list[tuple[int, int, int, int, float, int, int, int]] = []
    degenerate = 0
    degenerate_undeclared = 0
    undeclared_points = 0
    for window, prefix, reservation, budget, ratio, basis in itertools.product(
        (0, 32_000, 64_000, 128_000, 200_000, 1_000_000),
        (0, 5_000, 20_000, 43_175, 120_000, 250_000),
        (1_024, 4_096, 32_000),
        (10_000, 30_000, 100_000, 400_000),
        (1.0, 1.5, 2.2, 4.0),
        # The basis the budget is charged up to: none of the prefix, part of it, and all of it —
        # the last is the arithmetic before `agent_context_prefix_basis` existed.
        (0, 43_175, 10**9),
    ):
        monkeypatch.setattr(settings, "llm_context_window_tokens", window)
        monkeypatch.setattr(settings, "llm_max_tokens", reservation)
        monkeypatch.setattr(settings, "agent_context_prefix_basis", basis)
        reset_calibration()
        _observe(ratio)
        token = _prefix.set(prefix)
        try:
            trigger = effective_trigger(budget)
        finally:
            _prefix.reset(token)
        for sent in {0, 1, trigger // 2, trigger - 1, trigger}:
            if sent < 0 or sent > trigger:
                continue
            # The budget is charged the prefix up to the basis; the window, whole.
            charged = min(prefix, basis)
            fits_budget = (charged + sent) * estimator_ratio() <= budget
            fits_window = (not window) or (prefix + sent + reservation <= window)
            if not window:
                undeclared_points += 1
            if fits_budget and fits_window:
                continue
            if trigger == 1:
                degenerate += 1
                degenerate_undeclared += 0 if window else 1
                continue
            unsound.append((window, prefix, reservation, budget, ratio, basis, trigger, sent))

    assert not unsound, (
        "a request the overrun indicator reads as clean does not fit the budget it was cut to: "
        f"{unsound[:5]}"
    )
    assert degenerate, (
        "the sweep never reached the prefix-exhausts-the-budget corner, so it is not evidence that "
        "the corner is the only exception"
    )
    assert undeclared_points, (
        "the sweep declared a window at every point, which is exactly the case this invariant was "
        "extended to cover"
    )
    assert degenerate_undeclared, (
        "the floor was only ever reached with a window declared, so this sweep is not evidence "
        "about the corner the unconditional subtraction newly opens — a configured budget below "
        "the prefix, which is what a deployment reaches by lowering either context setting under "
        "its own bound tool surface"
    )


class _NamedTool:
    """The only thing `_tool_name` and a fake conversion need of a bound tool: its name."""

    def __init__(self, name: str) -> None:
        """Name it, because the memo below is keyed by exactly this."""
        self.name = name


def _costly_conversion(
    per_tool_seconds: float, converted: list[str], on_threads: list[int] | None = None
) -> Callable[[Any], dict[str, Any]]:
    """`convert_to_openai_tool` with its real cost made explicit and its real shape kept.

    Every call is recorded with its thread. A `time.sleep` rather than a busy-wait: both block the
    calling thread, but sleep releases the GIL, so the burst test measures where the work runs
    rather than CPython's GIL arbitration.
    """

    def convert(tool: Any) -> dict[str, Any]:
        converted.append(getattr(tool, "name", repr(tool)))
        if on_threads is not None:
            on_threads.append(threading.get_ident())
        time.sleep(per_tool_seconds)
        return {"type": "function", "function": {"name": tool.name, "description": "x" * 400}}

    return convert


def _tool_request(tools: list[Any]) -> Any:
    """A `ModelRequest` carrying a bound tool surface, which `_request` above deliberately omits."""
    from langchain.agents.middleware import ModelRequest

    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="x")])),
        messages=[HumanMessage(content="hello")],
        system_message=SystemMessage(content="you are a process chemist."),
        tools=tools,
        state={"messages": []},
        runtime=None,
    )


def test_the_tool_schema_sweep_runs_once_per_process_not_once_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool-schema sweep runs once per process, not once per turn.

    `MeasureRequestPrefix` is built per turn, so the memo is module-level. Counted, not timed; the
    prefix each turn publishes is asserted too, since a wrong memo is worse than none.
    """
    _SCHEMA_TOKENS.clear()
    converted: list[str] = []
    monkeypatch.setattr(
        "langchain_core.utils.function_calling.convert_to_openai_tool",
        _costly_conversion(0.0, converted),
    )
    tools = [_NamedTool(f"tool_{i}") for i in range(8)]
    published: list[int] = []

    def handler(_request: Any) -> str:
        published.append(prefix_tokens())
        return "done"

    turns = 12
    for _ in range(turns):
        MeasureRequestPrefix().wrap_model_call(_tool_request(tools), handler)

    assert len(converted) == len(tools), (
        f"the {len(tools)}-tool surface was converted {len(converted)} times over {turns} turns: "
        "the memo is per middleware, and a middleware is per turn, so every turn re-measures a "
        "surface this process has already measured"
    )
    assert len(set(published)) == 1 and published[0] > 0, (
        f"turns disagreed about the same surface's prefix: {sorted(set(published))}"
    )


def test_a_burst_of_cold_prefix_measurements_leaves_the_loop_schedulable() -> None:
    """A burst of cold prefix measurements leaves the loop schedulable.

    One event loop carries every stream and probe, and many turns can miss the memo at once, so the
    sweep runs off the loop. Asserted: every conversion runs on a thread other than the loop's, and
    a heartbeat keeps turning during the burst. The control replaces `asyncio.to_thread` with an
    inline await and must fail both. Thread identity catches work on the loop; the rate catches work
    moved to a thread and then waited on synchronously from the loop.
    """
    _SCHEMA_TOKENS.clear()
    converted: list[str] = []
    on_threads: list[int] = []
    per_tool = 0.04
    turns = 4
    # Distinct tools per turn, so the memo cannot confound the comparison: both arms do the same
    # conversions whatever the scheduling.
    tools_per_turn = [[_NamedTool(f"turn{n}_tool{i}") for i in range(8)] for n in range(turns)]
    conversions = turns * len(tools_per_turn[0])
    work = conversions * per_tool

    async def handler(_request: Any) -> str:
        return "done"

    async def _inline(call: Any, *args: Any, **kwargs: Any) -> Any:
        """`asyncio.to_thread` with the thread taken out — the mutation, run as the control."""
        return call(*args, **kwargs)

    async def heartbeat(stop: asyncio.Event, beats: list[float]) -> None:
        while not stop.is_set():
            await asyncio.sleep(0)
            beats.append(time.perf_counter())

    async def burst() -> tuple[int, float, int]:
        """The four measurements under the heartbeat; returns (loop turns, wall, the loop's thread).

        Both arms use the identical code path, differing only in whether `asyncio.to_thread` is
        patched; beats after the gather are dropped.
        """
        beats: list[float] = []
        stop = asyncio.Event()
        beat = asyncio.create_task(heartbeat(stop, beats))
        await asyncio.sleep(0.05)
        beats.clear()
        started = time.perf_counter()
        await asyncio.gather(
            *(
                MeasureRequestPrefix().awrap_model_call(_tool_request(turn_tools), handler)
                for turn_tools in tools_per_turn
            )
        )
        wall = time.perf_counter() - started
        stop.set()
        await beat
        return sum(1 for at in beats if at <= started + wall), wall, threading.get_ident()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "langchain_core.utils.function_calling.convert_to_openai_tool",
            _costly_conversion(per_tool, converted, on_threads),
        )
        beats, wall, loop_thread = asyncio.run(burst())
        offloaded_threads = list(on_threads)
        on_threads.clear()
        _SCHEMA_TOKENS.clear()
        with pytest.MonkeyPatch.context() as mutated:
            # Patched on `asyncio` itself, which is what `context_budget` resolves the name
            # through, and undone by the context manager before anything else runs.
            mutated.setattr(asyncio, "to_thread", _inline)
            blocked_beats, blocked_wall, blocked_loop_thread = asyncio.run(burst())
        blocked_threads = list(on_threads)

    assert len(offloaded_threads) == len(blocked_threads) == conversions, (
        f"expected {conversions} conversions per arm, got {len(offloaded_threads)} offloaded and "
        f"{len(blocked_threads)} on the loop, so the two arms did not do the same work"
    )
    assert blocked_wall > work / 2, (
        f"the un-offloaded control ran in {blocked_wall * 1000:.0f} ms against "
        f"{work * 1000:.0f} ms of serial work, so it is not doing the work this assertion is about"
    )
    assert loop_thread not in offloaded_threads, (
        f"{offloaded_threads.count(loop_thread)} of {conversions} tool-schema conversions ran on "
        "the event loop's own thread: the sweep is running on the loop that serves every other "
        "turn's stream and both kubelet probes"
    )
    # Loop turns per second against the in-process control, which a sleep on the loop pins near
    # zero; the two differ by orders of magnitude.
    offloaded_rate = beats / wall
    blocked_rate = max(blocked_beats, 1) / blocked_wall
    assert offloaded_rate > 20 * blocked_rate, (
        f"the event loop was scheduled {beats} time(s) in a {wall * 1000:.0f} ms burst "
        f"({offloaded_rate:.1f}/s), against {blocked_beats} in {blocked_wall * 1000:.0f} ms "
        f"({blocked_rate:.1f}/s) with the measurement on the loop — "
        f"{offloaded_rate / blocked_rate:.1f}x against a floor of 20x, although no conversion ran "
        "on the loop's thread: the loop is waiting on the sweep instead of running it, which "
        "holds it just the same"
    )
    # Last, and it is not optional: the control must have *been* the defect, or neither assertion
    # above was shown to separate anything in this run.
    assert set(blocked_threads) == {blocked_loop_thread}, (
        "with the offload neutered the conversions still ran off the loop's thread, so thread "
        "identity cannot tell the defect from the fix here"
    )


# ---------------------------------------------------------------------------------------------
# Counting the prefix exactly, where that can be done without reaching the network.
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def _clean_encoding() -> Any:
    """No test inherits another's resolved encoding or its memoised schema sweep."""
    reset_encoding()
    _SCHEMA_TOKENS.clear()
    yield
    reset_encoding()
    _SCHEMA_TOKENS.clear()


def _encoding_or_skip() -> Any:
    """The configured encoding, or a skip saying what this run is therefore not evidence about.

    The merge table is a deployment artefact (baked into the image, named by
    `TIKTOKEN_CACHE_DIR`); without it the fallback tests below apply.
    """
    encoding = _encoding()
    if encoding is None:
        pytest.skip(
            "no tiktoken merge table is cached here, so the exact-counting half of the budget "
            "is not exercised by this run; bake one and set TIKTOKEN_CACHE_DIR"
        )
    return encoding


def test_the_prompt_is_counted_with_the_encoding_rather_than_estimated(
    _clean_encoding: None,
) -> None:
    """The prompt is counted with the encoding rather than estimated.

    The system message arrives as a block list (`[{"type": "text", ...}]`), so counting only strings
    would silently fall back. Asserted that the count moved, in the direction chars/4 is wrong: it
    over-charges prose.
    """
    encoding = _encoding_or_skip()
    from chemclaw.agent.chemclaw_agent import instructions_for
    from chemclaw.agent.profile_discovery import load_profiles
    from chemclaw.agent.profiles import get_profile

    load_profiles()
    # This repository's own instructions, not invented prose: a hand-written string repeated 200
    # times tokenises at 1.0000x and the first version of this test asserted against that, which
    # measured the fixture rather than the prompt.
    text = instructions_for(get_profile("default"))
    prompt = SystemMessage(content=[{"type": "text", "text": text}])

    exact = _message_tokens(prompt)
    estimated = int(count_tokens_approximately([prompt]))

    assert exact < estimated, (
        f"the block-list prompt counted {exact} against the estimator's {estimated}: on this "
        "repository's own prose chars/4 over-charges by ~15%, so a count that did not fall is a "
        "count that fell back to the estimator"
    )
    envelope = int(count_tokens_approximately([prompt.model_copy(update={"content": ""})]))
    assert exact == envelope + len(encoding.encode_ordinary(text))


def test_the_schema_half_is_counted_with_the_encoding_too(_clean_encoding: None) -> None:
    """The schema half is counted with the encoding too.

    chars/4 is very close on JSON schemas, so this asserts close agreement rather than a direction.
    """
    _encoding_or_skip()
    from chemclaw.agent.chemclaw_agent import _capability_tools
    from chemclaw.agent.profile_discovery import load_profiles
    from chemclaw.agent.profiles import get_profile

    load_profiles()
    # Real schemas, for the reason the test above gives: `"x " * 500` measures 1.94x because a
    # repeated two-character token is nothing like a JSON schema, and asserting against it would
    # be asserting a property of the fixture.
    tools = _capability_tools(get_profile("default"))

    exact = estimate_tool_schemas(tools)
    _SCHEMA_TOKENS.clear()
    with_estimator = _with_encoding("", lambda: estimate_tool_schemas(tools))

    assert exact > 10_000 and with_estimator > 10_000
    assert abs(exact - with_estimator) < with_estimator * 0.05, (
        f"{exact} exact against {with_estimator} estimated over {len(tools)} schemas: these two "
        "counters disagree far more on JSON than the 1.3% this surface measured on 2026-09-16 "
        "(29,879 against 29,489) and the 0.05% the bound surface measured"
    )


def _with_encoding(name: str, call: Callable[[], int]) -> int:
    """Run `call` with `llm_token_encoding` set to `name`, putting the setting back afterwards."""
    previous = settings.llm_token_encoding
    settings.llm_token_encoding = name
    reset_encoding()
    try:
        return call()
    finally:
        settings.llm_token_encoding = previous
        reset_encoding()


def test_a_special_token_spelling_is_counted_rather_than_raising(_clean_encoding: None) -> None:
    """A special-token spelling is counted rather than raising.

    `encode` raises on `<|endoftext|>` and similar, which a server could return; this path uses
    `encode_ordinary`.
    """
    _encoding_or_skip()

    tokens = _message_tokens(SystemMessage(content="<|endoftext|> and <|fim_prefix|> in a result"))

    assert tokens > 0


def test_an_unpriceable_block_falls_back_to_the_estimator_whole(_clean_encoding: None) -> None:
    """An unpriceable block falls back to the estimator for the whole message.

    `_text_tokens` returning `None` keeps the two counters from mixing inside one message.
    """
    _encoding_or_skip()
    picture = SystemMessage(
        content=[{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "x"}}]
    )

    assert _message_tokens(picture) == int(count_tokens_approximately([picture]))


def test_no_baked_cache_means_the_estimator_and_no_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _clean_encoding: None
) -> None:
    """No baked cache means the estimator and no socket.

    `tiktoken.get_encoding` fetches over HTTPS on a miss, which on an air-gapped network may hang.
    So `_baked_cache_dir` asks whether a table was baked, and every socket constructor and name
    lookup fails this test if reached.
    """
    import socket

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the context budget reached the network to resolve an encoding")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)

    assert _encoding() is None
    message = SystemMessage(content="counted the old way")
    assert _message_tokens(message) == int(count_tokens_approximately([message]))


def test_an_encoding_nobody_baked_costs_accuracy_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, _clean_encoding: None
) -> None:
    """An encoding nobody baked costs accuracy and nothing else.

    A populated cache without the configured encoding costs one swallowed attempt per process and a
    WARNING naming it. Driven through a resolution that raises.
    """

    def explode(name: str) -> Any:
        raise RuntimeError(f"no merge table for {name}")

    import tiktoken

    monkeypatch.setattr(tiktoken, "get_encoding", explode)
    monkeypatch.setattr(
        "chemclaw.agent.context_budget._baked_cache_dir", lambda: __import__("pathlib").Path(".")
    )

    assert _encoding() is None
    message = SystemMessage(content="counted the old way")
    assert _message_tokens(message) == int(count_tokens_approximately([message]))


def test_the_cache_directory_this_module_asks_about_is_the_one_tiktoken_reads() -> None:
    """`_baked_cache_dir` transcribes `read_file_cached`'s resolution; the upstream shape is pinned.

    Including presence semantics: `"TIKTOKEN_CACHE_DIR" in os.environ`, with an empty value meaning
    caching disabled (`cache_dir == ""`).
    """
    import inspect

    import tiktoken.load

    source = inspect.getsource(tiktoken.load.read_file_cached)

    for expected in (
        '"TIKTOKEN_CACHE_DIR"',
        '"DATA_GYM_CACHE_DIR"',
        '"data-gym-cache"',
        '"TIKTOKEN_CACHE_DIR" in os.environ',
        'cache_dir == ""',
    ):
        assert expected in source, (
            f"tiktoken.load.read_file_cached no longer mentions {expected}: "
            "`context_budget._baked_cache_dir` transcribes that resolution and is now wrong"
        )


def test_the_baked_cache_question_is_answered_the_way_tiktoken_answers_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _clean_encoding: None
) -> None:
    """The baked-cache question is answered the way tiktoken answers it, every arm driven.

    A `Path` returned here means "loading is safe, nothing will be fetched". The empty-string arm is
    driven through `_encoding()` too, because the return value decides whether a socket opens. A
    sentinel failing on any host is the assertion: `core/netguard.py` exempts loopback, which a
    proxy variable can redirect to.
    """
    import socket
    import tempfile

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the context budget reached the network to resolve an encoding")

    for target, name in (
        (socket, "getaddrinfo"),
        (socket, "gethostbyname"),
        (socket, "create_connection"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
    ):
        monkeypatch.setattr(target, name, refuse)

    baked = tmp_path / "baked"
    baked.mkdir()
    (baked / "fb374d419588a4632f3f557e76b4b70aebbca790").write_bytes(b"a merge table")
    bare = tmp_path / "bare"
    bare.mkdir()
    # The no-variable arm resolves through `tempfile.gettempdir()`, so the temp directory is moved
    # under the fixture rather than the host's — otherwise this arm answers with whatever the
    # machine running the suite happens to have cached, which is exactly how the defect hid.
    temp = tmp_path / "tmp"
    (temp / "data-gym-cache").mkdir(parents=True)
    (temp / "data-gym-cache" / "blob").write_bytes(b"a merge table")
    monkeypatch.setattr(tempfile, "tempdir", str(temp))

    monkeypatch.delenv("DATA_GYM_CACHE_DIR", raising=False)

    # An empty value is upstream's "caching disabled", never "fall through to the next spelling".
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")
    assert _baked_cache_dir() is None, (
        "an empty TIKTOKEN_CACHE_DIR is tiktoken's 'caching disabled, fetch every time'; reading "
        "it as 'unset' hands `_resolve_encoding` a directory tiktoken will not read and a fetch "
        "it will make"
    )
    assert _encoding() is None
    reset_encoding()

    # …and it still means that when the next spelling is populated, because upstream branches on
    # presence and never reaches `DATA_GYM_CACHE_DIR` at all.
    monkeypatch.setenv("DATA_GYM_CACHE_DIR", str(baked))
    assert _baked_cache_dir() is None
    monkeypatch.delenv("DATA_GYM_CACHE_DIR")

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(baked))
    assert _baked_cache_dir() == baked

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(bare))
    assert _baked_cache_dir() is None, "an empty directory holds no merge table to load"

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "never-created"))
    assert _baked_cache_dir() is None

    monkeypatch.delenv("TIKTOKEN_CACHE_DIR")
    assert _baked_cache_dir() == temp / "data-gym-cache", (
        "with neither variable set the cache is `data-gym-cache` under the system temp directory, "
        "which is where a `tiktoken` that has ever run puts it"
    )


def test_the_encoding_the_image_bakes_is_the_encoding_the_config_asks_for() -> None:
    """The encoding the image bakes is the encoding the config asks for.

    `deploy/Containerfile` bakes a merge table under `TIKTOKEN_CACHE_DIR`, and `llm_token_encoding`
    names what `_resolve_encoding` asks for. If they differ, a populated cache lacks the configured
    encoding and `tiktoken` fetches with no timeout. The cache directory is asserted too: a table
    baked where no runtime reads it is the same fetch.
    """
    import re

    containerfile = (Path(__file__).resolve().parents[1] / "deploy" / "Containerfile").read_text(
        encoding="utf-8"
    )

    baked = re.search(r"tiktoken\.get_encoding\(['\"]([^'\"]+)['\"]\)", containerfile)
    assert baked, (
        "deploy/Containerfile no longer bakes a tiktoken merge table, so every pod resolves its "
        "encoding over the network on its first model call — or, air-gapped, never resolves one"
    )
    configured = cast(str, type(settings).model_fields["llm_token_encoding"].default)
    assert baked.group(1) == configured, (
        f"deploy/Containerfile bakes {baked.group(1)!r} and llm_token_encoding defaults to "
        f"{configured!r}: a shipped pod would find a populated cache without the table it wants "
        "and fetch it, which the air-gapped posture turns into a hang with no timeout"
    )

    bake_dir = re.search(r"TIKTOKEN_CACHE_DIR=(\S+) ", containerfile)
    run_dir = re.search(r"^\s+TIKTOKEN_CACHE_DIR=(\S+)\s*$", containerfile, flags=re.MULTILINE)
    assert bake_dir and run_dir, (
        "deploy/Containerfile no longer both bakes into and exports a cache dir"
    )
    assert bake_dir.group(1) == run_dir.group(1), (
        f"the image bakes the merge table into {bake_dir.group(1)} and runs with "
        f"TIKTOKEN_CACHE_DIR={run_dir.group(1)}: the baked table is never read"
    )
