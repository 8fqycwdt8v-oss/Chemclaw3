"""The per-turn runner's answer-verification wiring (plan F10-B2), driven with a fake agent.

Proves the runner stamps the verifier's confidence + unsupported claims on the final `AnswerEvent`
when verification is on, emits today's plain answer when it is off, and never lets a verifier
failure sink the turn. The verifier is faked in those tests (it has its own offline tests) so no
model runs.

It is *not* faked in the grounding tests further down, and that is deliberate: what they prove is
which evidence the runner hands the verifier — the turn's own tool results rather than the graph
on disk — which a fake verifier cannot show. Beside them sit the two other per-turn honesty
checks the same assembly point owns: the ungrounded-parameter scan, and the durable subsystem's
reachability probe that lets the model plan against the surface it will actually get.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_framework import AgentSession, Content, TodoSessionStore

import chemclaw.agent.verifier as verifier
import chemclaw.api.runner as runner
import chemclaw.api.runner_answer as runner_answer
import chemclaw.api.runner_trace as runner_trace
from chemclaw.agent.harness_todo import mark_awaiting_job
from chemclaw.agent.loop_cap import observe_loop_cap
from chemclaw.agent.verifier import ClaimCheck, VerificationResult
from chemclaw.api.events import (
    AnswerEvent,
    CapabilityDegradedEvent,
    ErrorEvent,
    Event,
    JobStartedEvent,
    PlanEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from chemclaw.core.config import settings
from chemclaw.core.turn_signals import record_job_started
from tests.fakes import FakeUpdate, fed
from tests.fakes_turn import Piece, ScriptedTurn


class _FakeAgent(ScriptedTurn):
    """Yields a two-token answer; no MCP tools to open."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        yield "Yield was 90% "
        yield "[[reaction-a]]."


def _run_turn(message: str = "q") -> list[Any]:
    """One turn's events on whichever engine is configured, with no connectors.

    `connectors=[]` is stated rather than defaulted for the reason every other driver in the suite
    states it — six hosts that are not running — and, on the graph engine, because the runner hands
    the list straight to the graph builder and the default is the other engine's representation.
    """
    agent = _FakeAgent()

    async def _collect() -> list[Any]:
        session = AgentSession(session_id="s-1")
        return [
            event
            async for event in runner.run_turn(
                session, message, connectors=[], graph_factory=agent.graph_factory
            )
        ]

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

    async def _fake_verify(answer: str, *_: Any, **__: Any) -> VerificationResult:
        return VerificationResult(
            claims=[ClaimCheck(text="Yield was 90%", supported=False)],
            confidence=0.2,
            verified_by="judge",
        )

    monkeypatch.setattr(runner_answer, "verify_turn_answer", _fake_verify)
    answer = _answer(_run_turn())
    assert answer.confidence == 0.2
    assert answer.unsupported_claims == ["Yield was 90%"]
    assert answer.review_required is True  # 0.2 < 0.7 threshold


def test_high_confidence_answer_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: a verdict at/above the threshold is scored but not routed to review."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    async def _fake_verify(answer: str, *_: Any, **__: Any) -> VerificationResult:
        return VerificationResult(claims=[], confidence=1.0, verified_by="judge")

    monkeypatch.setattr(runner_answer, "verify_turn_answer", _fake_verify)
    answer = _answer(_run_turn())
    assert answer.confidence == 1.0
    assert answer.review_required is False  # 1.0 >= 0.7 threshold


def test_confidence_exactly_at_threshold_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: confidence == threshold is acceptable (strictly-less rule), so not flagged."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    async def _fake_verify(answer: str, *_: Any, **__: Any) -> VerificationResult:
        return VerificationResult(claims=[], confidence=0.7, verified_by="judge")

    monkeypatch.setattr(runner_answer, "verify_turn_answer", _fake_verify)
    answer = _answer(_run_turn())
    assert answer.confidence == 0.7
    assert answer.review_required is False  # meeting the threshold is acceptable, not sub-threshold


