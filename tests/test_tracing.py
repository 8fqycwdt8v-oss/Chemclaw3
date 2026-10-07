"""Turn, tool and connector spans, and W3C trace propagation across the connector boundary.

A correlation id joins log lines after the fact; `traceparent` makes a connector's spans children
of the turn that asked for them. Driven against a real in-memory OTel SDK, because the thing under
test is whether a parent-child relationship forms across a header boundary.
"""

from collections.abc import Iterator

import pytest


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """A real tracer provider exporting into a list, with tracing switched on."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The global provider is set-once per process, so the tracer is taken from ours directly rather
    # than through `trace.set_tracer_provider`, which a second test in the same run cannot undo.
    monkeypatch.setattr("chemclaw.core.config.settings.otel_enabled", True)
    monkeypatch.setattr(
        "chemclaw.core.tracing._tracer", lambda: provider.get_tracer("chemclaw-test")
    )
    yield exporter.get_finished_spans  # type: ignore[misc]
    trace.NoOpTracerProvider()  # keep the global provider untouched for other tests


def test_a_span_is_written_where_there_were_none(spans: object) -> None:
    """The turn and tool boundaries had no spans at all — only MAF's own model calls."""
    from chemclaw.core.tracing import start_span

    with start_span("chemclaw.tool", **{"tool.name": "predict_pka"}):
        pass

    finished = spans()  # type: ignore[operator]
    assert [span.name for span in finished] == ["chemclaw.tool"]
    assert finished[0].attributes["tool.name"] == "predict_pka"


def test_a_nested_span_is_a_child_not_a_second_root(spans: object) -> None:
    """A tool call inside a turn has to be *inside* it, or the trace is a flat list of fragments.

    This is the whole reason a turn span exists: "the question took 40 seconds and 31 of them were
    one xTB call" is only readable if the tool span nests.
    """
    from chemclaw.core.tracing import start_span

    with start_span("chemclaw.turn"), start_span("chemclaw.tool"):
        pass

    finished = {span.name: span for span in spans()}  # type: ignore[operator]
    assert finished["chemclaw.tool"].parent is not None
    assert finished["chemclaw.tool"].parent.span_id == finished["chemclaw.turn"].context.span_id


