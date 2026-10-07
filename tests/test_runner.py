"""The per-turn runner's answer-verification wiring, driven with a fake agent.

The runner stamps the verifier's confidence and unsupported claims on the final `AnswerEvent`
when verification is on, emits a plain answer when off, and never lets a verifier failure sink
the turn. The grounding tests use the real verifier, because they prove which evidence the
runner hands it: the turn's own tool results, not the graph on disk. The ungrounded-parameter
scan and the durable-subsystem probe are covered here too.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import chemclaw.agent.verifier as verifier
import chemclaw.agent.verifier as verifier_module
import chemclaw.api.runner as runner
import chemclaw.api.runner_trace as runner_trace
from chemclaw.agent.loop_cap import record_loop_cap
from chemclaw.agent.plan_approval_store import plan_approval_store
from chemclaw.agent.plan_gate import PLAN_APPROVAL_PROMPT, approval_stands, plan_identity
from chemclaw.agent.session import TurnSession
from chemclaw.agent.spend_cap import record_spend_cap
from chemclaw.agent.verifier import ClaimCheck, VerificationResult
from chemclaw.api.events import (
    AnswerEvent,
    ApprovalRequestEvent,
    CapabilityDegradedEvent,
    ErrorEvent,
    JobStartedEvent,
    PlanEvent,
)
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.turn_signals import record_job_started
from tests.fakes_turn import Piece, ScriptedTurn


class _FakeAgent(ScriptedTurn):
    """Yields a two-token answer; no MCP tools to open."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield "Yield was 90% "
        yield "[[reaction-a]]."


def _run_turn(message: str = "q") -> list[Any]:
    """One turn's events on whichever engine is configured, with no connectors.

    `connectors=[]` is explicit because the runner hands the list straight to the graph builder.
    """
    agent = _FakeAgent()

    async def _collect() -> list[Any]:
        session = TurnSession(session_id="s-1")
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

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _fake_verify)
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

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _fake_verify)
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

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _fake_verify)
    answer = _answer(_run_turn())
    assert answer.confidence == 0.7
    assert answer.review_required is False  # meeting the threshold is acceptable, not sub-threshold


class _JobLaunchingAgent(ScriptedTurn):
    """Announces a launched job mid-stream, as a durable launcher does from inside a tool call."""

    def __init__(self, *job_ids: str, announce_on_last_update: bool = False) -> None:
        self._job_ids = job_ids
        self._on_last = announce_on_last_update

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        if not self._on_last:
            for job_id in self._job_ids:
                record_job_started(job_id, "report")
        yield "submitting. "
        if self._on_last:
            for job_id in self._job_ids:
                record_job_started(job_id, "report")
        yield "done."


def _events(agent: ScriptedTurn, session: TurnSession | None = None) -> list[Any]:
    """One turn's events for `agent`, on whichever engine is configured (see `_run_turn`).

    `session` is injectable so a planning fake writes into the session the runner reads.
    """
    turn_session = session if session is not None else TurnSession(session_id="s-jobs")

    async def _collect() -> list[Any]:
        return [
            event
            async for event in runner.run_turn(
                turn_session, "run it", connectors=[], graph_factory=agent.graph_factory
            )
        ]

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


class _PlanClearingAgent(ScriptedTurn):
    """Plans, launches a job, and clears its todo list in the resume — the topic-change shape.

    An emptied plan is ordinary behaviour; it lands at the runner's second `PlanEvent` site.
    """

    def __init__(self, session: TurnSession) -> None:
        """Plan into `session`'s todo store, then clear it on the continuation pass."""
        self.calls = 0
        self._session = session

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        self.calls += 1
        if self.calls == 1:
            record_job_started("qm-9", "qm")
            yield "running it. "
        else:
            yield "never mind, here is the answer."


class _CappedLoopAgent(ScriptedTurn):
    """An agent whose loop still wanted another iteration when it stopped — a capped turn.

    Calls `record_loop_cap`, as the real cap does (pinned in `tests/test_langgraph_stream.py`);
    this is the front-door half.
    """

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        record_loop_cap()
        yield "still working on it"


def test_a_capped_turn_reports_the_runaway_guard_before_its_partial_answer() -> None:
    """A capped turn emits one `loop_cap_reached` error, and its partial answer still goes out."""
    events = _events(_CappedLoopAgent())
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert [e.code for e in errors] == ["loop_cap_reached"]
    assert errors[0].retryable is False
    assert str(settings.harness_max_loop_iterations) in errors[0].message
    # Before the answer, so a surface can mark it partial as it lands rather than retroactively.
    assert events.index(errors[0]) < events.index(_answer(events))
    assert _answer(events).text == "still working on it"