class _JobLaunchingAgent(ScriptedTurn):
    """Announces a launched job mid-stream, as a durable launcher does from inside a tool call."""

    def __init__(self, *job_ids: str, announce_on_last_update: bool = False) -> None:
        self._job_ids = job_ids
        self._on_last = announce_on_last_update

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        if not self._on_last:
            for job_id in self._job_ids:
                record_job_started(job_id, "report")
        yield "submitting. "
        if self._on_last:
            for job_id in self._job_ids:
                record_job_started(job_id, "report")
        yield "done."


def _events(agent: ScriptedTurn, session: AgentSession | None = None) -> list[Any]:
    """One turn's events for `agent`, on whichever engine is configured (see `_run_turn`).

    `session` is injectable because a fake that plans has to write into the *same* session object
    the runner reads its todo list from — building one here and another in the test would leave
    the plan somewhere nothing looks.
    """
    turn_session = session if session is not None else AgentSession(session_id="s-jobs")

    async def _collect() -> list[Any]:
        return [
            event
            async for event in runner.run_turn(
                turn_session, "run it", connectors=[], graph_factory=agent.graph_factory
            )
        ]

    return asyncio.run(_collect())


class _ApprovalRequestingAgent(ScriptedTurn):
    """Streams a real MAF `function_approval_request` mid-turn, as a gated tool would.

    Stays a MAF-shaped fake — the content it emits is MAF's own type and the branch that reads it
    is MAF's — so `stream` yields updates rather than text and `run` passes them through. The class
    is a `ScriptedTurn` only so the shared driver can ask it for a `graph_factory`; the test that
    uses it is `maf_engine_only`, and on the graph engine that factory is never called.
    """

    async def stream(self, message: str) -> AsyncIterator[Any]:
        """The MAF-shaped stream: text, then MAF's own approval-request content, then text."""
        yield FakeUpdate(text="I need to run a DFT job. ")
        update = FakeUpdate()
        update.contents = [
            Content.from_function_approval_request(
                id="appr-1",
                function_call=Content.from_function_call(
                    call_id="c1", name="submit_dft_job", arguments={"smiles": "CCO"}
                ),
            )
        ]
        yield update
        yield FakeUpdate(text="waiting.")

    def run(  # noqa: D102 - see the class docstring
        self, message: str, *, stream: bool, session: Any, **_run_options: Any
    ) -> AsyncIterator[Any]:
        return self.stream(message)


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


class _PlanClearingAgent(ScriptedTurn):
    """Plans, launches a job, and clears its todo list in the resume — the topic-change shape.

    MAF's own todo instructions tell the model to clear the list when the chemist changes their
    mind, so an emptied plan is ordinary behaviour rather than a corrupted state. What made it
    interesting is *where* the emptying lands: after the mid-turn resume, at the runner's second
    `PlanEvent` site.
    """

    def __init__(self, session: AgentSession) -> None:
        """Plan into `session`'s todo store, then clear it on the continuation pass."""
        self.calls = 0
        self._session = session

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        self.calls += 1
        if self.calls == 1:
            await mark_awaiting_job(self._session, "qm-9", title="Await QM job qm-9")
            record_job_started("qm-9", "qm")
            yield "running it. "
        else:
            await TodoSessionStore().save_state(self._session, [], next_id=1, source_id="todo")
            yield "never mind, here is the answer."


class _CappedLoopAgent(ScriptedTurn):
    """An agent whose loop still wanted another iteration when it stopped — a capped turn.

    Drives the *real* `observe_loop_cap` wrapper, called the way MAF's loop middleware calls a
    predicate, rather than poking the contextvar: what the runner then reads is what a genuinely
    capped loop leaves behind. That a real MAF loop leaves it is pinned in
    `tests/test_harness_execution.py`; this is the front-door half — the turn says so.

    The wrapper is engine-neutral by construction (`chemclaw.agent.loop_cap` is one of the shared
    decisions M3 extracted), so the same call stands in for the graph engine's `lg_loop_cap`
    reaching the same watch — which is what `tests/test_langgraph_stream.py` pins from the other
    side.
    """

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        await observe_loop_cap(lambda **_kwargs: True)(session=None, agent=None)
        yield "still working on it"


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


