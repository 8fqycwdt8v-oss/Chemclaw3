"""The budget's unit, its ceiling, and the two things that were never measured.

Three claims, and none of them is about the edits — they are about the arithmetic the edits are
handed, which is where the defects were:

1. **The unit.** chars/4 is within 4% on prose and tool schemas and roughly *half* the truth on
   structured tool results, so a budget denominated in it is not a budget in billed tokens. The
   ratio is measurable from the provider's own `input_tokens`, and `note_model_call` is where the
   two meet.
2. **The ceiling.** Nothing knew the model's context window, so the budget was a constant that
   happened to sit under most of them.
3. **The prefix.** ~43,000 tokens on `default`, outside the budget entirely — and then outside it
   in every *shipped* configuration, because the subtraction that fixed that was guarded by a
   window setting nothing declares. It is charged unconditionally now, which makes
   `agent_context_token_budget` a bound on the whole request. A `ContextEdit` cannot see the
   prefix; a middleware can; the contextvar is the seam.
"""

import asyncio
import logging
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

    The property that makes this safe to ship: a deployment that upgrades and observes nothing gets
    the behaviour it had. It is a claim about *zero* samples, and it used to be a claim about the
    first twenty — see the test below for why that stopped being the shape.
    """
    assert estimator_ratio() == 1.0
    assert effective_trigger(100_000) == 100_000


def test_the_first_sample_is_believed_and_is_the_sample() -> None:
    """One observation calibrates the budget, and calibrates it to what was observed.

    **Two defects in one call, and the second hid the first.** `agent_context_calibration_min_calls`
    shipped at 20, so a process's first twenty model calls budgeted at the uncalibrated end — the
    *loose* end, since the ratio is clamped at 1.0 from below and believing a sample can therefore
    only tighten. And the average itself was seeded at 1.0 with `_ALPHA = 0.1`, so even with the
    floor lowered the answer stays mostly the seed for ~20 samples: the seed is divided back out
    now, which is the ordinary EWMA bias correction.

    Measured 2026-09-06 on a compiled graph with the connector surface bound, shipped defaults, a
    dense connector-JSON thread and a 128k window declared — model calls that went out over the
    123,904 tokens such a model accepts: **20** as shipped, **19** with the floor alone at 1, **1**
    with both. `core/config/agent.py` carries the argument; this is the arithmetic.

    Asserted as an identity rather than an inequality on purpose: after exactly one sample the
    answer is that sample, which is what "the seed is divided back out" means and what a bare
    `> 1.0` could not tell from the old 1.0675.
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
    """The seed is 1.0, and every test in this file reached that fact through `reset()` instead.

    `_CALIBRATION` is built once at module scope and the autouse fixture above re-seeds it through
    `reset()`, so `__init__`'s own `self._ratio = 1.0` had exactly one executing test in the whole
    repository and it reached it by *importing the module*. Seeded at 2.0 instead, a fresh process's
    first sample comes back as 11.5 rather than 2.5 — clamped to the maximum factor, so every pod's
    opening calls budget at a quarter of what was asked for — and the suite stays green, because
    `reset()` puts 1.0 back before any assertion looks.

    Constructed here rather than reached through the singleton, which is the whole point: a class
    whose constructor no test runs is a constructor no test asserts.
    """
    fresh = _Calibration()

    assert fresh.ratio() == 1.0, "an uncalibrated process must change nothing"

    fresh.note(10_000, 25_000)

    assert fresh.ratio() == pytest.approx(2.5, abs=1e-9), (
        f"one sample of 2.5 came back as {fresh.ratio()}: the bias correction divides out a seed "
        "of 1.0, so a constructor that seeds anything else answers with the seed instead"
    )