class _CappedSpendAgent(ScriptedTurn):
    """A turn whose spend guard fired, marked the way a real one marks it.

    The guard is proven on a compiled graph in `tests/test_spend_cap.py`; this covers the runner
    turning the mark into something a chemist sees.
    """

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        record_spend_cap(1_234_567)
        yield "as much as the budget bought"


def test_a_turn_stopped_by_its_budget_says_so_before_its_partial_answer() -> None:
    """A turn stopped by its budget says so, with the token count, before its partial answer.

    `tests/test_spend_cap.py` never goes through `run_turn`, so this is the only test of the
    user-visible half. The number lets a chemist tell a too-big request from a too-low ceiling.
    """
    events = _events(_CappedSpendAgent())
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert [e.code for e in errors] == ["spend_cap_reached"]
    assert errors[0].retryable is False
    assert "1,234,567" in errors[0].message, "the refusal does not say what the turn actually spent"
    # Before the answer, so a surface marks it partial as it lands rather than retroactively —
    # and `Chemclaw3_ui`'s `PARTIAL_ANSWER_CODES` depends on exactly this ordering.
    assert events.index(errors[0]) < events.index(_answer(events))
    assert _answer(events).text == "as much as the budget bought"


def test_a_capped_turns_spend_is_counted_for_an_operator() -> None:
    """The counter moves, which is the only way a deployment sees the guard firing at all.

    Declared-ness is asserted in `tests/test_spend_cap.py`; that a turn ever *increments* it was
    not, and deleting the `METRICS.increment` line survived the suite.
    """
    before = METRICS.value("chemclaw_turn_spend_caps_total")
    _events(_CappedSpendAgent())
    assert METRICS.value("chemclaw_turn_spend_caps_total") == before + 1


def test_an_ordinary_turn_does_not_claim_the_cap_fired() -> None:
    """A signal that is always on is worth nothing; the common path must stay silent."""
    assert [e for e in _run_turn() if isinstance(e, ErrorEvent)] == []


def test_verifier_failure_degrades_to_plain_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on but raising: the turn still returns its answer, unscored — never a sunk turn."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)

    async def _boom(answer: str, *_: Any, **__: Any) -> VerificationResult:
        raise RuntimeError("verifier down")

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _boom)
    answer = _answer(_run_turn())
    assert answer.text == "Yield was 90% [[reaction-a]]."
    assert answer.confidence is None
    # A verifier that was on and crashed must not read as one that ran and passed; the turn is still
    # returned.
    assert answer.review_required is True
    assert answer.unsupported_claims == ["verification did not run"]


def test_every_method_the_trace_offers_is_one_the_shipped_turn_calls() -> None:
    """No method of `ToolCallTrace` may have its only caller in this suite.

    A rule rather than a named absence, because a method a turn stops calling is otherwise hidden by
    its own tests still passing. Names containing `__mutmut_` are mutmut's scaffolding inside
    `mutants/`, not shipped methods, and are filtered so `make mutants` can start.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    api = (src / "api").rglob("*.py")
    readers = "\n".join(
        path.read_text(encoding="utf-8") for path in api if path.name != "runner_trace.py"
    )
    offered = [
        name
        for name, value in vars(runner_trace.ToolCallTrace).items()
        if not name.startswith("_")
        and "__mutmut_" not in name
        and callable(getattr(value, "fget", value))
    ]
    unused = [name for name in offered if f".{name}" not in readers]
    assert unused == [], (
        f"{unused} is offered by ToolCallTrace and called from no module under src/chemclaw/api; "
        "either the shipped turn stopped using it or its caller was never written"
    )


def test_a_result_event_carries_the_values_the_preview_cuts_off() -> None:
    """The trace reads ids and figures off the whole result; only the preview is truncated.

    Built from a recorded `ich_impurity_limit` result whose PDEs sit past the preview cut (see
    `tests/recorded_tool_results.py`).
    """
    from tests.recorded_tool_results import RECORDED_ICH_LIMITS

    result = RECORDED_ICH_LIMITS["palladium"]
    assert len(result) > settings.agent_audit_max_arg_chars

    trace = runner_trace.ToolCallTrace()
    trace.issued("i1", "ich_impurity_limit", '{"substance": "palladium"}')
    event = asyncio.run(trace.returned("i1", result))

    assert event.preview == result[: settings.agent_audit_max_arg_chars]
    assert {100.0, 10.0, 1.0} <= set(event.numbers)
    assert not {"100.0", "10.0"} & set(event.preview.split())


def test_a_result_with_more_values_than_the_wire_allows_is_capped_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A result with more values than the wire allows is capped, and the drop is announced."""
    flood = ", ".join(str(n + 0.5) for n in range(settings.stream_max_result_numbers + 50))
    trace = runner_trace.ToolCallTrace()
    trace.issued("f1", "dump_table", "{}")
    with caplog.at_level(logging.WARNING, logger=runner_trace.__name__):
        event = asyncio.run(trace.returned("f1", flood))

    assert len(event.numbers) == settings.stream_max_result_numbers
    assert "dump_table" in caplog.text