def test_verifier_failure_degrades_to_plain_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on but raising: the turn still returns its answer, unscored — never a sunk turn."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)

    async def _boom(answer: str, *_: Any, **__: Any) -> VerificationResult:
        raise RuntimeError("verifier down")

    monkeypatch.setattr(runner_answer, "verify_turn_answer", _boom)
    answer = _answer(_run_turn())
    assert answer.text == "Yield was 90% [[reaction-a]]."
    assert answer.confidence is None
    # A check that was configured on and *crashed* must not read as one that ran and passed.
    # It used to leave `review_required` False and `unsupported_claims` empty — byte-for-byte
    # the event a clean verdict produces — so a verification outage was invisible to the
    # surface and to the reviewer. The turn is still returned, which is what "never a sunk
    # turn" meant and still means.
    assert answer.review_required is True
    assert answer.unsupported_claims == ["verification did not run"]


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


def _update(*contents: object) -> FakeUpdate:
    update = FakeUpdate()
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
    trace = runner_trace.ToolCallTrace()
    assert fed(trace, _update(_CallContent(name="add", call_id="c1", arguments={}))) == []
    # The provider opens the argument stream with an *empty* fragment before the first characters
    # arrive. Reading that as "nothing more is coming" closed the call early and shipped an empty
    # preview to the UI — the second way this defect survived a fix (D-138).
    assert fed(trace, _update(_CallContent(call_id="c1", arguments=""))) == []
    assert fed(trace, _update(_CallContent(call_id="c1", arguments='{"a": 1'))) == []
    event = _one_call(fed(trace, _update(_CallContent(call_id="c1", arguments='7, "b": 25}'))))
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
    trace = runner_trace.ToolCallTrace()
    fed(trace, _update(_CallContent(name="predict_pka", call_id="p1", arguments={})))

    issued = _one_call(
        fed(trace, _update(_CallContent(call_id="p1", arguments='{"smiles": "CCO"}')))
    )
    assert issued.tool == "predict_pka"

    # ...and only now, after the tool has actually run, does its result arrive.
    returned = _one_result(fed(trace, _update(_CallContent(call_id="p1", result="pKa 15.9"))))
    assert (returned.tool, returned.preview) == ("predict_pka", "pKa 15.9")


def test_a_result_is_reported_even_though_its_content_carries_no_name() -> None:
    """The result content has only a `call_id`, so the name has to be remembered from the call."""
    trace = runner_trace.ToolCallTrace()
    fed(trace, _update(_CallContent(name="compute_xtb_energy", call_id="x", arguments={})))
    fed(trace, _update(_CallContent(call_id="x", arguments='{"smiles": "CCO"}')))
    result = _one_result(fed(trace, _update(_CallContent(call_id="x", result="-154.5 Hartree"))))
    assert result.tool == "compute_xtb_energy"


def test_an_empty_result_reports_nothing_rather_than_an_empty_value() -> None:
    """A trace that shows a value it does not have is worse than one that shows none."""
    trace = runner_trace.ToolCallTrace()
    fed(trace, _update(_CallContent(name="t", call_id="e", arguments={})))
    fed(trace, _update(_CallContent(call_id="e", arguments="{}")))
    assert fed(trace, _update(_CallContent(call_id="e", result=""))) == []
    assert fed(trace, _update(_CallContent(call_id="e"))) == []


def test_arguments_that_never_parse_still_fall_back_to_the_update_went_by_rule() -> None:
    """A provider that does not stream JSON must still get its call announced (D-159).

    Completeness-by-parse is what buys the earlier timing, but it cannot be the only rule: a
    format it does not recognise would leave the call open forever. The old rule stays underneath
    it, so such a call is announced at the previous, later moment rather than never.
    """
    trace = runner_trace.ToolCallTrace()
    fed(trace, _update(_CallContent(name="odd_tool", call_id="c9", arguments={})))
    assert fed(trace, _update(_CallContent(call_id="c9", arguments="smiles=CCO"))) == []
    event = _one_call(fed(trace, _update(_CallContent(call_id="c9"))))
    assert (event.tool, event.arguments) == ("odd_tool", "smiles=CCO")


def test_a_call_whose_arguments_end_the_stream_is_still_reported() -> None:
    """Nothing follows the last update, so the flush is what keeps the final call from vanishing."""
    trace = runner_trace.ToolCallTrace()
    fed(trace, _update(_CallContent(name="screen_hazards", call_id="c9", arguments={})))
    assert fed(trace, _update(_CallContent(call_id="c9", arguments='{"smiles":'))) == []
    event = _one_call(trace.flush())
    assert (event.tool, event.arguments) == ("screen_hazards", '{"smiles":')


