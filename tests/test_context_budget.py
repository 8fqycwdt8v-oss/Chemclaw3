"""The budget's unit, its ceiling, and the two things that were never measured.

Three claims, and none of them is about the edits — they are about the arithmetic the edits are
handed, which is where the defects were:

1. **The unit.** chars/4 is within 4% on prose and tool schemas and roughly *half* the truth on
   structured tool results, so a budget denominated in it is not a budget in billed tokens. The
   ratio is measurable from the provider's own `input_tokens`, and `note_model_call` is where the
   two meet.
2. **The ceiling.** Nothing knew the model's context window, so the budget was a constant that
   happened to sit under most of them and the prefix — ~28,000 tokens on `default` — sat outside it.
3. **The prefix.** A `ContextEdit` cannot see it; a middleware can; the contextvar is the seam.
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from chemclaw.agent.context_budget import (
    MeasureRequestPrefix,
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


def test_an_uncalibrated_process_changes_nothing() -> None:
    """Below the sample floor the trigger is exactly the configured number.

    The property that makes this safe to ship: a deployment that upgrades and observes nothing gets
    the behaviour it had, and the first turns of a fresh process are not budgeted against one
    unusual sample.
    """
    assert estimator_ratio() == 1.0
    assert effective_trigger(100_000) == 100_000

    note_model_call(10_000, 22_000)

    assert estimator_ratio() == 1.0, "one call moved the budget"
    assert effective_trigger(100_000) == 100_000


def test_a_measured_underestimate_tightens_the_trigger() -> None:
    """A budget in billed tokens becomes a smaller number in the estimator's unit.

    2.2x is the measured figure for a thread of connector JSON results — the payload class the
    tool-result edit exists to reclaim — so a 100,000-token budget is really ~45,000 estimated
    tokens, and that is the line the edits must compare against.
    """
    _observe(2.2)

    assert estimator_ratio() == pytest.approx(2.2, abs=0.15)
    assert 40_000 < effective_trigger(100_000) < 50_000


def test_it_never_loosens_a_budget() -> None:
    """An overestimate leaves the trigger alone rather than raising it.

    The asymmetry is the whole safety argument. chars/4 *over*-estimates prose by up to 20%, and a
    ratio below 1.0 would let a thread grow past what the deployment asked to spend in order to
    correct a conservative estimate — trading a hard provider failure for a rounding error. Clamped
    at 1.0, the worst a mismeasurement can do is compact earlier than necessary.
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
    """With a window declared the thread gets what the model has left, not what config hoped for.

    Undeclared is the default and keeps today's behaviour — which is the honest state for an
    endpoint whose window this repository cannot know.
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
    """The prefix is ~28,000 tokens on `default` and it is not in the thread — so it is subtracted.

    Driven through the middleware rather than by setting the contextvar, because the claim is that
    a *request*'s prefix reaches the edits: the seam is the whole point and setting the variable by
    hand would assert nothing about it.
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
    """Through `convert_to_openai_tool`, which is what LangChain binds with.

    `tests/test_context_floor.py` records why: reading `.name`/`.description` off a plain decorated
    callable finds a repr, an empty string and `None`, and measures the whole tool surface at ~11
    tokens per tool — a number that would make every budget derived from it meaningless.
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

    The measurement and the bill exist in one function, three lines apart, and nothing compared
    them — which is why the budget stayed in the wrong unit. A fake model reporting usage is enough
    to prove the wiring, and the wiring is the part that was missing.
    """
    monkeypatch.setattr(settings, "agent_context_calibration_min_calls", 1)
    # Comfortably above what one short turn estimates — the static prefix alone is ~28,000 tokens
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


def test_a_clean_overrun_reading_means_the_request_fits_its_declared_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where a window is declared, `_record_overrun`'s silence is sound — swept, not argued.

    `compaction._record_overrun` compares the thread against `effective_trigger(budget)`, and
    `docs/planning/BACKLOG.md` reads that as a tautology: the window edit has just cut the thread to
    that very number, so the indicator "reads clean by construction". The first half is right and
    the conclusion drawn from it is not, and the difference decides whether a window-aware arm in
    that function has anything to catch. This is the sweep that answers it.

    The invariant: with `llm_context_window_tokens` declared, `sent <= effective_trigger(budget)`
    implies `prefix + sent + llm_max_tokens <= window`. It holds because the trigger is *derived*
    from `window - prefix - llm_max_tokens` and the ratio can only tighten it further — so a clean
    reading is a request that fits the model, and the failure this counter exists to lead cannot
    happen silently under a declared window.

    **The one exception is stated rather than swept under.** When the prefix plus the output
    reservation already exceeds the window there is no room for any thread at all, and the trigger
    floors at 1 rather than going negative. A thread of 0 or 1 estimated tokens then reads clean on
    a request that cannot fit — unreachable in practice, since `count_tokens_approximately` charges
    every non-empty list several tokens, and any real thread ticks the counter.

    The prefix is set on the contextvar directly here because the claim is about the arithmetic;
    that a *request*'s prefix reaches it is
    `test_a_declared_window_subtracts_the_measured_prefix`'s claim, and is proven through the
    middleware there.
    """
    unsound: list[tuple[int, int, int, int, float, int, int]] = []
    degenerate = 0
    for window in (32_000, 64_000, 128_000, 200_000, 1_000_000):
        for prefix in (0, 5_000, 20_000, 43_175, 120_000, 250_000):
            for reservation in (1_024, 4_096, 32_000):
                for budget in (10_000, 30_000, 100_000, 400_000):
                    for ratio in (1.0, 1.5, 2.2, 4.0):
                        monkeypatch.setattr(settings, "llm_context_window_tokens", window)
                        monkeypatch.setattr(settings, "llm_max_tokens", reservation)
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
                            if prefix + sent + reservation <= window:
                                continue
                            if trigger == 1:
                                degenerate += 1
                                continue
                            unsound.append(
                                (window, prefix, reservation, budget, ratio, trigger, sent)
                            )

    assert not unsound, (
        "a request the overrun indicator reads as clean does not fit its declared window: "
        f"{unsound[:5]}"
    )
    assert degenerate, (
        "the sweep never reached the prefix-exceeds-window corner, so it is not evidence that the "
        "corner is the only exception"
    )