class _CitingAgent(ScriptedTurn):
    """Answers with a citation, optionally after a tool that returned it.

    `tool_result` grounds the citation in this turn. `graph_factory` is overridden to compile a
    graph with one real tool, because the grounding gate reads `ToolCallTrace.outputs`; the tool is
    not named `find_notes`, which the registry already advertises.
    """

    def __init__(self, answer: str, *, tool_result: str | None = None) -> None:
        self._answer = answer
        self._tool_result = tool_result

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """The answer only; keeping the script to prose lets the no-tool case share this class."""
        yield self._answer

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
        build_kwargs["audit_sink"] = NullAuditSink()
        return build_langgraph_agent(ScriptedChatModel(script), **build_kwargs)


def _offline_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification on, judge unreachable — so the offline citation gate produces the verdict.

    `verifier_enabled` also routes to the LLM judge, and an unreachable judge degrades to the
    offline check.
    """
    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    class _Unreachable:
        async def get_response(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("no verifier endpoint in a test process")

    monkeypatch.setattr(verifier, "_default_client", _Unreachable)


def _verified_answer(agent: ScriptedTurn) -> AnswerEvent:
    async def _collect() -> list[Any]:
        session = TurnSession(session_id="s-cite")
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
    """A citation the turn never retrieved is unsupported, though the note exists on disk.

    The verifier scores against what the turn saw; re-resolving from `knowledge_path` would certify
    it.
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
    # The citation gate stood in for the judge; it checks resolvability, not faithfulness, so it
    # cannot clear review on the judge's behalf. The citation still resolved at confidence 1.0.
    assert answer.verified_by == "citation-gate"
    assert answer.review_required is True


def test_a_tool_result_grounds_the_answer_past_the_uis_preview_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grounding reads the whole tool result; only the wire carries the 200-character preview.

    The cited note does not exist on disk, so only the full tool result can ground it.
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
    # The citation gate stood in for the judge; it checks resolvability, not faithfulness, so it
    # cannot clear review on the judge's behalf. The citation still resolved at confidence 1.0.
    assert answer.verified_by == "citation-gate"
    assert answer.review_required is True


_METHOD_ANSWER = "Use a Kinetex C18 column at 1.0 mL/min with detection at 254 nm."


def test_an_ungrounded_method_parameter_marks_the_answer_for_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ungrounded branded method parameter marks the answer for review.

    Verification is off, so the mark can only come from the shape scan; the matched text rides on
    `unsupported_claims`.
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


def test_the_shape_gate_turned_off_marks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the shape gate turned off, the same answer that trips it gets no mark."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    monkeypatch.setattr(settings, "answer_shape_gate_enabled", False)
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
        session = TurnSession(session_id="s-probe")
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
    """A durable outage is announced before the first token, in the event connectors use.

    An unreachable broker removes every durable capability at once; the model must know before it
    plans.
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
    """A broker that fails the health RPC is degraded although `connect` succeeds.

    The client is cached for the process's life, so only the health RPC reaches the wire per turn.
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

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield ""


def test_a_turn_that_writes_nothing_says_so_instead_of_answering_emptily() -> None:
    """A turn that writes nothing says so with an `ErrorEvent` instead of answering emptily.

    Inventing prose to fill the gap would be worse.
    """
    events = _events(_SilentAgent())

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors, "a turn that produced no text emitted no error — the silent death itself"
    assert errors[0].code == "empty_answer"
    assert errors[0].retryable is True, "a narrower question can succeed; this is not terminal"

    # And no `AnswerEvent` beside it: an empty answer renders as an answered turn, and only
    # `loop_cap_reached` shares its turn with an answer.
    assert not [e for e in events if isinstance(e, AnswerEvent)], (
        "a turn that produced nothing also claimed an answer; the error and the empty bubble "
        "tell a chemist two different things about the same turn"
    )