def test_tracing_off_is_a_no_op_rather_than_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracing off is the default and must be a no-op rather than a failure.

    It runs per tool call on the loop serving every SSE stream, so the disabled cost is a boolean
    read and the body executes unchanged.
    """
    from chemclaw.core.tracing import start_span, trace_headers

    monkeypatch.setattr("chemclaw.core.config.settings.otel_enabled", False)
    ran = False
    with start_span("chemclaw.tool"):
        ran = True
    assert ran
    assert trace_headers() == {}


def test_a_connector_call_carries_the_standard_header_as_well_as_the_custom_one(
    spans: object,
) -> None:
    """A connector call carries `traceparent` as well as the custom correlation header.

    The correlation id keys `audit_events` and works without a collector; `traceparent` joins the
    connector's spans to this turn.
    """
    from chemclaw.core.call_identity import (
        HEADER_CORRELATION,
        turn_headers,
    )
    from chemclaw.core.identity_context import (
        reset_current_correlation_id,
        set_current_correlation_id,
    )
    from chemclaw.core.tracing import TRACEPARENT, start_span

    token = set_current_correlation_id("cid-1")
    try:
        with start_span("chemclaw.turn"):
            headers = turn_headers()
    finally:
        reset_current_correlation_id(token)

    assert headers[HEADER_CORRELATION] == "cid-1", "the audit trail's join key stopped travelling"
    assert TRACEPARENT in headers, (
        "no W3C trace context on a connector call, so the connector's spans start a new trace"
    )


def test_the_connector_side_adopts_the_caller_trace(spans: object) -> None:
    """The receiving half. Without it the propagation is write-only.

    A connector that receives `traceparent` and ignores it produces exactly the orphan trace the
    header was added to prevent — and the sending half would still pass its own test.
    """
    from chemclaw.core.tracing import continue_trace, start_span, trace_headers

    with start_span("chemclaw.turn"):
        carrier = trace_headers()

    # A fresh context, as a separate process would have.
    with continue_trace(carrier), start_span("connector.tool"):
        pass

    finished = {span.name: span for span in spans()}  # type: ignore[operator]
    turn, connector = finished["chemclaw.turn"], finished["connector.tool"]
    assert connector.context.trace_id == turn.context.trace_id, (
        "the connector's span is in its own trace, which is the orphan this header prevents"
    )


def test_an_absent_traceparent_is_not_an_error(spans: object) -> None:
    """A caller with tracing off still reaches a connector with it on."""
    from chemclaw.core.tracing import continue_trace, start_span

    with continue_trace({}), start_span("connector.tool"):
        pass
    assert [span.name for span in spans()] == ["connector.tool"]  # type: ignore[operator]


def test_both_boundaries_are_actually_instrumented() -> None:
    """Both boundaries call the tracing helpers.

    Asserted on the source, since a real turn needs a model and a connector needs a server.
    """
    import inspect
    import re

    from chemclaw.agent import audit
    from chemclaw.api import runner
    from chemclaw.connectors import server
    from chemclaw.core import call_identity

    def _opens(module: object, name: str) -> bool:
        """Whether `module` calls `start_span` with `name`, across a line break.

        Matched as a call spanning whitespace, since the formatter may wrap a long
        `start_span(...)`.
        """
        source = inspect.getsource(module)  # type: ignore[arg-type]
        return re.search(rf'start_span\(\s*"{re.escape(name)}"', source) is not None

    assert _opens(runner, "chemclaw.turn"), "a turn opens no span"
    assert _opens(audit, "chemclaw.tool"), "a tool call opens no span"
    assert "trace_headers()" in inspect.getsource(call_identity), (
        "no trace context leaves the process"
    )
    assert "continue_trace(" in inspect.getsource(server), "a connector ignores the caller's trace"


async def test_a_real_turn_exports_a_turn_span(spans: object) -> None:
    """A real turn exports a turn span.

    The source check above cannot tell a context manager created from one entered, so the boundary
    is exercised through `run_turn` with a fake agent.
    """
    from collections.abc import AsyncIterator

    from chemclaw.agent.session import TurnSession
    from chemclaw.api.runner import run_turn
    from tests.fakes_turn import Piece, ScriptedTurn

    class _Turn(ScriptedTurn):
        """One token, so the turn really runs and really finishes."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield "ok"

    session = TurnSession(session_id="s-trace")
    turn = _Turn()
    async for _event in run_turn(session, "hello", connectors=[], graph_factory=turn.graph_factory):
        pass

    assert "chemclaw.turn" in {span.name for span in spans()}, (  # type: ignore[operator]
        "a real turn exported no span, so the boundary the docs claim is still uninstrumented"
    )


def test_a_failure_description_carries_no_content_while_the_flag_is_off(
    spans: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure description carries no content while `otel_include_sensitive_data` is off.

    The span status description and the SDK's `exception` event are content channels too, and
    `SecretRedactingFilter` never sees spans.
    """
    from chemclaw.core.tracing import start_span

    monkeypatch.setattr("chemclaw.core.config.settings.otel_include_sensitive_data", False)
    marker = "CONTENT-MARKER-patient-identifier"
    with pytest.raises(ValueError):
        with start_span("chemclaw.tool", **{"tool.name": "mytool"}) as span:
            span.failed(f'ValueError("upstream said: {marker}")')
            raise ValueError(f"upstream said: {marker}")

    exported = spans()  # type: ignore[operator]
    blob = repr(
        [
            (s.status.description, [(e.name, dict(e.attributes or {})) for e in s.events])
            for s in exported
        ]
    )
    assert marker not in blob, blob
    # The class survives, because "which failure" is an identifier and the rule this module states
    # is identifiers and counts. An operator filtering by `status=ERROR` still sees the span.
    assert "ValueError" in blob
    assert exported[0].status.status_code.name == "ERROR"


def test_a_credential_never_reaches_a_span_even_with_content_allowed(
    spans: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential never reaches a span, even with content allowed.

    The flag governs turn content, not this process's credentials, which an upstream can echo back
    in a 401 body; the `core/logging` value inventory applies either way.
    """
    from chemclaw.core.tracing import start_span

    monkeypatch.setenv("CHEMCLAW_LLM_API_KEY", "MARKERLLMKEY9a4x")
    monkeypatch.setattr("chemclaw.core.config.settings.otel_include_sensitive_data", True)
    with start_span("chemclaw.tool") as span:
        span.failed("RuntimeError: 401 from backend: Authorization: Bearer MARKERLLMKEY9a4x")

    exported = spans()  # type: ignore[operator]
    assert "MARKERLLMKEY9a4x" not in repr(exported[0].status.description)
