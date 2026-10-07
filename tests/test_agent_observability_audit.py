"""A refused tool call, a crashed one and an abandoned one are three events, not one.

Decision: `D-2026-08-27-a-refusal-is-not-a-crash`. Refusals get their own outcome, log class and
counter; a returned MCP error marks the span `ERROR` (an MCP tool never raises). Everything drives
the real middleware against the real metrics registry and an in-memory OTel exporter.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from chemclaw.agent.audit import AuditEvent, make_audit_middleware, refusal_reason
from chemclaw.agent.plan_gate import plan_approval_refusal
from chemclaw.agent.plan_link import plan_link_for_call
from chemclaw.agent.repeat_guard import RepeatedCallRefusal
from chemclaw.agent.skill_backend import SkillsReadOnlyRefusal
from chemclaw.agent.tool_authz import DryRunRefusal, UndeclaredWriteRefusal
from chemclaw.core.metrics import METRICS
from tests.middleware import run_middleware, tool_request

_TODOS = [
    {"content": "look the solvent up", "status": "completed"},
    {"content": "run the conformer search", "status": "in_progress"},
]


class _Sink:
    """An `AuditSink` that keeps what the middleware decided to write."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Keep the event."""
        self.events.append(event)


def _drive(
    name: str,
    *,
    raises: BaseException | None = None,
    returns: Any = None,
    todos: list[dict[str, Any]] | None = None,
    batch_todos: list[dict[str, Any]] | None = None,
    registered: bool = True,
) -> tuple[_Sink, BaseException | None]:
    """Run one tool call through the audit middleware; return its sink and whatever escaped.

    The handler raises `raises` if given, otherwise returns `returns`. The escaping exception is
    returned so a test can assert both the row and that the same exception object reached the
    caller.
    """
    sink = _Sink()
    middleware = make_audit_middleware(correlation_id="cid-1", actor="alice@corp", sink=sink)
    # A registered tool is what the graph passes for a name it holds; `metric_tool_name` reads its
    # `.name`. `registered=False` is the `ToolNode` shape for a name the model invented.
    tool = SimpleNamespace(name=name, metadata={}) if registered else None
    request = tool_request(name, {"q": "x"}, tool=tool)
    if todos is not None or batch_todos is not None:
        state: dict[str, Any] = {"todos": todos or []}
        if batch_todos is not None:
            # The canonical harness batch: one assistant message carrying `write_todos` beside the
            # step's own call. `request.state["todos"]` is `ToolNode`'s pre-batch snapshot, so the
            # rewrite in this message is the only place the plan as of *this* call is visible.
            state["messages"] = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_todos",
                            "args": {"todos": batch_todos},
                            "id": "call-plan",
                        },
                        {"name": name, "args": {"q": "x"}, "id": "call-1"},
                    ],
                )
            ]
        request = request.override(state=state)

    async def _handler(_request: Any) -> Any:
        if raises is not None:
            raise raises
        return returns

    escaped: BaseException | None = None
    try:
        asyncio.run(run_middleware(middleware, request, _handler))
    except BaseException as exc:  # the point of this helper is to inspect whatever escaped
        escaped = exc
    return sink, escaped


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], Any]]:
    """A real tracer provider exporting into a list, with tracing switched on.

    The property under test is what a collector would receive, not that an API was called.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("chemclaw.core.config.settings.otel_enabled", True)
    monkeypatch.setattr(
        "chemclaw.core.tracing._tracer", lambda: provider.get_tracer("chemclaw-test")
    )
    yield exporter.get_finished_spans


def test_each_gate_classifies_as_its_own_reason_and_a_bug_classifies_as_none() -> None:
    """The five reasons `chemclaw_tool_refusals_total` declares, and the negative case.

    Order matters: four types subclass `AuthorizationError`, so testing the base first would call
    every refusal `authz`. A `KeyError` in a parser must not become a governance decision.
    """
    assert refusal_reason(DryRunRefusal("no")) == "dry_run"
    assert refusal_reason(UndeclaredWriteRefusal("no")) == "undeclared_write"
    assert refusal_reason(plan_approval_refusal("record_note")) == "plan_gate"
    assert refusal_reason(RepeatedCallRefusal("again")) == "repeat"
    # The base, reached by a plain role denial and by the skills tree's write refusal.
    assert refusal_reason(SkillsReadOnlyRefusal("read-only")) == "authz"
    assert refusal_reason(KeyError("solvent")) is None
    assert refusal_reason(TimeoutError()) is None


def test_a_refusal_is_recorded_as_refused_and_counted_by_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The row says `refused`, the log names the class, and the reason counter moves."""
    before = METRICS.value("chemclaw_tool_refusals_total")
    refusal = DryRunRefusal("DRY RUN — record_note changes stored data, so it was not called.")

    with caplog.at_level(logging.WARNING):
        sink, escaped = _drive("record_note", raises=refusal)

    assert escaped is refusal  # observe-only: the refusal reaches the gate above unchanged
    assert [event.outcome for event in sink.events] == ["refused"]
    assert "was refused" in caplog.text
    # The class name, which `%s` on the exception instance threw away.
    assert "DryRunRefusal" in caplog.text
    assert METRICS.value("chemclaw_tool_refusals_total") == before + 1
    assert 'chemclaw_tool_refusals_total{reason="dry_run"}' in METRICS.render()