def test_two_interleaved_calls_keep_their_own_arguments() -> None:
    """Parallel tool calls share the stream; the `call_id` is what keeps them apart."""
    trace = runner_trace.ToolCallTrace()
    fed(
        trace,
        _update(
            _CallContent(name="predict_pka", call_id="a", arguments={}),
            _CallContent(name="predict_logd", call_id="b", arguments={}),
        ),
    )
    events = fed(
        trace,
        _update(
            _CallContent(call_id="a", arguments='{"smiles": "CC(=O)O"}'),
            _CallContent(call_id="b", arguments='{"smiles": "c1ccccc1"}'),
        ),
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
    trace = runner_trace.ToolCallTrace()
    event = _one_call(
        fed(
            trace,
            _update(_CallContent(name="find_notes", call_id="z", arguments={"query": "amide"})),
        )
    )
    assert (event.tool, event.arguments) == ("find_notes", '{"query": "amide"}')
    assert trace.flush() == []  # nothing left open, so nothing is emitted twice


def test_a_result_event_carries_the_values_the_preview_cuts_off() -> None:
    """The trace reads ids *and* figures off the whole result, and only the preview is truncated.

    Built from what `ich_impurity_limit` really returns rather than from a shaped string: the six
    ICH PDEs sit past character 200 of that result, and a live judge with only the preview called
    every one of them invented. `numbers` is what lets a scorer disagree with it.
    """
    from chemclaw.science.safety.ich import impurity_limit

    result = impurity_limit("palladium").model_dump_json(indent=2)
    assert len(result) > settings.agent_audit_max_arg_chars

    trace = runner_trace.ToolCallTrace()
    fed(trace, _update(_CallContent(name="ich_impurity_limit", call_id="i1", arguments={})))
    fed(trace, _update(_CallContent(call_id="i1", arguments='{"substance": "palladium"}')))
    event = _one_result(fed(trace, _update(_CallContent(call_id="i1", result=result))))

    assert event.preview == result[: settings.agent_audit_max_arg_chars]
    assert {100.0, 10.0, 1.0} <= set(event.numbers)
    assert not {"100.0", "10.0"} & set(event.preview.split())


def test_a_result_with_more_values_than_the_wire_allows_is_capped_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bounded field that truncates silently reads as completeness — the defect, one field over.

    The cap is an order of magnitude above the largest real result (49 values, a full
    electronic-properties calculation), so this drives it deliberately. What matters is that the
    drop is announced: a consumer told to trust the list would otherwise be trusting a short one.
    """
    flood = ", ".join(str(n + 0.5) for n in range(settings.stream_max_result_numbers + 50))
    trace = runner_trace.ToolCallTrace()
    fed(trace, _update(_CallContent(name="dump_table", call_id="f1", arguments={})))
    fed(trace, _update(_CallContent(call_id="f1", arguments="{}")))
    with caplog.at_level(logging.WARNING, logger=runner_trace.__name__):
        event = _one_result(fed(trace, _update(_CallContent(call_id="f1", result=flood))))

    assert len(event.numbers) == settings.stream_max_result_numbers
    assert "dump_table" in caplog.text


class _CitingAgent(ScriptedTurn):
    """Answers with a citation, optionally after a tool that returned it.

    `tool_result` is the whole point: it is what makes the citation grounded *in this turn*, which
    is a different question from whether the note exists.

    **`graph_factory` is overridden rather than inherited**, and that is the escape hatch
    `ScriptedTurn` is designed to leave open: the shared double replays *text*, and what this test
    needs is a turn that really calls a tool and really gets a result back, because the grounding
    gate reads `ToolCallTrace.outputs`. On the graph engine a tool result cannot be narrated into
    existence — the tool node has to run — so this builds the graph directly, with one tool that
    returns the scripted result. The tool is deliberately named for a note lookup and not
    `find_notes`: the registry already advertises that name, and two tools sharing one name is a
    graph that would not compile.
    """

    def __init__(self, answer: str, *, tool_result: str | None = None) -> None:
        self._answer = answer
        self._tool_result = tool_result

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """The answer, and only the answer — the tool is each engine's own business.

        A tool result cannot be narrated: on MAF it is a streamed content (see `run`), on the graph
        it is a tool node that ran (see `graph_factory`). Keeping this to the prose is what lets the
        no-tool case — a citation the turn never retrieved — run unchanged on both engines.
        """
        yield self._answer

    def run(
        self, message: str, *, stream: bool, session: Any, **_run_options: Any
    ) -> AsyncIterator[Any]:
        """The MAF face: the call and its result as streamed contents, then the answer."""

        async def _updates() -> AsyncIterator[Any]:
            if self._tool_result is not None:
                yield _update(_CallContent(name="find_notes", call_id="c1", arguments={"q": "x"}))
                yield _update(_CallContent(call_id="c1", result=self._tool_result))
            yield FakeUpdate(text=self._answer)

        return _updates()

    def graph_factory(self, **build_kwargs: Any) -> Any:
        """A real graph whose model calls one result-returning tool, then answers."""
        from langchain_core.tools import tool as make_tool

        from chemclaw.agent.audit import NullAuditSink
        from chemclaw.agent.langgraph_agent import build_langgraph_agent
        from tests.fakes_langgraph import ScriptedChatModel

        result = self._tool_result
        if result is None:
            return super().graph_factory(**build_kwargs)

        @make_tool
        def recall_note(query: str) -> str:
            """Return the note text this turn is supposed to have retrieved."""
            return result

        script: list[Any] = [{"name": "recall_note", "args": {"query": "x"}}, self._answer]
        build_kwargs["connectors"] = [*(build_kwargs.get("connectors") or []), recall_note]
        return build_langgraph_agent(
            ScriptedChatModel(script), audit_sink=NullAuditSink(), **build_kwargs
        )


def _offline_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification on, judge unreachable — so the offline citation gate produces the verdict.

    `build_answer_event` verifies only when `verifier_enabled`, and that same flag is what routes
    `verify_answer` to the LLM judge, so the deterministic gate cannot be reached through the
    runner without a judge that does not answer. Which is the realistic shape anyway: no model
    endpoint is configured in a test process, and the documented behaviour is that an unreachable
    judge degrades to the offline check rather than leaving the answer unscored.
    """
    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    class _Unreachable:
        async def get_response(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("no verifier endpoint in a test process")

    monkeypatch.setattr(verifier, "_default_client", _Unreachable)


def _verified_answer(agent: ScriptedTurn) -> AnswerEvent:
    async def _collect() -> list[Any]:
        session = AgentSession(session_id="s-cite")
        return [
            event
            async for event in runner.run_turn(
                session, "q", connectors=[], graph_factory=agent.graph_factory
            )
        ]

    return _answer(asyncio.run(_collect()))


def test_a_citation_the_turn_never_retrieved_is_unsupported_though_the_note_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verifier scores against what the turn saw, never against what happens to be on disk.

    `compound-thf` is a real note in this repo's graph and the turn calls no tool at all, so the
    naive implementation — re-resolving the answer's citations from `knowledge_path` — gives the
    *wrong* answer here: it certifies a citation the turn never obtained, at confidence 1.0 and
    `review_required=False`. That is exactly the case the gate exists for, and re-retrieval made
    it unfailable, because `known` meant "note ids that exist" rather than "note ids this turn
    saw". `evals.live._score_citations` has scored it the correct way from the start.
    """
    assert (settings.knowledge_path / "compound" / "compound-thf.md").exists(), (
        "the fixture depends on this note being real — a naive implementation must pass it"
    )
    _offline_verification(monkeypatch)
    answer = _verified_answer(_CitingAgent("THF was the solvent [[compound-thf]]."))
    assert answer.confidence == 0.0
    # Two entries, and the second is not noise: this verdict came from the citation gate standing
    # in for an unreachable judge, so the event carries *why* it is flagged as well as what failed.
    # A bare `review_required` beside `confidence=1.0` is a flag a reviewer cannot act on.
    assert answer.unsupported_claims == [
        "THF was the solvent [[compound-thf]].",
        "verified by the citation gate only; the judge did not run",
    ]
    assert answer.review_required is True


def test_the_same_citation_is_supported_when_a_tool_in_the_turn_returned_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the test above: grounding it in a tool result is what makes it pass."""
    _offline_verification(monkeypatch)
    answer = _verified_answer(
        _CitingAgent(
            "THF was the solvent [[compound-thf]].",
            tool_result="find_notes: [[compound-thf]] — tetrahydrofuran, ethereal solvent",
        )
    )
    assert answer.confidence == 1.0
    # `_offline_verification` is "verification on, judge unreachable", so this verdict came
    # from the citation gate standing in for the judge. It scores *resolvability* — do the
    # wikilinks name chunks this turn retrieved — not the *faithfulness* the judge scores,
    # and measured it is the more generous of the two: the same cited-but-contradicted
    # answer scores 1.0/supported degraded against 0.0/unsupported judged. A substitute
    # check cannot clear the gate on behalf of the check that did not run. What this test
    # still proves is the thing it was written for: the citation resolved, confidence 1.0.
    assert answer.verified_by == "citation-gate"
    assert answer.review_required is True


def test_a_tool_result_grounds_the_answer_past_the_uis_preview_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grounding reads the whole tool result; only the wire carries the 200-character preview.

    A `gather_evidence` result is ~20,000 characters over ~40 chunks, so scoring against
    `ToolResultEvent.preview` would call 39 of its 40 citations fabricated. The budget is right
    for the UI trace and wrong for a grounding check, so the two read different things.

    The cited note deliberately does *not* exist on disk, so the tool result is the only thing
    that can ground it: an implementation that re-resolved from the graph, and one that read the
    truncated preview, both call this answer fabricated.
    """
    note_id = "reaction-only-this-turn-saw-it"
    assert not list(settings.knowledge_path.rglob(f"{note_id}.md")), "the note must not exist"
    _offline_verification(monkeypatch)
    buried = "filler chunk. " * 40 + f"[[{note_id}]]"
    assert len(buried) > settings.agent_audit_max_arg_chars
    answer = _verified_answer(
        _CitingAgent(f"The solvent was screened [[{note_id}]].", tool_result=buried)
    )
    assert answer.confidence == 1.0
    # `_offline_verification` is "verification on, judge unreachable", so this verdict came
    # from the citation gate standing in for the judge. It scores *resolvability* — do the
    # wikilinks name chunks this turn retrieved — not the *faithfulness* the judge scores,
    # and measured it is the more generous of the two: the same cited-but-contradicted
    # answer scores 1.0/supported degraded against 0.0/unsupported judged. A substitute
    # check cannot clear the gate on behalf of the check that did not run. What this test
    # still proves is the thing it was written for: the citation resolved, confidence 1.0.
    assert answer.verified_by == "citation-gate"
    assert answer.review_required is True


_METHOD_ANSWER = "Use a Kinetex C18 column at 1.0 mL/min with detection at 254 nm."


def test_an_ungrounded_method_parameter_marks_the_answer_for_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate the live run asked for: a branded method no tool in the turn produced is flagged.

    Verification stays off here, so `review_required` can only have come from the shape scan — the
    two checks are independent knobs and either one may raise the flag. The matched text rides on
    `unsupported_claims`, because "this answer wants a look" without saying at what is not
    something a reviewer can act on.
    """
    monkeypatch.setattr(settings, "verifier_enabled", False)
    monkeypatch.setattr(settings, "answer_shape_gate_enabled", True)
    answer = _verified_answer(_CitingAgent(_METHOD_ANSWER))
    assert answer.review_required is True
    assert answer.unsupported_claims == [
        "flow rate: 1.0 mL/min",
        "wavelength: 254 nm",
        "column brand: Kinetex",
    ]
    assert answer.confidence is None  # nothing was *scored*; the scan is not a measurement


def test_the_shape_gate_is_off_unless_the_deployment_asks_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-off, because a heuristic that fires on a legitimate answer is worse than none."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    assert settings.answer_shape_gate_enabled is False, "the gate must be off unless asked for"
    answer = _verified_answer(_CitingAgent(_METHOD_ANSWER))
    assert answer.review_required is False
    assert answer.unsupported_claims == []


class _Broker:
    """A stand-in Temporal client whose health RPC answers however the test needs."""

    def __init__(self, healthy: bool) -> None:
        self.service_client = self
        self._healthy = healthy
        self.probes = 0

    async def check_health(self, *, retry: bool = True) -> bool:
        self.probes += 1
        assert retry is False, "a per-turn probe must not enter the SDK's retry loop"
        return self._healthy


def _degraded(events: list[Any]) -> list[str]:
    return [name for e in events if isinstance(e, CapabilityDegradedEvent) for name in e.connectors]


def _turn_events(**overrides: Any) -> list[Any]:
    agent = _FakeAgent()

    async def _collect() -> list[Any]:
        session = AgentSession(session_id="s-probe")
        return [
            event
            async for event in runner.run_turn(
                session, "q", connectors=[], graph_factory=agent.graph_factory, **overrides
            )
        ]

    return asyncio.run(_collect())


def test_a_durable_outage_is_announced_before_the_first_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model must meet the outage in its context, not in a tool failure halfway through.

    Every long or expensive capability is a workflow, so an unreachable broker removes all of them
    at once — and in the 190-probe live run 0 of 7 durable launchers ran while the model read the
    failures as its own bad input and re-asked for parameters it already had. Announced first, in
    the same event connectors use, because a surface does the same thing with either name.
    """

    async def _unreachable() -> Any:
        raise RuntimeError("Failed client connect: tcp connect error")

    monkeypatch.setattr(runner, "connect", _unreachable)
    events = _turn_events()
    assert _degraded(events) == [runner._DURABLE_SUBSYSTEM]
    kinds = [e.type for e in events]
    assert kinds.index("capability_degraded") < kinds.index("token")


def test_a_reachable_broker_announces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: a healthy turn is byte-for-byte the turn it was, with no announcement."""
    broker = _Broker(healthy=True)

    async def _reachable() -> Any:
        return broker

    monkeypatch.setattr(runner, "connect", _reachable)
    events = _turn_events()
    assert _degraded(events) == []
    assert broker.probes == 1, "probed once per turn, not once per update"


def test_a_broker_that_answers_the_health_rpc_falsely_is_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`connect` succeeding is not reachability: the client is cached for the process's life.

    Once one turn has connected, every later turn gets that cached handle back instantly — so a
    broker that has since died would look reachable forever if the probe stopped at `connect`.
    The health RPC is what actually goes to the wire each turn.
    """

    async def _reachable() -> Any:
        return _Broker(healthy=False)

    monkeypatch.setattr(runner, "connect", _reachable)
    assert _degraded(_turn_events()) == [runner._DURABLE_SUBSYSTEM]


def test_a_hanging_broker_does_not_hold_up_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounded by the same probe budget the connector sweep uses, so one dead host costs one turn.

    Unbounded, a broker that accepts the connection and never answers would stall every turn's
    first token for as long as it stayed silent — which is worse than the outage being probed for.
    """

    async def _hangs() -> Any:
        await asyncio.sleep(30)
        raise AssertionError("the probe was not bounded")  # pragma: no cover

    monkeypatch.setattr(runner, "connect", _hangs)
    monkeypatch.setattr(settings, "connector_health_timeout_seconds", 0.05)
    started = time.perf_counter()
    events = _turn_events()
    elapsed = time.perf_counter() - started
    assert _degraded(events) == [runner._DURABLE_SUBSYSTEM]
    assert elapsed < 5, f"the probe was not bounded: the turn took {elapsed:.1f}s"


class _SilentAgent(ScriptedTurn):
    """Runs, yields no text at all, and ends the turn — the shape a live turn actually took."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        yield ""


def test_a_turn_that_writes_nothing_says_so_instead_of_answering_emptily() -> None:
    """The silent death, made loud.

    Measured on 2026-08-04 with the harness *off*: a turn made 29 tool calls over 197 s, never
    reached the capability the question needed, and ended with an empty `AnswerEvent`. No error, no
    tokens — nothing a user could read, retry or report. The existing guard covers only the harness
    loop cap (`loop_cap_reached`), so this path had none at all, and `evals.live` scores exactly
    this shape as `failed_loudly=False` because it is the worst outcome a turn can have: a user
    cannot retry what never said it went wrong.

    The assertion is on the `ErrorEvent`, not on the answer text: the system genuinely had nothing
    to say, and inventing prose to fill the gap would be the other, worse failure.
    """
    events = _events(_SilentAgent())

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors, "a turn that produced no text emitted no error — the silent death itself"
    assert errors[0].code == "empty_answer"
    assert errors[0].retryable is True, "a narrower question can succeed; this is not terminal"


def test_a_named_fragment_stream_reassembles_into_one_call() -> None:
    """The OpenAI Responses shape: every fragment carries the name *and* a partial document.

    Measured live on 2026-08-04, driving the `openai_compatible` seam with a mock model: an
    eight-fragment call produced **ten `tool_call` events against one `tool_result`**, the first
    announcing `{"t` as though it were the whole argument document. `feed` had branched on
    `name and arguments`, on the stated assumption that a streamed call's named content always
    carries empty arguments and its fragments never carry a name — true of Anthropic, false of the
    Responses API, and the `openai_compatible` path had never been exercised live.

    A name says nothing about whether the arguments are finished. Only the arguments do.
    """
    trace = runner_trace.ToolCallTrace()
    events: list[Event] = []
    for fragment in ('{"a": 1', "7, ", '"b": 25}'):
        events.extend(
            fed(trace, _update(_CallContent(name="add", call_id="c1", arguments=fragment)))
        )
    events.extend(trace.flush())

    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(calls) == 1, f"one call must announce once, got {[c.arguments for c in calls]}"
    assert calls[0].tool == "add"
    assert json.loads(calls[0].arguments) == {"a": 17, "b": 25}


def test_a_whole_call_delivered_as_a_structured_object_still_announces_immediately() -> None:
    """The other side of that fix: a Mapping is a finished call and must not wait for more.

    Kept explicit because the fix narrowed the whole-call branch to the argument *type*; if that
    narrowing had gone one step further and dropped the branch entirely, a non-streamed provider's
    call would sit unannounced until the next update went by.
    """
    trace = runner_trace.ToolCallTrace()
    events = fed(trace, _update(_CallContent(name="add", call_id="c9", arguments={"a": 1})))

    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(calls) == 1
    assert json.loads(calls[0].arguments) == {"a": 1}


class _ResultOnlyContent:
    """A function-result content: it carries a `call_id` and a `result`, and no `arguments` at all.

    Distinct from `_CallContent`, which always defines `arguments` (as None when absent). The
    difference is the point: `ToolCallTrace.feed` admits a content when it has *either* an
    `arguments` attribute *or* a `call_id`, and a result content is the shape that has only the
    second one. A class that defined both would make the guard untestable.
    """

    def __init__(self, *, call_id: str, result: str) -> None:
        self.call_id = call_id
        self.result = result


def test_a_content_carrying_only_a_call_id_is_still_read() -> None:
    """The duck-typing guard is `or`, not `and`, and a result content is why.

    Found by mutation testing (2026-08-04): flipping
    `hasattr(content, "arguments") or hasattr(content, "call_id")` to `and` survived every test of
    this module. Under `and`, a function-result content — which has a `call_id` and no `arguments`
    attribute — is skipped outright: no `tool_result` event, nothing appended to `outputs`, and
    therefore no evidence for the answer verifier to score the answer against. Every citation in
    that turn would read as fabricated.

    That is the same species as the defect this module shipped on 2026-08-04, where a guard was
    written against a stream shape somebody believed rather than one somebody had captured. So the
    two shapes are asserted separately: a call announced from `arguments` alone, and a result read
    from `call_id` alone.
    """
    trace = runner_trace.ToolCallTrace()
    update = FakeUpdate()
    update.contents = [_CallContent(name="find_notes", call_id="c1", arguments={"text": "x"})]
    calls = fed(trace, update)
    assert [event.tool for event in calls if isinstance(event, ToolCallEvent)] == ["find_notes"]

    result_update = FakeUpdate()
    result_update.contents = [_ResultOnlyContent(call_id="c1", result='[{"id": "note-a"}]')]
    events = fed(trace, result_update)
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert [event.tool for event in results] == ["find_notes"], (
        "a result content carries no name — it is matched back to its call by id, and dropping it "
        "would leave the turn with a call nothing ever answered"
    )
    assert trace.outputs == ['[{"id": "note-a"}]'], (
        "the full result text is what the answer verifier scores against; without it every "
        "citation in the turn reads as fabricated"
    )
