"""First-party spans, and the propagation that makes them join up across a process boundary.

Two span boundaries: a turn and a tool call, the units a chemist and an operator reason in.
Deliberately no finer spans (per loop iteration or retriever).

`traceparent` (W3C trace context) is sent to connectors so their spans appear inside the turn that
called them rather than as orphan traces. The custom `X-Chemclaw-Correlation-Id` header stays: it
keys the audit trail and works without a collector.

Everything here is inert when tracing is off (the default): `start_span` yields a no-op handle and
`trace_headers` returns `{}`, costing one boolean read on the event loop.
"""

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.logging import redact_secrets

logger = logging.getLogger(__name__)

# The instrumentation scope every first-party span is created under, so a collector can separate
# "spans Chemclaw wrote" from the ones the framework and the OTel instrumentations produce.
TRACER_NAME = "chemclaw"

# The standard W3C trace-context headers, named here so the connector server can list what it
# accepts without reaching into OTel.
TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"


def _tracer() -> Any:
    """The process tracer, or None when tracing is off or the SDK is absent.

    Imported lazily and tolerantly: callers run in processes where the observability extras may be
    missing, and an import error must mean "no spans", never a broken turn.
    """
    if not settings.otel_enabled:
        return None
    try:
        from opentelemetry import trace

        return trace.get_tracer(TRACER_NAME)
    except Exception:  # pragma: no cover - defensive; tracing must never break the caller
        logger.debug("tracing enabled but the OpenTelemetry API is unavailable", exc_info=True)
        return None


# The leading `ClassName(` or `ClassName:` of a failure description (as `bounded_repr(exc)` and the
# escape handler below render it). The class is an identifier and survives suppression; the rest is
# the message, which is content.
_FAILURE_CLASS = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]{0,63})[(:]")

# What a span says instead of a message when content is not permitted on the wire. Names the flag,
# because the operator reading a bare `***` in a collector has no way to find out what governs it.
_WITHHELD = "detail withheld (CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA is off)"


def exportable_detail(description: str) -> str:
    """What a failure description may carry to the collector, under the two rules that govern it.

    The one filter on the span exception channel: spans are not `LogRecord`s, so the log redaction
    never sees them.

    - A credential is never exported, whatever the content flag says: `redact_secrets` always runs.
    - A message is content, so with `otel_include_sensitive_data` off only the failure's class is
      exported, keeping `status=ERROR` plus the exception type diagnosable.
    """
    if settings.otel_include_sensitive_data:
        return redact_secrets(description)
    match = _FAILURE_CLASS.match(description.strip())
    return f"{match.group(1)}: {_WITHHELD}" if match else _WITHHELD


class SpanHandle:
    """The span the block is running in — or nothing at all, when tracing is off.

    Keeps the OpenTelemetry API inside this module, so callers in processes without the extras need
    no `Status` import or degradation logic of their own. Both methods are no-ops when untraced.
    """

    __slots__ = ("_span",)

    def __init__(self, span: Any | None) -> None:
        """Hold the live span, or `None` when tracing is off."""
        self._span = span

    def set_attribute(self, key: str, value: str | int | float | bool) -> None:
        """Stamp one attribute, under the rule `start_span` states: identifiers and counts only.

        Swallows any tracing failure, like `failed`: it is called from `except` blocks on the tool
        path, where a tracing error must not replace the failure being reported.
        """
        if self._span is None:
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:  # pragma: no cover - defensive; tracing must never break the caller
            logger.debug("could not set attribute %s on the current span", key, exc_info=True)

    def failed(self, description: str) -> None:
        """Mark the span `ERROR` with `description` — for a failure that does not *raise*.

        OpenTelemetry sets status only from an exception escaping the block, but an MCP tool returns
        its failure and a cancellation is a `BaseException` that `use_span` does not catch. Swallows
        tracing errors for the reason `record_metric` does. `description` is filtered here through
        `exportable_detail`, so no caller can forget the rule.
        """
        if self._span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_status(Status(StatusCode.ERROR, exportable_detail(description)))
        except Exception:  # pragma: no cover - defensive; tracing must never break the caller
            logger.debug("could not set an error status on the current span", exc_info=True)


@contextmanager
def start_span(name: str, **attributes: str | int | float | bool) -> Iterator[SpanHandle]:
    """Run the block inside a span named `name`, or unchanged when tracing is off.

    Attributes are keyword literals so any turn-content attribute is visible in review: identifiers
    and counts only, never a question, an argument or an answer. Yields a `SpanHandle` for marking
    an outcome or a returned (not raised) failure.
    """
    tracer = _tracer()
    if tracer is None:
        yield SpanHandle(None)
        return
    # The SDK's exception recording and status-on-exception are switched off and replaced:
    # `use_span` would export the exception message, full stacktrace and a status description no
    # first-party code can filter. The ERROR status is set here instead (with `exportable_detail`),
    # so refusals stay marked. `Exception` matches what `use_span` catches; `CancelledError` is
    # marked by `agent/audit.py`.
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        handle = SpanHandle(span)
        try:
            yield handle
        except Exception as exc:
            handle.failed(f"{type(exc).__name__}: {exc}")
            raise


def trace_header_names() -> frozenset[str]:
    """Every header name `trace_headers()` can produce, whether or not a span is active now.

    For `connectors.identity.turn_identity_hook`, which strips everything this system stamped when a
    request leaves the connector's origin, possibly after the span ended. Read from the configured
    propagator's `fields`, so a B3 propagator yields B3 names. Empty when tracing is off.
    """
    if _tracer() is None:
        return frozenset()
    try:
        from opentelemetry.propagate import get_global_textmap

        return frozenset(get_global_textmap().fields)
    except Exception:  # pragma: no cover - defensive, as `trace_headers` below
        logger.debug("could not read the propagator's header names", exc_info=True)
        return frozenset()


def trace_headers() -> dict[str, str]:
    """The W3C trace-context headers for the current span, empty when there is no trace.

    What makes a connector's spans children of the turn that called it.
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

    Unlike the advisory `X-Chemclaw-*` identity headers, trace context is safe to trust from
    outside: a forged `traceparent` only misattaches spans and grants no authority.
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
