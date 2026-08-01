"""The per-turn runner's answer-verification wiring (plan F10-B2), driven with a fake agent.

Proves the runner stamps the verifier's confidence + unsupported claims on the final `AnswerEvent`
when verification is on, emits today's plain answer when it is off, and never lets a verifier
failure sink the turn. The verifier is faked here (it has its own offline tests) so no model runs.
"""

import asyncio
from typing import Any

import pytest
from agent_framework import AgentSession

import chemclaw.api.runner as runner
from chemclaw.agent.harness_todo import complete_awaiting_job, mark_awaiting_job
from chemclaw.agent.loop_cap import observe_loop_cap
from chemclaw.agent.turn_signals import record_job_started
from chemclaw.agent.verifier import ClaimCheck, VerificationResult
from chemclaw.api.events import (
    AnswerEvent,
    ErrorEvent,
    Event,
    JobStartedEvent,
    PlanEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from chemclaw.core.config import settings


class _Update:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.contents: list[object] = []
        self.user_input_requests: list[object] = []


class _FakeAgent:
    """Yields a two-token answer; no MCP tools to open."""

    mcp_tools: list[object] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        async def _gen() -> Any:
            yield _Update(text="Yield was 90% ")
            yield _Update(text="[[reaction-a]].")

        return _gen()


def _run_turn(message: str = "q") -> list[Any]:
    async def _collect() -> list[Any]:
        session = AgentSession(session_id="s-1")
        return [event async for event in runner.run_turn(_FakeAgent(), session, message)]

    return asyncio.run(_collect())


def _answer(events: list[Any]) -> AnswerEvent:
    return next(e for e in events if isinstance(e, AnswerEvent))


def test_answer_is_unscored_when_verification_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier off (default): the final answer carries no confidence — today's behavior exactly."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", False)
    answer = _answer(_run_turn())
    assert answer.text == "Yield was 90% [[reaction-a]]."
    assert answer.confidence is None and answer.unsupported_claims == []
    assert answer.review_required is False  # unscored answers are never flagged for review


def test_low_confidence_answer_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: a sub-threshold verdict stamps confidence, unsupported claims, review flag."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    async def _fake_verify(answer: str, **_: Any) -> VerificationResult:
        return VerificationResult(
            claims=[ClaimCheck(text="Yield was 90%", supported=False)], confidence=0.2
        )

    monkeypatch.setattr(runner, "verify_turn_answer", _fake_verify)
    answer = _answer(_run_turn())
    assert answer.confidence == 0.2
    assert answer.unsupported_claims == ["Yield was 90%"]
    assert answer.review_required is True  # 0.2 < 0.7 threshold


def test_high_confidence_answer_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: a verdict at/above the threshold is scored but not routed to review."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    async def _fake_verify(answer: str, **_: Any) -> VerificationResult:
        return VerificationResult(claims=[], confidence=1.0)

    monkeypatch.setattr(runner, "verify_turn_answer", _fake_verify)
    answer = _answer(_run_turn())
    assert answer.confidence == 1.0
    assert answer.review_required is False  # 1.0 >= 0.7 threshold


def test_confidence_exactly_at_threshold_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: confidence == threshold is acceptable (strictly-less rule), so not flagged."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    async def _fake_verify(answer: str, **_: Any) -> VerificationResult:
        return VerificationResult(claims=[], confidence=0.7)

    monkeypatch.setattr(runner, "verify_turn_answer", _fake_verify)
    answer = _answer(_run_turn())
    assert answer.confidence == 0.7
    assert answer.review_required is False  # meeting the threshold is acceptable, not sub-threshold


class _JobLaunchingAgent:
    """Announces a launched job mid-stream, as a durable launcher does from inside a tool call."""

    mcp_tools: list[object] = []

    def __init__(self, *job_ids: str, announce_on_last_update: bool = False) -> None:
        self._job_ids = job_ids
        self._on_last = announce_on_last_update

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        async def _gen() -> Any:
            if not self._on_last:
                for job_id in self._job_ids:
                    record_job_started(job_id, "report")
            yield _Update(text="submitting. ")
            if self._on_last:
                for job_id in self._job_ids:
                    record_job_started(job_id, "report")
            yield _Update(text="done.")

        return _gen()


def _events(agent: Any) -> list[Any]:
    async def _collect() -> list[Any]:
        session = AgentSession(session_id="s-jobs")
        return [event async for event in runner.run_turn(agent, session, "run it")]

    return asyncio.run(_collect())


def test_launched_job_is_announced_to_the_streaming_turn() -> None:
    """A job launched by a tool surfaces as a JobStartedEvent — not silence until push-back."""
    events = _events(_JobLaunchingAgent("qm-abc"))
    started = [e for e in events if isinstance(e, JobStartedEvent)]
    assert [e.job_id for e in started] == ["qm-abc"]
    # It reaches the client before the turn's answer, which is the entire point.
    assert events.index(started[0]) < events.index(_answer(events))


def test_a_job_launched_in_the_final_update_is_still_announced() -> None:
    """The post-stream drain catches a launch with no later iteration to carry it."""
    events = _events(_JobLaunchingAgent("qm-late", announce_on_last_update=True))
    assert [e.job_id for e in events if isinstance(e, JobStartedEvent)] == ["qm-late"]


def test_each_job_is_announced_exactly_once() -> None:
    """Draining clears the sink, so two updates cannot re-announce the same job."""
    events = _events(_JobLaunchingAgent("qm-1", "qm-2"))
    assert [e.job_id for e in events if isinstance(e, JobStartedEvent)] == ["qm-1", "qm-2"]


def test_jobs_do_not_leak_between_turns() -> None:
    """The sink is per-turn: an undrained announcement cannot surface in someone else's stream."""
    _events(_JobLaunchingAgent("qm-first"))
    events = _events(_JobLaunchingAgent("qm-second"))
    assert [e.job_id for e in events if isinstance(e, JobStartedEvent)] == ["qm-second"]


def test_classic_agent_emits_no_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the harness there is no todo state, so no (empty, misleading) plan is sent."""
    monkeypatch.setattr(settings, "harness_enabled", False)
    assert [e for e in _run_turn() if isinstance(e, PlanEvent)] == []


def test_plan_is_emitted_and_only_when_it_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness's todo list is streamed as a checklist, once per distinct state.

    Re-sending an unchanged plan on every update would flood the transcript with duplicates.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)

    class _PlanningAgent:
        mcp_tools: list[object] = []

        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> Any:
            async def _gen() -> Any:
                await mark_awaiting_job(session, "qm-1", title="Await QM job qm-1")
                yield _Update(text="a")
                yield _Update(text="b")  # same plan: must not re-emit
                await complete_awaiting_job(session, "qm-1", reason="finished")
                yield _Update(text="c")

            return _gen()

    plans = [e for e in _events(_PlanningAgent()) if isinstance(e, PlanEvent)]
    assert [p.todos for p in plans] == [
        ["[ ] Await QM job qm-1"],
        ["[x] Await QM job qm-1"],
    ]


class _CappedLoopAgent:
    """An agent whose loop still wanted another iteration when it stopped — a capped turn.

    Drives the *real* `observe_loop_cap` wrapper, called the way MAF's loop middleware calls a
    predicate, rather than poking the contextvar: what the runner then reads is what a genuinely
    capped loop leaves behind. That a real MAF loop leaves it is pinned in
    `tests/test_harness_execution.py`; this is the front-door half — the turn says so.
    """

    mcp_tools: list[object] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self, message: str, *, stream: bool, session: AgentSession, **_run_options: Any
    ) -> Any:
        async def _gen() -> Any:
            await observe_loop_cap(lambda **_kwargs: True)(session=session, agent=None)
            yield _Update(text="still working on it")

        return _gen()


def test_a_capped_turn_reports_the_runaway_guard_before_its_partial_answer() -> None:
    """The cap stops being silent: one `loop_cap_reached` error, and the answer still goes out.

    Both halves matter. Without the event a capped turn is indistinguishable from a finished one —
    the silence that forced `runaway_rate` onto a residue proxy and left production with nothing to
    alert on. Without the answer the turn would lose the work the capped iterations did do.
    """
    events = _events(_CappedLoopAgent())
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert [e.code for e in errors] == ["loop_cap_reached"]
    assert errors[0].retryable is False
    assert str(settings.harness_max_loop_iterations) in errors[0].message
    # Before the answer, so a surface can mark it partial as it lands rather than retroactively.
    assert events.index(errors[0]) < events.index(_answer(events))
    assert _answer(events).text == "still working on it"


def test_an_ordinary_turn_does_not_claim_the_cap_fired() -> None:
    """A signal that is always on is worth nothing; the common path must stay silent."""
    assert [e for e in _run_turn() if isinstance(e, ErrorEvent)] == []


def test_unreadable_plan_does_not_sink_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed todo state costs the plan view, never the answer — it is only a view."""
    monkeypatch.setattr(settings, "harness_enabled", True)

    async def _boom(session: AgentSession) -> list[str]:
        raise ValueError("corrupt todo state")

    monkeypatch.setattr(runner, "todo_titles", _boom)
    events = _run_turn()
    assert [e for e in events if isinstance(e, PlanEvent)] == []
    assert _answer(events).text == "Yield was 90% [[reaction-a]]."


def test_verifier_failure_degrades_to_plain_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on but raising: the turn still returns its answer, unscored — never a sunk turn."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)

    async def _boom(answer: str, **_: Any) -> VerificationResult:
        raise RuntimeError("verifier down")

    monkeypatch.setattr(runner, "verify_turn_answer", _boom)
    answer = _answer(_run_turn())
    assert answer.text == "Yield was 90% [[reaction-a]]."
    assert answer.confidence is None and answer.unsupported_claims == []


class _CallContent:
    """One streamed function-call content, in the shape the provider actually emits.

    The name arrives once, on a content whose `arguments` is still an empty dict; the argument
    JSON then streams as text fragments on contents that carry only the `call_id`. Reproduced
    here from a live capture rather than invented, because the bug being guarded against was
    believing a different shape (D-138).
    """

    def __init__(
        self,
        *,
        name: str = "",
        call_id: str = "",
        arguments: object = None,
        result: object = None,
    ) -> None:
        self.name = name
        self.call_id = call_id
        self.arguments = arguments
        # A result content carries no `arguments` field at all, which is how the trace tells the
        # two apart; `result` is what it reports once it has.
        if result is not None:
            self.result = result


def _update(*contents: object) -> _Update:
    update = _Update()
    update.contents = list(contents)
    return update


def _one_call(events: list[Event]) -> ToolCallEvent:
    """The single `tool_call` the trace produced — asserting the kind, not just the count."""
    (event,) = events
    assert isinstance(event, ToolCallEvent), f"expected a tool_call, got {event.type}"
    return event


def _one_result(events: list[Event]) -> ToolResultEvent:
    """The single `tool_result` the trace produced."""
    (event,) = events
    assert isinstance(event, ToolResultEvent), f"expected a tool_result, got {event.type}"
    return event


def test_a_streamed_tool_call_reports_the_arguments_it_was_called_with() -> None:
    """`ToolCallEvent.arguments` must carry the reassembled JSON, not the empty opening content.

    The call is announced on the update that *closes* the argument JSON, not on a later one —
    see the timing test below for why that difference is the whole of D-159.
    """
    trace = runner._ToolCallTrace()
    assert trace.feed(_update(_CallContent(name="add", call_id="c1", arguments={}))) == []
    # The provider opens the argument stream with an *empty* fragment before the first characters
    # arrive. Reading that as "nothing more is coming" closed the call early and shipped an empty
    # preview to the UI — the second way this defect survived a fix (D-138).
    assert trace.feed(_update(_CallContent(call_id="c1", arguments=""))) == []
    assert trace.feed(_update(_CallContent(call_id="c1", arguments='{"a": 1'))) == []
    event = _one_call(trace.feed(_update(_CallContent(call_id="c1", arguments='7, "b": 25}'))))
    assert event.tool == "add"
    assert event.arguments == '{"a": 17, "b": 25}'
    assert trace.flush() == []


def test_a_call_is_announced_before_its_result_is_seen() -> None:
    """The timing D-159 exists for: the trace must not wait for the tool to come back.

    For a streamed call the next update after the arguments is the one carrying the *result* —
    the provider closes the argument JSON, the framework runs the tool, and only then does
    anything else arrive. So flushing on "an update went by" announced `predict_pka(...)` after
    the twenty seconds were already spent, and a working calculation looked exactly like a hung
    server.

    Asserting on order rather than on wall-clock: the call event must be produced by the update
    that ends the arguments, with the result update producing a *different* event afterwards.
    """
    trace = runner._ToolCallTrace()
    trace.feed(_update(_CallContent(name="predict_pka", call_id="p1", arguments={})))

    issued = _one_call(
        trace.feed(_update(_CallContent(call_id="p1", arguments='{"smiles": "CCO"}')))
    )
    assert issued.tool == "predict_pka"

    # ...and only now, after the tool has actually run, does its result arrive.
    returned = _one_result(trace.feed(_update(_CallContent(call_id="p1", result="pKa 15.9"))))
    assert (returned.tool, returned.preview) == ("predict_pka", "pKa 15.9")


def test_a_result_is_reported_even_though_its_content_carries_no_name() -> None:
    """The result content has only a `call_id`, so the name has to be remembered from the call."""
    trace = runner._ToolCallTrace()
    trace.feed(_update(_CallContent(name="compute_xtb_energy", call_id="x", arguments={})))
    trace.feed(_update(_CallContent(call_id="x", arguments='{"smiles": "CCO"}')))
    result = _one_result(trace.feed(_update(_CallContent(call_id="x", result="-154.5 Hartree"))))
    assert result.tool == "compute_xtb_energy"


def test_an_empty_result_reports_nothing_rather_than_an_empty_value() -> None:
    """A trace that shows a value it does not have is worse than one that shows none."""
    trace = runner._ToolCallTrace()
    trace.feed(_update(_CallContent(name="t", call_id="e", arguments={})))
    trace.feed(_update(_CallContent(call_id="e", arguments="{}")))
    assert trace.feed(_update(_CallContent(call_id="e", result=""))) == []
    assert trace.feed(_update(_CallContent(call_id="e"))) == []


def test_arguments_that_never_parse_still_fall_back_to_the_update_went_by_rule() -> None:
    """A provider that does not stream JSON must still get its call announced (D-159).

    Completeness-by-parse is what buys the earlier timing, but it cannot be the only rule: a
    format it does not recognise would leave the call open forever. The old rule stays underneath
    it, so such a call is announced at the previous, later moment rather than never.
    """
    trace = runner._ToolCallTrace()
    trace.feed(_update(_CallContent(name="odd_tool", call_id="c9", arguments={})))
    assert trace.feed(_update(_CallContent(call_id="c9", arguments="smiles=CCO"))) == []
    event = _one_call(trace.feed(_update(_CallContent(call_id="c9"))))
    assert (event.tool, event.arguments) == ("odd_tool", "smiles=CCO")


def test_a_call_whose_arguments_end_the_stream_is_still_reported() -> None:
    """Nothing follows the last update, so the flush is what keeps the final call from vanishing."""
    trace = runner._ToolCallTrace()
    trace.feed(_update(_CallContent(name="screen_hazards", call_id="c9", arguments={})))
    assert trace.feed(_update(_CallContent(call_id="c9", arguments='{"smiles":'))) == []
    event = _one_call(trace.flush())
    assert (event.tool, event.arguments) == ("screen_hazards", '{"smiles":')


def test_two_interleaved_calls_keep_their_own_arguments() -> None:
    """Parallel tool calls share the stream; the `call_id` is what keeps them apart."""
    trace = runner._ToolCallTrace()
    trace.feed(
        _update(
            _CallContent(name="predict_pka", call_id="a", arguments={}),
            _CallContent(name="predict_logd", call_id="b", arguments={}),
        )
    )
    events = trace.feed(
        _update(
            _CallContent(call_id="a", arguments='{"smiles": "CC(=O)O"}'),
            _CallContent(call_id="b", arguments='{"smiles": "c1ccccc1"}'),
        )
    )
    calls = sorted((e for e in events if isinstance(e, ToolCallEvent)), key=lambda e: e.tool)
    assert [(e.tool, e.arguments) for e in calls] == [
        ("predict_logd", '{"smiles": "c1ccccc1"}'),
        ("predict_pka", '{"smiles": "CC(=O)O"}'),
    ]
    assert trace.flush() == []


def test_a_call_delivered_whole_is_reported_without_waiting_for_the_next_update() -> None:
    """Name plus complete arguments in one content means the call is finished, so emit it now.

    Holding it back until an update went by would push the trace entry behind the text the model
    produces next, which reads as the tool having run after the sentence that describes it.
    """
    trace = runner._ToolCallTrace()
    event = _one_call(
        trace.feed(
            _update(_CallContent(name="find_notes", call_id="z", arguments={"query": "amide"}))
        )
    )
    assert (event.tool, event.arguments) == ("find_notes", '{"query": "amide"}')
    assert trace.flush() == []  # nothing left open, so nothing is emitted twice