def test_a_genuine_failure_stays_an_error_and_moves_no_refusal_counter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half of the separation: a parser bug is not a policy decision.

    Without this, the fix would be free to classify everything as a refusal and still pass the test
    above — which is the shape of the defect it corrects.
    """
    before = METRICS.value("chemclaw_tool_refusals_total")

    with caplog.at_level(logging.WARNING):
        sink, escaped = _drive("predict_pka", raises=KeyError("solvent"))

    assert isinstance(escaped, KeyError)
    assert [event.outcome for event in sink.events] == ["error"]
    assert "KeyError" in caplog.text
    assert METRICS.value("chemclaw_tool_refusals_total") == before


def test_a_name_the_graph_does_not_hold_cannot_mint_a_series() -> None:
    """A hallucinated tool name is one bucket, not one time series per string.

    `ToolNode` invokes this chain for unregistered names, so the label would be the model's string,
    and model output is attacker-influenceable: an injected document could grow `/metrics` without
    bound. The audit row still carries the name the model asked for.
    """
    hallucinated = "totally_made_up_tool_'; DROP TABLE audit_events; --" + "X" * 200
    sink, _ = _drive(hallucinated, returns="ok", registered=False)

    exposition = METRICS.render()
    assert hallucinated not in exposition, "a model-authored name reached the metrics surface"
    assert 'chemclaw_tool_calls_total{outcome="ok",tool="unknown"}' in exposition
    assert 'chemclaw_tool_duration_seconds_count{tool="unknown"}' in exposition
    # The trail keeps the real question, because that is the forensic fact.
    assert sink.events[-1].tool == hallucinated


def test_every_call_is_counted_by_tool_and_outcome_and_timed_under_its_own_name() -> None:
    """`chemclaw_tool_calls_total{tool,outcome}` and the per-tool latency label.

    Per tool, so a slow turn can be attributed to the tool that made it slow.
    """
    before = METRICS.observations("chemclaw_tool_duration_seconds")[0]

    _drive("predict_solubility", returns="0.4 g/L")

    exposition = METRICS.render()
    assert 'chemclaw_tool_calls_total{outcome="ok",tool="predict_solubility"}' in exposition
    assert 'chemclaw_tool_duration_seconds_count{tool="predict_solubility"}' in exposition
    assert METRICS.observations("chemclaw_tool_duration_seconds")[0] == before + 1


def test_the_row_names_the_plan_step_the_call_served() -> None:
    """`audit_events.plan_step` — the same join `job_records` has.

    Read off the request through `plan_link_for_call`, because the ambient link is reset by the
    innermost middleware before this outermost one writes. This case is a batch with no plan rewrite
    in it.
    """
    sink, _ = _drive("compute_xtb_energy", todos=_TODOS)
    assert sink.events[0].plan_step == "run the conformer search"


def test_the_row_names_the_step_the_batch_marks_not_the_one_it_just_finished() -> None:
    """The row names the step a batch marks in progress, not the one it just finished.

    In the "tick step N, mark N+1, call the tool" batch, `request.state["todos"]` is the snapshot
    from before the batch, so reading state directly names step N. The audit row and `job_records`
    must use the same `plan_link.plan_link_for_call`; this asserts them against each other.
    """
    after_the_tick = [
        {"content": "run the conformer search", "status": "completed"},
        {"content": "compute the pKa", "status": "in_progress"},
    ]
    sink, _ = _drive("compute_xtb_energy", todos=_TODOS, batch_todos=after_the_tick)
    assert sink.events[0].plan_step == "compute the pKa"
    # The same request, read the way a launched job reads it: one answer, not two.
    request = tool_request("compute_xtb_energy", {"q": "x"}).override(
        state={
            "todos": _TODOS,
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "write_todos", "args": {"todos": after_the_tick}, "id": "c-plan"},
                        {"name": "compute_xtb_energy", "args": {"q": "x"}, "id": "call-1"},
                    ],
                )
            ],
        }
    )
    assert plan_link_for_call(request)[0] == sink.events[0].plan_step


def test_a_call_outside_a_plan_stamps_the_empty_step_rather_than_a_guess() -> None:
    """No todo list reads as "this call was not made from a plan step" — 057's contract."""
    assert _drive("compute_xtb_energy")[0].events[0].plan_step == ""


