"""First-party spans, and the propagation that makes them join up across a process boundary.

`configure_telemetry` installs the tracer provider and that was once the whole tracing story: the
LLM client's own spans, and nothing else. So a trace showed model calls floating with no parent
— no turn to hang them from, no tool call around them, and nothing at all from a connector, because
each connector process started its own unrelated trace. Meanwhile `deploy/README.md` claimed "spans
cover a turn and a job" and that dashboards track loop iterations, which described a system nobody
had built.

Two things were missing, and only one of them is spans.

**A span at each boundary this system actually has.** A turn, and a tool call. Those are the two
units a chemist and an operator both reason in — "this question took 40 seconds, and 31 of them were
one xTB call" — and neither existed. Deliberately not more: a span per loop iteration or per
retriever would be a trace nobody reads, and the row's complaint was that the docs overstate the
tracing, so answering it with *more* unread spans would be the same mistake in the other direction.

**`traceparent`, which is the part that was really broken.** `connectors/identity.py` propagates a
*custom* `X-Chemclaw-Correlation` header, and that header is the tell the readiness review picked
up on: it exists because the standard one was not being sent. A correlation id joins *log lines*
after the fact, by grep. W3C trace context joins *spans*, live, in whatever the collector shows —
so a connector's work appears inside the turn that asked for it instead of as an orphan trace an
operator has to know to go looking for. The custom header stays: it is what the audit trail is
keyed on (`audit_events.correlation_id`), it survives where no collector is configured, and the two
answer different questions.

**Everything here is inert when tracing is off**, which is the default. `start_span` returns a
no-op context manager and `trace_headers` returns an empty dict, so the cost on the ordinary path is
one boolean read. That matters more than it sounds: this is called per tool call on the event loop
that also serves every SSE stream.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# The instrumentation scope every first-party span is created under, so a collector can separate
# "spans Chemclaw wrote" from the ones the framework and the OTel instrumentations produce.
TRACER_NAME = "chemclaw"

# The standard W3C trace-context headers. Named here rather than reached for through the propagator
# so the connector server can list what it accepts, and so a reader of `connectors/identity.py` can
# see what leaves the process without following an indirection into OTel.
TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"


def _tracer() -> Any:
    """The process tracer, or None when tracing is off or the SDK is absent.

    Imported lazily and tolerantly for the reason `core/metrics_bridge.py` is: this is called from
    the agent and connector layers, which must run in processes where the observability extras may
    not be installed, and an import error here must degrade to "no spans" rather than break a turn.
    """
    if not settings.otel_enabled:
        return None
    try:
        from opentelemetry import trace

        return trace.get_tracer(TRACER_NAME)
    except Exception:  # pragma: no cover - defensive; tracing must never break the caller
        logger.debug("tracing enabled but the OpenTelemetry API is unavailable", exc_info=True)
        return None


@contextmanager
def start_span(name: str, **attributes: str | int | float | bool) -> Iterator[None]:
    """Run the block inside a span named `name`, or unchanged when tracing is off.

    Attributes are keyword arguments rather than a dict because every call site here passes a
    handful of literals, and the keyword form is what makes an accidental turn *content* attribute
    visible in review — a span attribute travels to the collector, so the rule is the one
    `/metrics` follows: identifiers and counts, never a question, an argument or an answer.
    """
    tracer = _tracer()
    if tracer is None:
        yield
        return
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        yield


def trace_headers() -> dict[str, str]:
    """The W3C trace-context headers for the current span, empty when there is no trace.

    This is what makes a connector's spans children of the turn that called it. Without it every
    connector process starts a fresh trace, so the expensive half of a chemist's question — the
    calculation — is an orphan an operator has to know to go looking for.
    """
    tracer = _tracer()
    if tracer is None:
        return {}
    try:
        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        return carrier
    except Exception:  # pragma: no cover - defensive, as above
        logger.debug("could not inject trace context", exc_info=True)
        return {}


@contextmanager
def continue_trace(headers: Mapping[str, str]) -> Iterator[None]:
    """Adopt an incoming request's trace context for the body of the block.

    The receiving half of `trace_headers`. Unlike the `X-Chemclaw-*` identity headers — which are
    advisory and must never reach an access decision — trace context is *safe* to trust from
    outside, because the worst a forged `traceparent` can do is attach spans to a trace that is not
    theirs. It buys no authority, so the trust rule that governs the identity headers does not
    apply and the difference is worth stating where both arrive on the same request.
    """
    tracer = _tracer()
    if tracer is None or TRACEPARENT not in headers:
        yield
        return
    try:
        from opentelemetry import context as otel_context
        from opentelemetry.propagate import extract

        token = otel_context.attach(extract(dict(headers)))
    except Exception:  # pragma: no cover - defensive, as above
        logger.debug("could not extract trace context", exc_info=True)
        yield
        return
    try:
        yield
    finally:
        otel_context.detach(token)
