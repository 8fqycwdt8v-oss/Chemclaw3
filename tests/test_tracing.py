"""Spans that exist, and the propagation that was the real finding.

`configure_telemetry` called MAF's `configure_otel_providers` and that was the whole tracing story:
the LLM client's own spans and nothing else. A trace showed model calls with no parent — no turn to
hang them from, no tool call around them — and nothing at all from a connector, because each
connector process started an unrelated trace. `deploy/README.md` meanwhile claimed spans cover a
turn and a job and that dashboards track loop iterations, none of which existed.

The propagation is the half worth naming. `connectors/identity.py` sends a *custom*
`X-Chemclaw-Correlation` header, and that header is the tell: it exists because the standard one was
not being sent. A correlation id joins log lines after the fact, by grep; `traceparent` joins spans,
live, so a connector's work appears inside the turn that asked for it.

Driven against a real in-memory OTel SDK rather than a mock. The failure mode being tested is
whether a *parent-child relationship* forms across a header boundary, and a mock that records calls
would assert that this module invoked an API, not that the trace joined up.
"""

from collections.abc import Iterator

import pytest

from tests.fakes import FakeUpdate


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
    """Off is the default, and it runs per tool call on the loop that serves every SSE stream.

    So the cost when disabled has to be a boolean read and the block has to execute unchanged —
    a tracing helper that raises or swallows the body when the collector is absent would be worse
    than no tracing.
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
    """The finding: a custom correlation header existed *because* `traceparent` did not.

    Both are sent, and they are not redundant. The correlation id is what `audit_events` is keyed
    on and survives where no collector is configured; `traceparent` is what makes a connector's
    spans children of this turn rather than an orphan trace.
    """
    from chemclaw.connectors.identity import HEADER_CORRELATION, turn_headers
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
    """The wiring, which is the claim the docs made and the code did not support.

    Asserted on the source because exercising a real turn needs a model and a connector needs a
    server; what can be wrong offline is that the helper exists and nothing calls it, which is the
    state this row is about.
    """
    import inspect
    import re

    from chemclaw.agent import audit
    from chemclaw.api import runner
    from chemclaw.connectors import identity, server

    def _opens(module: object, name: str) -> bool:
        """Whether `module` calls `start_span` with `name` — across a line break.

        Matched as a *call* spanning whitespace rather than as one literal string, because the
        formatter wraps a long `start_span(...)` and an assertion that only knows the one-line form
        fails on correct code. The same too-narrow-check family as the two entries in
        `tasks/lessons.md`, caught here by the test failing on a change I had just made.
        """
        source = inspect.getsource(module)  # type: ignore[arg-type]
        return re.search(rf'start_span\(\s*"{re.escape(name)}"', source) is not None

    assert _opens(runner, "chemclaw.turn"), "a turn opens no span"
    assert _opens(audit, "chemclaw.tool"), "a tool call opens no span"
    assert "trace_headers()" in inspect.getsource(identity), "no trace context leaves the process"
    assert "continue_trace(" in inspect.getsource(server), "a connector ignores the caller's trace"


def test_a_real_turn_exports_a_turn_span(spans: object) -> None:
    """Driven through `run_turn` itself, because "the call exists" is not "the span is entered".

    Found by a mutation: replacing `stack.enter_context(...)` with a plain assignment builds the
    context manager, never enters it, exports nothing — and passed the source check
    above, which can only see that the call is written. A `with`-less context manager is a
    plausible refactor and a silent loss of every turn span, so the boundary is exercised for real
    with a fake agent rather than asserted about.
    """
    import asyncio
    from typing import Any

    from agent_framework import AgentSession

    from chemclaw.api.runner import run_turn

    class _Agent:
        def run(self, message: str, **_options: Any) -> object:
            async def _gen() -> Any:
                yield FakeUpdate("ok")

            return _gen()

    async def _drive() -> None:
        session = AgentSession(session_id="s-trace")
        async for _event in run_turn(_Agent(), session, "hello", connectors=[]):
            pass

    asyncio.run(_drive())

    assert "chemclaw.turn" in {span.name for span in spans()}, (  # type: ignore[operator]
        "a real turn exported no span, so the boundary the docs claim is still uninstrumented"
    )