def test_the_row_is_dated_when_the_call_started_not_when_the_sink_flushed() -> None:
    """`ts` is stamped in the middleware, so a batching sink cannot re-date the trail.

    Otherwise both the timestamps and the order `chemclaw explain` reconstructs would be the
    flush's.
    """
    before = datetime.now(UTC)
    sink, _ = _drive("find_notes")
    assert before <= sink.events[0].ts <= datetime.now(UTC)


def test_a_returned_failure_marks_the_span_error_where_it_used_to_say_nothing(
    spans: Callable[[], Any],
) -> None:
    """A returned failure marks the span `ERROR` — the case covering most production tool failures.

    An MCP tool never raises (`isError=True` comes back as a return), so without this the span
    stayed `UNSET` while the audit row said `error`.
    """
    from langchain_core.messages import ToolMessage
    from opentelemetry.trace import StatusCode

    failure = ToolMessage(content="no such solvent", tool_call_id="call-1", status="error")
    sink, escaped = _drive("predict_solubility", returns=failure)

    assert escaped is None  # it *returned* the failure; nothing raised, which is the whole point
    assert [event.outcome for event in sink.events] == ["error"]
    span = spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["outcome"] == "error"
    assert span.attributes["tool.name"] == "predict_solubility"
    # The join to the audit trail, in the direction a trace is read.
    assert span.attributes["correlation.id"] == "cid-1"


def test_a_cancelled_call_marks_the_span_too(spans: Callable[[], Any]) -> None:
    """`use_span` catches `Exception`, and a teardown delivers a `BaseException`.

    So a cancelled call must mark its span explicitly to agree with the `cancelled` audit row.
    """
    from opentelemetry.trace import StatusCode

    _sink, escaped = _drive("compute_xtb_energy", raises=asyncio.CancelledError())

    assert isinstance(escaped, asyncio.CancelledError)
    span = spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["outcome"] == "cancelled"


def test_a_clean_call_leaves_the_span_unset_and_says_so(spans: Callable[[], Any]) -> None:
    """The negative case: marking everything ERROR would pass both tests above and help nobody."""
    from opentelemetry.trace import StatusCode

    _drive("find_notes", returns="two notes")

    span = spans()[0]
    assert span.status.status_code is StatusCode.UNSET
    assert span.attributes["outcome"] == "ok"


def test_a_refusal_is_distinguishable_on_the_span_without_flooding_the_error_view(
    spans: Callable[[], Any],
) -> None:
    """A refusal raises, so OpenTelemetry marks it — the `outcome` attribute is what separates it.

    A policy decision is not a fault; the attribute, beside `chemclaw_tool_refusals_total{reason}`,
    lets a view separate the two.
    """
    _drive("record_note", raises=UndeclaredWriteRefusal("not given to this agent"))

    assert spans()[0].attributes["outcome"] == "refused"
