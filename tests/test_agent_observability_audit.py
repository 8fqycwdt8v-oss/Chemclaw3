"""A refused tool call, a crashed one and an abandoned one are three events, not one.

The decision is `D-2026-08-27-a-refusal-is-not-a-crash`. Measured before it: a dry-run refusal and a
repeat-guard trip both produced `outcome='error'` and a log line reading `tool X failed after N
ms: <prose>` — `agent/audit.py` interpolated `%s` on the exception *instance*, so the class was gone
from the log while `bounded_repr`'s repr kept it in the row. The database was strictly more
diagnostic than the log, inverting that module's own opening rule that the log is the floor.

The span half was measured the same way: clean `UNSET`, raised `ERROR`, `CancelledError` `UNSET`,
**returned error `UNSET`** — and CLAUDE.md records that an MCP tool never raises, so essentially
every connector-tool failure in production was a span an operator filtering `status=ERROR` could not
see.

Everything here drives the real middleware against the real metrics registry and a real in-memory
OTel exporter. A refusal that is classified correctly and counted wrongly is exactly the failure
this file exists to catch, so nothing is asserted through a double.
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

    The handler raises `raises` if given, otherwise returns `returns` — which covers the three ways
    a tool ends that the trail must tell apart, plus the cancellation case a caller drives by
    passing `asyncio.CancelledError()`.

    The escaping exception is **returned rather than left to `pytest.raises`** because both halves
    are claims: the row is written *and* the exception reaches the caller unchanged. Catching it
    here lets one test assert `raised is refusal` — object identity, so a middleware that re-raised
    a re-wrapped copy would fail — while still reading the sink the call filled on its way out.
    """
    sink = _Sink()
    middleware = make_audit_middleware(correlation_id="cid-1", actor="alice@corp", sink=sink)
    # A registered tool, because that is what the graph passes for a name it holds — and
    # `metric_tool_name` reads `.name` off it to decide whether the label is safe to mint.
    # A registered tool, because that is what the graph passes for a name it holds — and
    # `metric_tool_name` reads `.name` off it to decide whether the label is safe to mint.
    # `registered=False` is the `ToolNode` shape for a name the model invented.
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

    The same arrangement `tests/test_tracing.py` uses and for the same reason: the property under
    test is what a *collector* would receive, and a mock that records calls would assert this module
    invoked an API rather than that the span said what it should.
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

    Order is the classification: four of the five types are `AuthorizationError` subclasses, so a
    scan that tested the base first would report every refusal as `authz`. The negative case is the
    point of the whole exercise — a `KeyError` in a parser must not become a governance decision.
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
    """The row says `refused`, the log names the class, and the reason counter moves.

    All three, because each was its own half-measure: the outcome is what an auditor reads, the
    class is what a log query filters on, and the counter is what a dashboard shows without anybody
    reading either.
    """
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

    `ToolNode` invokes this chain for a name the graph does not hold — that is deliberate, so an
    interceptor can short-circuit an unregistered call — so the name on `request.tool_call` is the
    *model's* string, and putting it on a metric label makes `/metrics` grow by one series per
    thing a model invents. Measured on a compiled graph before the clamp: a single hallucinated
    call created a `chemclaw_tool_calls_total` series **and** a full fourteen-bucket histogram, and
    driven directly the label accepted 230 characters of arbitrary text. Model output is
    attacker-influenceable here — it is why this tree carries `frame_untrusted` — so an injected
    document could grow the registry until the pod died.

    The audit *row* still carries what the model asked for; only the label is refused.
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

    One distribution used to pool a minutes-long xTB call through the calc connector with a
    sub-millisecond `read_attachment`, so "why is this turn slow" could not be attributed to a
    tool — the question the histogram's own docstring says it exists to answer.
    """
    before = METRICS.observations("chemclaw_tool_duration_seconds")[0]

    _drive("predict_solubility", returns="0.4 g/L")

    exposition = METRICS.render()
    assert 'chemclaw_tool_calls_total{outcome="ok",tool="predict_solubility"}' in exposition
    assert 'chemclaw_tool_duration_seconds_count{tool="predict_solubility"}' in exposition
    assert METRICS.observations("chemclaw_tool_duration_seconds")[0] == before + 1


def test_the_row_names_the_plan_step_the_call_served() -> None:
    """`audit_events.plan_step` — the join `job_records` had and the trail did not.

    Read off the request through the same `plan_link_for_call` a job is stamped with, because the
    ambient link `stamp_plan_link` binds is *reset* by the time the row is written: that middleware
    is innermost and resets in a `finally` while this one is outermost. Measured before the fix —
    `get_current_plan_link()` read `("", "")` at this point.

    This case is a batch with **no** rewrite in it, which is the fallback half of that reading. The
    case with one is next door, and it is the one the state-only read got wrong.
    """
    sink, _ = _drive("compute_xtb_energy", todos=_TODOS)
    assert sink.events[0].plan_step == "run the conformer search"


def test_the_row_names_the_step_the_batch_marks_not_the_one_it_just_finished() -> None:
    """The off-by-one this row carried, and the reason the two records could not agree.

    The canonical harness batch is "tick step N completed, mark N+1 in_progress, call the tool" —
    one assistant message, `TodoListMiddleware`'s own pattern — and `request.state["todos"]` is the
    snapshot `ToolNode` took *before* it. `agent/plan_link.py` has worked around that from the day
    it was written; this row read the state directly, so measured on that batch it stamped
    `'run the conformer search'` — the step that had just **finished** — while
    `job_records.plan_step` for the job the same call launched said `'compute the pKa'`. The
    docstring beside it claimed "a tool call and the job it launched cannot disagree about which
    step they served", and `chemclaw explain` rendered the previous step for every ordinary call of
    every plan.

    The two readings are now one function (`plan_link.plan_link_for_call`), and this asserts them
    against each other rather than restating either.
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

    `record` buffers and returns, `ts` defaulted to `now()` at INSERT and `id` is a `BIGSERIAL`
    assigned at the same moment — so under load both the timestamps and the ordering
    `chemclaw explain` reconstructs a turn from belonged to the flusher.
    """
    before = datetime.now(UTC)
    sink, _ = _drive("find_notes")
    assert before <= sink.events[0].ts <= datetime.now(UTC)


def test_a_returned_failure_marks_the_span_error_where_it_used_to_say_nothing(
    spans: Callable[[], Any],
) -> None:
    """The T1 fix, and the case that covers most production tool failures.

    An MCP tool never raises: `langchain_mcp_adapters` converts an `isError=True` result inside
    `StructuredTool.ainvoke` and it comes back as a *return*. So the `with start_span(...)` block
    exited cleanly and OpenTelemetry had nothing to set a status from — measured `UNSET` while the
    audit row said `error`, two artifacts about one event that disagreed.
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

    So a tool interrupted by a client disconnect or the turn deadline left an `UNSET` span while
    the trail recorded a `cancelled` row on a shielded write — the same disagreement as above,
    reached through the one exception family OpenTelemetry's own helper does not see.
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

    Nothing here sets `ERROR` for a refusal deliberately: a policy decision is not a fault, and an
    error view full of them is an error view nobody reads. What makes the distinction available is
    the attribute, beside `chemclaw_tool_refusals_total{reason}`.
    """
    _drive("record_note", raises=UndeclaredWriteRefusal("not given to this agent"))

    assert spans()[0].attributes["outcome"] == "refused"