def test_the_plausible_band_includes_its_own_endpoints() -> None:
    """`_SANE` is a closed interval, and which way it closes decides whether a sample is believed.

    The band exists to drop a measurement fault — usage reported for a different request — rather
    than a tokenizer difference, so its endpoints are plausible ratios and are kept. Nothing
    asserted that: both `<=` could become `<` and 249 tests could not tell, because every sample
    any test feeds sits comfortably inside.

    Driven downwards from an already-calibrated ratio, because `ratio()` clamps at 1.0 from below
    and a low sample is otherwise invisible from outside — the observable consequence of accepting
    0.2 is that it *pulls a high average down*, which is exactly the safety property the clamp is
    there to bound.
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
    """The guard rejects *non-positive* estimates, and `<= 1` is a different rule with no test.

    `test_a_nonsense_sample_is_dropped` pins `note(0, n)` and `note(n, 0)`. Neither of them can
    distinguish `estimated <= 0` from `estimated <= 1`, and the second silently discards a real
    call — small, but it is the one every degenerate request makes, and a filter that widens
    without saying so is how a calibration stops calibrating.
    """
    for _ in range(40):
        note_model_call(1, 4)

    assert estimator_ratio() == pytest.approx(4.0, abs=0.05)


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

    Undeclared, the configured budget is the only bound — which is the honest state for an endpoint
    whose window this repository cannot know. It is not, any more, "today's behaviour": the prefix
    comes off in both arms (`test_the_prefix_reaches_the_edits_with_no_window_declared`), and this
    test isolates the *window* arm by running off the request path, where the prefix is 0.
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
    """The prefix is ~43,000 tokens on `default` and it is not in the thread — so it is subtracted.

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