def test_the_transcript_stores_what_the_agent_did_not_only_what_it_said() -> None:
    """The turn's tool exchanges reach `session_messages`, so a reload shows what the agent did.

    Asserted on the stored rows, since the transcript route reads them correctly.
    """

    class _Recorder:
        """A history provider that keeps what it was handed, which is the whole assertion."""

        def __init__(self) -> None:
            self.rows: list[Any] = []

        async def save_messages(self, session_id: str, messages: Any, **_kw: Any) -> None:
            self.rows.extend(messages)

    history = _Recorder()
    agent = _CitingAgent("THF was the solvent [[compound-thf]].", tool_result="THF, 65 C")

    async def _collect() -> list[Any]:
        session = TurnSession(session_id="s-transcript")
        return [
            event
            async for event in runner.run_turn(
                session,
                "which solvent?",
                connectors=[],
                graph_factory=agent.graph_factory,
                history=history,
            )
        ]

    events = asyncio.run(_collect())

    assert any(e.type == "tool_result" for e in events), "the fixture never ran a tool"
    calls = [m for m in history.rows if getattr(m, "tool_calls", None)]
    results = [m for m in history.rows if getattr(m, "tool_call_id", None)]

    assert calls, "the assistant's tool call was not stored — the transcript cannot show it"
    assert results, "the tool result was not stored — no past result is fetchable on reload"
    # And the pairing survives, which is what the ref join needs: a result whose call id matches no
    # stored call is worse than an absent one, because the transcript would render it under nothing.
    assert {c["id"] for m in calls for c in m.tool_calls} == {m.tool_call_id for m in results}


# --- the plan-approval prompt: a gated turn that ends blocked must say so on the stream ----------


def _plan_gated(monkeypatch: pytest.MonkeyPatch, titles: list[str] | None, approved: bool) -> None:
    """Arrange a `plan_only` turn whose session proposes `titles` under a given decision state.

    Faked at the runner's imports, since the emission rule is under test. The fake returns whole
    steps, declarations included, because the plan identity is taken over them.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")

    async def _plan(_session_id: str) -> list[dict[str, Any]] | None:
        if titles is None:
            return None
        return [{"content": t, "status": "pending", "tools": []} for t in titles]

    async def _stands(_session_id: str, _plan_hash: str | None) -> bool:
        return approved

    monkeypatch.setattr(runner, "session_plan", _plan)
    monkeypatch.setattr(runner, "approval_stands", _stands)


def test_a_gated_turn_holding_an_unapproved_plan_asks_for_the_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated turn holding an unapproved plan emits `ApprovalRequestEvent` before the answer."""
    _plan_gated(monkeypatch, ["compute the pKa", "propose a note"], approved=False)
    events = _run_turn()
    prompts = [e for e in events if isinstance(e, ApprovalRequestEvent)]
    assert len(prompts) == 1, "one blocked turn asks exactly once"
    assert prompts[0].approval_id == "", "a plan approval has no durable hold to address"
    assert prompts[0].prompt == PLAN_APPROVAL_PROMPT
    assert events.index(prompts[0]) < events.index(_answer(events)), (
        "the ask must precede the answer, which ends the turn"
    )


def test_an_approved_plan_is_not_re_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A standing approval means nothing is waiting on the chemist — prompting would cry wolf."""
    _plan_gated(monkeypatch, ["compute the pKa"], approved=True)
    assert [e for e in _run_turn() if isinstance(e, ApprovalRequestEvent)] == []


def test_a_session_proposing_no_plan_is_not_asked_to_approve_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty plan has no identity to decide on — same rule as the decision route's 409.

    `plan_identity` returns `None` for it, and both readers ask the same function.
    """
    _plan_gated(monkeypatch, [], approved=False)
    assert [e for e in _run_turn() if isinstance(e, ApprovalRequestEvent)] == []


def test_an_unreadable_plan_stays_silent_rather_than_failing_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` from the checkpointer is "unreadable", not "absent" — the turn keeps its answer.

    The card is skipped, and the gate still refuses unapproved work on the next call.
    """
    _plan_gated(monkeypatch, None, approved=False)
    events = _run_turn()
    assert [e for e in events if isinstance(e, ApprovalRequestEvent)] == []
    assert _answer(events) is not None


def test_an_approval_that_authorizes_no_tool_is_still_an_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approval that authorizes no tool is still an approval, so the card is not re-asked.

    `frozenset()` means approved with no state-changing tool; `None` means undecided. The real
    `approval_stands` and store are driven, since stubbing them hid a truthiness collapse.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    plan_approval_store.cache_clear()
    store = plan_approval_store()
    _plan_gated(monkeypatch, ["look up the melting point of aspirin"], approved=False)
    monkeypatch.setattr(runner, "approval_stands", approval_stands)
    steps = [{"content": "look up the melting point of aspirin", "status": "pending", "tools": []}]
    plan_hash = plan_identity(steps)
    assert plan_hash is not None
    asyncio.run(store.record("s-1", plan_hash, "chemist-1", True, frozenset()))
    try:
        assert [e for e in _run_turn() if isinstance(e, ApprovalRequestEvent)] == [], (
            "a plan whose approval authorizes no tool was read as unapproved, so the chemist is "
            "asked again for a decision they have already made"
        )
    finally:
        plan_approval_store.cache_clear()


def test_a_classic_turn_never_asks_for_plan_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the harness off there is no plan and no gate — the prompt would be unanswerable."""
    monkeypatch.setattr(settings, "harness_enabled", False)

    async def _plan(_session_id: str) -> list[dict[str, Any]] | None:
        raise AssertionError("an ungated turn must not read the plan at all")

    monkeypatch.setattr(runner, "session_plan", _plan)
    assert [e for e in _run_turn() if isinstance(e, ApprovalRequestEvent)] == []


class _CappedAndSilentAgent(ScriptedTurn):
    """A turn whose cap fired before the model wrote anything — `cap` picks which cap."""

    def __init__(self, cap: str) -> None:
        self._cap = cap

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Record the cap and yield no prose, which is what the drive at `cap=1` produced."""
        if self._cap == "spend":
            record_spend_cap(1_020)
        else:
            record_loop_cap()
        yield ""


@pytest.mark.parametrize(
    ("cap", "code"), [("spend", "spend_cap_reached"), ("loop", "loop_cap_reached")]
)
def test_a_capped_turn_that_wrote_nothing_says_so_once(cap: str, code: str) -> None:
    """A capped turn that wrote nothing emits one event, moves one counter, promises nothing.

    Driven through `run_turn` because the defect is a sequence of two events: the cap error and an
    `empty_answer` error with opposite `retryable` flags, the empty-answer counter moving, and a cap
    message saying "the answer below is partial". Parametrized over both caps, which share one
    sentence. The turn's outcome is carried by `chemclaw_turns_finished_total`.
    """
    empties = METRICS.value("chemclaw_turn_empty_answers_total")
    events = _events(_CappedAndSilentAgent(cap))
    errors = [event for event in events if isinstance(event, ErrorEvent)]

    assert [error.code for error in errors] == [code], (
        f"a capped silent turn emitted {[e.code for e in errors]}; two errors about one silence "
        "with opposite `retryable` flags is what a surface cannot reconcile"
    )
    assert METRICS.value("chemclaw_turn_empty_answers_total") == empties, (
        "`chemclaw_turn_empty_answers_total` moved for a turn a cap had already named, so "
        "`ChemclawTurnsAnsweringEmpty` fires with its own description ('nothing explains it') "
        "false and the operator is sent after the wrong cause"
    )
    assert "nothing below to read" in errors[0].message, (
        f"the cap event still promises an answer that was never written: {errors[0].message}"
    )
    assert "the answer below is partial" not in errors[0].message, errors[0].message
    # No `AnswerEvent` at all: an empty one renders as a blank assistant bubble, costs a judge call
    # under `verifier_enabled`, and books `completed=True` for a turn that answered nothing.
    assert not [event for event in events if isinstance(event, AnswerEvent)], (
        "a capped silent turn shipped an AnswerEvent, so it books as answered"
    )


def test_a_capped_turn_that_did_write_still_calls_its_answer_partial() -> None:
    """A capped turn that did write still calls its answer partial; the wording is conditional."""
    events = _events(_CappedSpendAgent())
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [error.code for error in errors] == ["spend_cap_reached"]
    assert "so the answer below is partial" in errors[0].message, errors[0].message
    assert _answer(events).text == "as much as the budget bought"