def test_the_prefix_reaches_the_edits_with_no_window_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same seam, in the arm that ships — and the one this repository had left open.

    `llm_context_window_tokens` defaults to 0 and no value in `deploy/`, `infra/` or `.env.example`
    states one, so the `if window:` that used to guard the subtraction meant the prefix was charged
    against nothing in every real deployment: the system message, the skills listing and every bound
    tool schema were outside the only budget there was. Measured end to end on 2026-09-04, a thread
    the policy cut to its 90,030-token budget left as a 137,301-token request at a 128k model with
    the overrun indicator flat.

    So `agent_context_token_budget` is a bound on *request* spend now, and this is that sentence as
    an assertion: with no window declared, the trigger the edits compare against is the configured
    budget **minus** what this particular request already costs before a word of conversation.

    Through the middleware, for `test_a_declared_window_subtracts_the_measured_prefix`'s reason: the
    claim is about a request's prefix reaching the edits, and setting the contextvar by hand would
    assert nothing about the seam that carries it.
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
    """The degenerate case, and the reason it is loud rather than silent.

    A trigger of 1 is not a small budget, it is "reduce on every model call": every non-empty thread
    is over it, so the lossless edit clears every reclaimable tool result and the window cuts back
    to the newest group, on every call, forever. Raising instead is not the alternative — these run
    inside a middleware and an exception there costs the turn — so the floor stands and the fact is
    reported.

    **It is reachable from a plain misconfiguration now, not only from the window corner.** Before
    the prefix was charged unconditionally it took a declared window narrower than its own prefix;
    it now takes any configured budget below the prefix — which is where the shipped
    `agent_tool_result_clear_trigger` sat, unnoticed, for as long as the number was derived from a
    prefix measured with no connector bound. Both defaults were re-derived on 2026-09-05 from the
    honest prefix, and
    `tests/test_compaction.py::test_the_shipped_clear_trigger_clears_the_prefix_it_is_charged` is
    what asserts the shipped configuration is out of this state — the **opposite** state to the one
    this test drives — against a prefix that includes the connector surface, which is the half that
    made the previous assertion vacuous.

    No shipped figure is restated here beyond that, deliberately: this test is about the mechanism,
    and a default quoted in it is a claim about a commit
    (`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`).

    Once per distinct `(configured, prefix, window)`, because the condition is static: it is the
    same on every model call of every turn, so a line per call would be noise in exactly the
    situation an operator is trying to read.
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
    """One estimated token of thread is the smallest budget there is; it is not the floor.

    The floor and the smallest budget return the **same number**, so the only observable difference
    between them is the WARNING — which is the whole point of reporting it, since a deployment that
    asked for a tiny thread allowance and one that asked for a negative one behave identically and
    need to be told apart. `trigger < 1` could therefore become `<= 1` or `< 2` and 299 tests could
    not tell, because none of them drove the boundary: the test above drives 0 and below, this one
    drives exactly 1 and asserts the line is *not* emitted.

    The pair is run against one prefix so the two budgets differ by exactly one token, which is the
    only way the two predicates disagree.
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
    """The once-per-key set is bounded, because a key is three numbers a caller chooses.

    `_note_floored_trigger` de-duplicates on `(configured, prefix, window)` and the prefix moves
    with the bound tool surface, so the set is not closed by construction — `_MAX_REPORTED_FLOORS`
    is what stops a pathological deployment turning a once-per-condition warning into a log per
    model call. Nothing drove it: `>=` and `>` are the same for every key count any test reached.
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


def test_the_ambient_prefix_is_put_back_after_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A contextvar that is set and never reset is one turn's prefix budgeting the next one.

    Every existing assertion about this middleware reads `prefix_tokens()` from *inside* the
    handler, which proves the value is published and says nothing about the `finally`. Inverted to
    `if token is None`, the reset never runs on the path that matters and the ambient keeps the
    last measured prefix for whatever runs next in that context — a background sweep, the next turn
    on a reused context — while every test still passes.

    All three exits, because the guard is about which of them own a token: a call that returns, a
    call that raises, and a call whose measurement failed and therefore set nothing at all.
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

    Read inside the coroutine on purpose: `asyncio.run` copies the context, so a contextvar the
    call leaks would be invisible to an assertion made after it returns — which is how the async
    half of this guard stayed unasserted while the sync half was pinned.
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
    """`_record_overrun`'s silence is sound — swept, not argued, and now with no window needed.

    `compaction._record_overrun` compares the thread being sent against
    `effective_trigger(agent_context_token_budget)`. The question this sweep answers is what a
    *clean* reading of that counter is evidence of.

    **The predecessor of this test could only answer it under a declared window**, because that was
    the only case in which `effective_trigger` charged the request's prefix — so undeclared, the
    counter compared a thread against a number the prefix had never met, and a 137,301-token
    request left at a 128k model with the counter flat. The prefix is charged unconditionally now,
    so the invariant gets stronger rather than being relaxed:

    - **Always**: `sent <= effective_trigger(budget)` implies `(prefix + sent) * ratio <= budget`.
      That is the whole of what the setting now means — a bound on the *request*, not on the
      thread — and it holds with no window declared, which is every shipped deployment.

      **The parenthesis is the correction, and this sweep shipped without it.** It read
      `prefix + sent * ratio <= budget`, which charges the measured ratio to the thread alone and
      so asserts that the prefix bills at exactly one billed token per estimated one. Nothing
      measured that; measured 2026-09-06 the `default` prefix bills 0.985 and a connector-JSON
      thread ~1.6, so the blend `note_model_call` folds is dragged toward the prefix and the thread
      is permitted to grow into the difference. `effective_trigger` divided by that blend and this
      sweep multiplied by it in the same asymmetric way, so the two agreed with each other at every
      one of these points while a driven request billed 140,500 against a 119,000 budget. Written
      whole, this sweep is red against the old arithmetic at every point with `ratio > 1` and
      `prefix > 0` — it is the unit-level half of
      `tests/test_compaction.py::test_a_calibrated_process_does_not_bill_past_its_budget`.
    - **And where a window is declared**, additionally `prefix + sent + llm_max_tokens <= window`,
      which is D-2026-08-28's property, kept: the window arm still caps the budget at
      `window - llm_max_tokens` before the prefix comes off.

    Both follow from the trigger being *derived* from those quantities, and from the ratio being
    clamped at 1.0 so it can only tighten. What a clean reading is therefore evidence *of* is now a
    statement about the configured budget in every configuration, and about the provider's real
    limit wherever a deployment states one.

    **The one exception is stated rather than swept under, and it is no longer exotic.** When the
    prefix alone exhausts the budget there is no room for any thread and the trigger floors at 1, so
    a thread of 0 or 1 estimated tokens reads clean on a request that is already over. That corner
    used to need a window narrower than its own prefix; it is now reachable from a plain
    misconfiguration — any configured budget under a prefix that measured 43,175 tokens on
    2026-09-04 and grows with every bound tool. That is why `effective_trigger` reports a floor
    instead of returning it silently. **No shipped default sits in it**:
    `agent_tool_result_clear_trigger` is derived as the ratchet ceiling plus 30,000 of thread, and
    `tests/test_compaction.py::test_the_shipped_clear_trigger_clears_the_prefix_it_is_charged` pins
    that band end to end.

    The prefix is set on the contextvar directly here because the claim is about the arithmetic;
    that a *request*'s prefix reaches it is
    `test_the_prefix_reaches_the_edits_with_no_window_declared`'s claim, and is proven through the
    middleware there.
    """
    unsound: list[tuple[int, int, int, int, float, int, int]] = []
    degenerate = 0
    degenerate_undeclared = 0
    undeclared_points = 0
    for window in (0, 32_000, 64_000, 128_000, 200_000, 1_000_000):
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
                            fits_budget = (prefix + sent) * estimator_ratio() <= budget
                            fits_window = (not window) or (prefix + sent + reservation <= window)
                            if not window:
                                undeclared_points += 1
                            if fits_budget and fits_window:
                                continue
                            if trigger == 1:
                                degenerate += 1
                                degenerate_undeclared += 0 if window else 1
                                continue
                            unsound.append(
                                (window, prefix, reservation, budget, ratio, trigger, sent)
                            )

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
    per_tool_seconds: float, converted: list[str]
) -> Callable[[Any], dict[str, Any]]:
    """`convert_to_openai_tool` with its real cost made explicit and its real shape kept.

    A busy-wait rather than a `sleep`, because what the two tests below measure is a *CPU* block on
    the event loop and a sleeping stand-in would release the loop exactly where the real one does
    not. Every call is recorded, so "how often was the surface swept" is a count rather than a
    timing inference.
    """

    def convert(tool: Any) -> dict[str, Any]:
        converted.append(getattr(tool, "name", repr(tool)))
        deadline = time.perf_counter() + per_tool_seconds
        while time.perf_counter() < deadline:
            pass
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
    """The memo outlives the middleware, because the middleware is per turn and the answer is not.

    `MeasureRequestPrefix` is constructed inside the compaction group of a graph `langgraph_agent`
    compiles **per turn**, so an instance memo is cold at every turn's first model call: measured
    2026-09-06 on the `default` profile with connectors bound (92 tools), every turn paid ~20 ms of
    `convert_to_openai_tool` over a surface that had not changed since the process started, and the
    process's first turn paid ~100 ms.

    Counted rather than timed, because the claim is about how often the sweep runs. The prefix each
    turn publishes is asserted too: a memo that returns the wrong number is worse than no memo.
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
    """The front door has one event loop, and a memo miss must not own it.

    One uvicorn worker carries every SSE stream, both kubelet probes and the submission side of
    every token validation, and `service_max_concurrent_turns` turns may take their first model
    call together — a pod rollout, a UI reconnect storm, several chemists hitting send. On a cold
    process every one of those misses the memo, so what is asserted here is the *gap*: with the
    measurement on the loop the 12 sweeps ran back to back in one iteration and the 1 ms heartbeat
    was not serviced once for the whole of it (measured: worst gap equal to the total work).

    A ratio rather than a wall clock, and a generous one: offloading buys no parallelism — the GIL
    is held between switch intervals — so what it buys is the loop being *scheduled* during the
    work, which `api/runner.py` measured at 3.1x for the graph build. Anything at or near 1.0 is
    the block this test exists to keep out.

    **The control is run rather than assumed, and that is a correction.** This assertion used to
    divide a *nominal* constant — `turns * len(tools) * per_tool`, the work if every conversion ran
    end to end — and compare the worst gap against a third of it. But the twelve bursts overlap, so
    that denominator is a duration the burst never takes: measured over ten runs here, the real wall
    was 54–166 ms against a nominal 384 ms, and `worst` tracked `wall` to within a few ms *every
    time*. So what the old form actually measured was how much the twelve busy-waits happened to
    overlap — thread-pool scheduling luck against core count and machine load — and it failed about
    one run in five, on CI at 129 ms against a 128 ms line, a 0.8% miss.

    The docstring above already named the right basis and never ran it. It does now: the same burst
    runs with `asyncio.to_thread` neutered, which does the identical work on the loop, and the
    async path must leave the loop schedulable by a clear factor against *that* measurement. Both
    numbers then come from this process, this core count and this load.

    **In-process was not enough, and the fix is to stop measuring a duration.** On 2026-09-20 this
    measured **1.289x** against a fixed `/ 1.3` margin — a 0.9% miss — inside a 46-minute run
    competing with three subagents, and reddened `check` on a pull request containing zero files
    under `src/`. `D-2026-09-13-a-stable-failure-set-is-not-two-green-runs` tabulates it as failing
    2 of 5 parallel runs with the serial column reading an unqualified "passes"; it passes serially
    *on an unloaded machine*, and a merged ADR is never edited, so that table still tells a reader
    the serial gate is safe from this. It is not. **Parallelism was never the mechanism — load
    is**, and `-n 4` is one way to produce it.

    Raising the control's size was tried first and is not enough either, for a reason worth writing
    down: `_costly_conversion` busy-waits to a `perf_counter` deadline, so the control's duration is
    pinned by the wall clock and does not move with the machine. A ratio against it is therefore an
    *absolute* bound wearing a ratio's clothes — the numerator grows under load and the denominator
    does not. Measured under six CPU spinners on four cores, that form fell to **1.16x** against a
    bar of 4.

    **So the quantity is a count: how many times the loop was scheduled while the work ran.** That
    is the property the test is named for, and it is what the two outcomes actually differ in — a
    coroutine that never awaits inside `_measured` holds the loop for the whole gather, so the
    un-offloaded arm is scheduled essentially never, however fast or slow the box is. Measured,
    four runs quiet: **18-28 beats offloaded against 0-1**. Under six spinners on four cores:
    **13-26 against 1**. The bar at 4 sits 3.2x below the worst honest run *under load* and 4x
    above the block, where every duration-shaped form of this assertion has had the defect and the
    passing case within 30% of each other on a busy machine.

    `turns` is 4 rather than 12, which narrows the regime: the premise above is
    `service_max_concurrent_turns` turns arriving together, and what is asserted is schedulability
    at four of them. The trade buys a control large enough to be unambiguous, and the sibling test
    below covers that the sweep runs once per process rather than once per turn at any width.

    **The defect *is* the control here**, which is why there is no third arm: neutering
    `asyncio.to_thread` is the mutation this test exists to fail, and running it as the basis makes
    the comparison a driven defect rather than a stand-in for one
    (`D-2026-09-18-a-mutation-watched-failing-is-half-a-guard`).
    """
    _SCHEMA_TOKENS.clear()
    converted: list[str] = []
    # 0.04 s and four turns rather than 0.004 and twelve — see the docstring: the control has to be
    # large next to machine noise, and the offloaded arm's gap grows with the *concurrency*, so
    # fewer and heavier turns move the two outcomes apart on both axes at once. Costs about 2 s.
    per_tool = 0.04
    turns = 4
    # **Distinct tools per turn, so the memo cannot confound the comparison.** With twelve turns
    # sharing eight tools, how many conversions actually run depends on how the turns interleave:
    # started together they all miss, run serially the first warms `_SCHEMA_TOKENS` for the rest.
    # That made two earlier drafts of this test wrong in opposite directions — a control that did
    # 8 conversions against the burst's 96 and read as faster than the thing it bounds, and then a
    # version that passed with the offload deleted, because the mutated path serialised and went
    # warm while the control stayed cold. Giving every turn its own tool names makes both arms do
    # the same 96 conversions whatever the scheduling, which is the only way the ratio means
    # anything.
    tools_per_turn = [[_NamedTool(f"turn{n}_tool{i}") for i in range(8)] for n in range(turns)]
    work = turns * len(tools_per_turn[0]) * per_tool

    async def handler(_request: Any) -> str:
        return "done"

    async def _inline(call: Any, *args: Any, **kwargs: Any) -> Any:
        """`asyncio.to_thread` with the thread taken out — the mutation, run as the control."""
        return call(*args, **kwargs)

    async def heartbeat(stop: asyncio.Event, beats: list[float]) -> None:
        while not stop.is_set():
            await asyncio.sleep(0.001)
            beats.append(time.perf_counter())

    async def burst() -> tuple[int, float]:
        """The same four measurements under the same heartbeat, offloaded or not.

        Which of the two it is depends on whether `asyncio.to_thread` is patched out around the
        call, so both arms go through the identical code path and differ by exactly the line under
        test.

        Returns **how many times the loop was scheduled while the work ran**, not how long the
        worst stall was. Beats after the gather returns are dropped, so the count is about the
        burst rather than about the teardown.
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
        return sum(1 for at in beats if at <= started + wall), wall

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "langchain_core.utils.function_calling.convert_to_openai_tool",
            _costly_conversion(per_tool, converted),
        )
        beats, wall = asyncio.run(burst())
        _SCHEMA_TOKENS.clear()
        with pytest.MonkeyPatch.context() as mutated:
            # Patched on `asyncio` itself, which is what `context_budget` resolves the name
            # through, and undone by the context manager before anything else runs.
            mutated.setattr(asyncio, "to_thread", _inline)
            blocked_beats, blocked_wall = asyncio.run(burst())

    assert converted, "nothing was measured, so this run says nothing about the loop"
    assert blocked_wall > work / 3, (
        f"the un-offloaded control ran in {blocked_wall * 1000:.0f} ms against "
        f"{work * 1000:.0f} ms of nominal work, so it is not doing the work this assertion is "
        "about — the control has stopped being a control"
    )
    # **A *rate*, against the control measured in the same process — and the absolute count it
    # replaces was the fourth duration-shaped form of this assertion to fail.**
    #
    # The two arms run for different lengths: on the CI runner the offloaded burst is ~660 ms and
    # the control ~1285 ms, because offloading is a halving. So a raw count penalises the arm for
    # being *faster* — fewer milliseconds in which to accumulate ticks — and compares it against a
    # bar calibrated where the offloaded window was 430-565 ms on a quicker box. Measured, the bar
    # at 4 failed 3 of 4 CI runs (2, 3, pass, 3 beats) while every local configuration gave 22-38:
    # isolated, under `--cov`, on two cores via `taskset`, and in the full `make cov` run.
    #
    # Beats per second, offloaded over blocked, is scale-free and is what the docstring above was
    # already reasoning about when it said "18-28 against 0-1":
    #
    #   CI run 1   2 / 0.664 s  vs 1 / 1.283 s  =  3.86x
    #   CI run 2   3 / 0.660 s  vs 1 / 1.285 s  =  5.84x
    #   CI run 4   3 / 0.655 s  vs 1 / 1.283 s  =  5.88x
    #   here       22-38 / 0.43-0.565 s         =  ~65-113x
    #   block      identical code path          =  1.0x by construction
    #
    # **The bar is 2.5 and the headroom is thinner than this file would like, which is worth saying
    # rather than hiding in a constant.** 2.5 sits 1.5x below the worst honest observation and 2.5x
    # above the block; the old standard of "3.2x below the worst honest run and 4x above the block"
    # is not available, because CI's honest run is 3.86x and the block is 1.0x. If a future CI
    # observation lands under 2.5, the *instrument* is what needs changing and not the bar: a
    # heartbeat of `asyncio.sleep(0)` would count loop turns instead of 1 ms ticks and give two
    # orders of magnitude more resolution, at the cost of measuring schedulability rather than
    # millisecond responsiveness — which is a different property, and the 1 ms tick is the one the
    # kubelet probes care about.
    #
    # `max(blocked_beats, 1)` because the control's count quantises at 0-1, and a control that got
    # zero would otherwise divide by zero on the arm this test exists to catch.
    offloaded_rate = beats / wall
    blocked_rate = max(blocked_beats, 1) / blocked_wall
    assert offloaded_rate > 2.5 * blocked_rate, (
        f"the event loop was scheduled {beats} time(s) in a {wall * 1000:.0f} ms burst "
        f"({offloaded_rate:.1f}/s), against {blocked_beats} in {blocked_wall * 1000:.0f} ms "
        f"({blocked_rate:.1f}/s) when the same measurement runs on the loop — a ratio of "
        f"{offloaded_rate / blocked_rate:.2f}x against a floor of 2.5x, where deleting the offload "
        "gives 1.0x: the sweep is running on the loop that serves every other turn's stream and "
        "both kubelet probes"
    )
    assert blocked_beats < beats, (
        f"the un-offloaded control was scheduled {blocked_beats} time(s) against the offloaded "
        f"arm's {beats}, so this run cannot tell the two apart and the bar above is vacuous"
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

    A skip rather than a failure because the merge table is a *deployment* artefact: an image bakes
    it and `TIKTOKEN_CACHE_DIR` names it, and a checkout with neither is exactly the fallback the
    tests below this one cover. Loud, because a check that quietly shrinks is worse than one that
    says what it did not look at.
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
    """The system message is a *block list*, and counting only strings was a silent no-op.

    This is the assertion the first implementation needed and did not have. `_message_tokens`
    tested `isinstance(content, str)` and fell back otherwise — and the prompt `create_agent` hands
    a model arrives as `[{"type": "text", "text": ...}]`, so the exact path was never taken and the
    measured prefix was byte-for-byte the estimator's. A fallback that is never taken and one that
    is always taken produce the same number, so the property to assert is that the count *moved*,
    and moved in the direction chars/4 is wrong in: it over-charges prose. Measured 2026-09-16 on
    the observed `default` prompt, 7,755 estimated against 6,574 billed by `o200k_base` — 18%.
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
    """And it barely moves, which is the finding rather than a weak assertion.

    Measured 2026-09-16 over the `default` profile's 98 bound tools: 61,123 against 61,093
    estimated — chars/4 is within 0.05% on JSON schemas. So this asserts the two agree *closely*
    rather than that one is smaller, because the direction is not the property and pinning a
    direction here would fail on a schema whose punctuation happened to tokenise the other way.
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
    """A tool result is text a server wrote, and `Encoding.encode` refuses some of it.

    `encode` raises `ValueError` on `<|endoftext|>` and the other special-token spellings; a
    connector could return one in a document, a SMILES comment or an error string. A counter that
    raises on its own input would fail the turn it exists to make cheaper, so this path uses
    `encode_ordinary`, which has no such refusal.
    """
    _encoding_or_skip()

    tokens = _message_tokens(SystemMessage(content="<|endoftext|> and <|fim_prefix|> in a result"))

    assert tokens > 0


def test_an_unpriceable_block_falls_back_to_the_estimator_whole(_clean_encoding: None) -> None:
    """An image is 85 tokens to the estimator and nothing at all to an encoding.

    So a message carrying one is counted by the estimator entirely rather than half each way,
    which is what `_text_tokens` returning `None` buys: the two counters are never mixed *inside*
    one message.
    """
    _encoding_or_skip()
    picture = SystemMessage(
        content=[{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "x"}}]
    )

    assert _message_tokens(picture) == int(count_tokens_approximately([picture]))


def test_no_baked_cache_means_the_estimator_and_no_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, _clean_encoding: None
) -> None:
    """Production is air-gapped, so the fallback must not be "try the network and recover".

    `tiktoken.get_encoding` fetches its merge table over HTTPS on a miss. Measured here with an
    empty cache directory and no network namespace, that is a `requests.exceptions.ProxyError` —
    an `OSError` — after 0.03 s where the proxy refuses at once; on a network that *drops* the
    packet instead it is a connect timeout, on the thread measuring the prefix. So the question
    `_baked_cache_dir` asks is whether a table was baked at all, and this test is what proves the
    common misconfiguration never dials: every socket constructor and every name lookup fails the
    test if it is reached.
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
    """A name that cannot be resolved degrades to the estimator instead of failing the turn.

    The residual `_baked_cache_dir` cannot close — a populated cache that does not hold the
    *configured* encoding — is this one, and what it must cost is one swallowed attempt per process
    and a WARNING naming the encoding. Driven through a resolution that raises rather than by
    unsetting the cache, so it covers the arm the directory check lets through.
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
    """An upstream shape, pinned: `_baked_cache_dir` transcribes `read_file_cached`'s resolution.

    That function takes a blob path rather than answering "where would you look", so the steps are
    copied into this module. A bump that renames or reorders them would leave `_baked_cache_dir`
    pointing at a directory nothing bakes into, and the only symptom would be a budget quietly
    counting with chars/4 again.

    **Two of the five strings below are the ones this test used to be missing**, and their absence
    is the whole of the defect the test beside it now drives: upstream decides on *presence*
    (`"TIKTOKEN_CACHE_DIR" in os.environ`) and treats an empty value as *caching disabled — fetch
    every time* (`cache_dir == ""`), where this module asked `os.environ.get(...) or ...` and so
    read an empty value as "not set". Pinning the three directory names could not see that,
    because the three names were never the part that was wrong.
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
    """Every arm of the resolution, driven — because the string pin above agreed with a dial.

    `_baked_cache_dir` is the whole air-gap precondition: `_resolve_encoding` calls `tiktoken` only
    where this says a table is baked, so a `Path` returned here is this module saying "loading is
    safe, nothing will be fetched". The assertion that used to stand behind that claim read three
    string literals out of upstream's source, which is evidence about upstream and none at all
    about this function. Measured with `TIKTOKEN_CACHE_DIR=""` and a populated
    `/tmp/data-gym-cache`, with that assertion green: this returned `/tmp/data-gym-cache`,
    `tiktoken` ignored it exactly as upstream documents, and the resolve dialled `127.0.0.1` — the
    guard was the reason the fetch happened.

    So the arms are driven instead. The empty-string one is the regression, and it is driven
    through `_encoding()` as well as through the return value, because what the return value is
    *for* is deciding whether a socket is opened.

    **The egress guard is not what asserts that here, and the reason is worth recording.**
    `core/netguard.py` is armed in this process, and it did not see the dial above: a proxy
    variable moved the destination to loopback, which `_check` exempts by construction and must
    keep exempting. So `_refused` stayed 0 through the defect it exists to catch, and the sentinel
    below — which fails on *any* host, loopback included — is the assertion that holds.
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
    """The one declaration that decides whether a shipped pod ever reaches the network.

    Two files name this encoding and nothing joined them: `deploy/Containerfile` bakes a merge
    table under `TIKTOKEN_CACHE_DIR` as a literal, and `llm_token_encoding` is what
    `_resolve_encoding` then asks for. They agreeing is not a tidiness property — it is the
    residual `_baked_cache_dir` cannot close, because a *populated* cache that does not hold the
    configured encoding is precisely the case where this module says "safe to load" and `tiktoken`
    fetches. On a dropping network that fetch has no timeout (`tiktoken.load.read_file` calls
    `requests.get` with none), so changing the config default alone would put every pod on that
    path at its first model call, with the only symptom a slow one.

    The cache *directory* is asserted for the same reason and in the same breath: a bake into one
    path and an `ENV` naming another leaves the image with a table no runtime reads, which is the
    same fetch by a different route.
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
