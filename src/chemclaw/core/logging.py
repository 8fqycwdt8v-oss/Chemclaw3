"""Application-wide logging setup — one config-driven switch, plus what a log line has to carry.

Why this exists: before this, the app emitted essentially no logs, so troubleshooting a
stuck worker or a silent ELN sync meant reading raw tracebacks with no context. This gives a
single idempotent `configure_logging()` — called at each worker's entrypoint — that wires the
stdlib root logger to the configured level and format (`CHEMCLAW_LOG_LEVEL`,
`CHEMCLAW_LOG_FORMAT`), so verbosity is an ENV switch, not a code change. Application modules
just do `logging.getLogger(__name__)` and log; they never configure logging themselves.

**Two things a readiness review found missing, and they are not the same thing.**

*A log line had nothing to join on.* One `%`-format string, no JSON option, and no filter
injecting the correlation/actor/session `ContextVar`s — which already existed and were already
being read by audit, authorization and the connector headers. So an ordinary WARNING sat in a
stream beside the audit trail and the traces and could not be tied to either: the correlation id
that keys `audit_events` and the session that `chemclaw explain` walks were present in the
process and absent from the line. `ContextFilter` puts them on every record, and `log_json` makes
the whole record a machine-readable object rather than a string a log stack has to guess at.

*Nothing redacted a credential.* `core/db.py::_redact` strips a password from a DSN before it is
echoed — in exactly one place, which is the tell that the concern is real and unsystematised. A
DSN, a bearer token or an API key reaches a log through the ordinary routes: an exception message,
an httpx error, a `repr` of a config object. `SecretRedactingFilter` scrubs them everywhere.

**What is deliberately *not* redacted: the audit trail's arguments.** `SECURITY.md` states plainly
that the trail records each tool call's arguments, that they are user free text and may contain PII
or confidential chemistry, and that this is *intentional* — the trail exists to be an attributable
"who did what to which inputs" record. Redacting that would break the very thing it is for.
The row asked for redaction and the honest reading of it is credentials, which have no such
justification and which nothing was protecting.
"""

import json
import logging
import os
import re
from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.session_context import get_current_session_id

if TYPE_CHECKING:
    # Annotations only. The SDK is imported *inside* `configure_telemetry` at runtime, because this
    # module is what every entrypoint imports first and telemetry is off by default — and because
    # "the extras are missing" has to be catchable there rather than at import of the kernel.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter


def configure_logging() -> None:
    """Configure the root logger from config (level + format).

    Safe to call more than once: `force=True` replaces any existing handlers, so a second
    call (e.g. a test, or both workers in one process) re-applies the configured settings
    rather than stacking duplicate handlers.
    """
    logging.basicConfig(
        level=settings.log_level.upper(),
        format=settings.log_format,
        force=True,
    )
    # The filters go on the *handlers* rather than on a logger, because a filter attached to a
    # logger is not consulted for records that propagate up from a child — and every module here
    # logs through `getLogger(__name__)`, so almost every record is a propagated one. On the
    # handler, nothing reaches an output stream unfiltered.
    # One filter pair, constructed once and shared. `SecretRedactingFilter.__init__` walks the
    # connector registry off disk, so a pair per handler would repeat that work — and, when the
    # registry is broken, would log the ERROR and increment the failure counter once per handler
    # per call rather than once per startup, which `core/metrics.py` says it means.
    context, redaction = ContextFilter(), SecretRedactingFilter()
    for handler in _handlers_that_reach_an_output_stream():
        # `force=True` above resets the *root's* handlers, so a second `configure_logging()` starts
        # them clean — but a non-propagating logger's handlers are not ours to reset and would
        # otherwise accumulate a pair per call, running redaction N times per record on the front
        # door's hot path. Measured 2 -> 4 -> 6 filters over three calls before this guard.
        if not any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
            handler.addFilter(context)
            handler.addFilter(redaction)
        # The filter is fail-open by design, so a record it could not redact still reaches
        # logging's own error path with its original text. That path prints to stderr; this makes
        # it print a redacted copy. Unconditional because it is idempotent by construction — see
        # the function.
        _install_redacting_handle_error(handler, redaction)
        if settings.log_json:
            handler.setFormatter(JsonFormatter())


def _handlers_that_reach_an_output_stream() -> list[logging.Handler]:
    """Every handler a record can reach — the root's, plus any non-propagating logger's own.

    "Put the filter on the root handler" is only complete while every record propagates to the
    root, and the front door is the one process where that is false. It is started as
    `exec uvicorn ... --factory` with no `--log-config`, so uvicorn installs its own dictConfig
    first and gives `uvicorn` a handler with `propagate: false`. `uvicorn.error` — which logs every
    unhandled ASGI exception with `exc_info`, i.e. exactly the records that carry a DSN or an auth
    header — then reaches an output stream that this module had never touched: unredacted,
    uncorrelated, and in plain text even under `CHEMCLAW_LOG_JSON=true`.

    `core/worker_http.py` and `connectors/server_entry.py` avoid this by passing `log_config=None`,
    but that only helps a process we start ourselves in Python. Sweeping the manager here covers
    the entrypoint's `exec uvicorn` as well, without it having to know this module exists.

    **The sweep is one-shot, and an earlier version of this docstring claimed more than that.** It
    said the sweep also covers "any future library that configures its own logger", which is false:
    it walks `logging.Logger.manager` once, at `configure_logging()` time, so a non-propagating
    logger created *after* that call is never reached. What makes it work for uvicorn is an
    ordering fact rather than a general property — `uvicorn.Config.__init__` calls
    `configure_logging()`, which runs `dictConfig`, before the app factory this module is
    configured from. Measured on uvicorn 0.51.0: `uvicorn.error` exists with `propagate == False`
    before the factory runs, and an end-to-end run under `CHEMCLAW_LOG_JSON=true` shows a DSN
    password redacted in its traceback. A library that configures a logger later needs its own
    call, or this sweep needs to become a `logging.setLoggerClass` hook.
    """
    handlers: list[logging.Handler] = list(logging.getLogger().handlers)
    # `logging.lastResort` is neither a root handler nor any logger's own, and it is what a
    # non-propagating logger with no handlers of its own falls back to — an ordinary library shape,
    # and the same shape the rest of this sweep exists for. Measured: such a logger printed
    # `token=<value>` to stderr through it, unfiltered, on a *successful* emit rather than on any
    # error path. Swept here so the fallback carries the same redaction as everything else.
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    # Snapshot under the logging module's own lock. `loggerDict` is mutated by `getLogger()`, and
    # this runs in the app factory while worker startup, a lazy connector import or OTel's first use
    # may be creating loggers on another thread — iterating the live view raised
    # `RuntimeError: dictionary changed size during iteration` in 64 of 4000 measured attempts, and
    # the raise would abort `configure_logging()` with filters attached to only some handlers.
    # `list()` of the values view is a single C-level copy that does not release the GIL, so it
    # cannot observe a concurrent insertion mid-iteration. A comprehension over the live view can,
    # and did.
    known = list(logging.root.manager.loggerDict.values())
    for existing in known:
        # `PlaceHolder` entries are not loggers and carry no handlers.
        if isinstance(existing, logging.Logger) and not existing.propagate:
            handlers.extend(existing.handlers)
    return handlers


def _redacted_for_diagnostic(value: object, extra_secrets: tuple[str, ...]) -> str:
    """One field of logging's own error diagnostic, rendered and scrubbed, never raising.

    `repr` for a non-string, because that is what `handleError` would have printed anyway, and a
    bare `***` if even rendering raises: this runs *inside* logging's error path, on a record that
    has already failed to render once, so a `msg` whose `__str__` raises or an argument with a
    hostile `__repr__` is the expected input rather than an exotic one. A diagnostic that cannot be
    produced safely must not become a second exception in the handler that was reporting the first.
    """
    try:
        return redact_secrets(value if isinstance(value, str) else repr(value), extra_secrets)
    except Exception:
        return _REDACTED


def _install_redacting_handle_error(
    handler: logging.Handler, redaction: "SecretRedactingFilter"
) -> None:
    r"""Bind a `handleError` on `handler` that scrubs the record before stderr sees it.

    **The leak this closes.** `SecretRedactingFilter.filter` is deliberately fail-open: a record it
    cannot process is kept and passed on, because a silently dropped log line is worse than a
    malformed one. That stays exactly as it is — but its own docstring names the consequence, which
    nothing acted on: the record continues carrying its **original** `msg` and `args`. A record the
    filter could not render is one `Formatter.format` cannot render either, so `Handler.emit`
    raises and `Handler.handleError` runs. Read from CPython 3.11.15, `handleError` writes

        'Message: %r\nArguments: %s\n' % (record.msg, record.args)

    straight to `sys.stderr` — the pre-redaction message *and* the pre-redaction arguments, which
    is precisely where a credential lives (`logger.info("dsn=%s", dsn)` keeps the DSN in `args`
    until format time). `logging.raiseExceptions` defaults to True, so this path is live in every
    deployment, and it is reached by the ordinary malformations the filter was hardened to survive
    rather than by anything unusual.

    **Not `logging.raiseExceptions = False`.** That is the tempting one-liner and it is the wrong
    fix: it closes the leak by silencing *every* handler diagnostic in the process — a failing
    `emit`, a broken formatter, a closed stream all stop being reported anywhere at all. It trades
    one credential for a permanent, process-wide blind spot over the logging stack itself, which is
    the component you most need to be able to see fail. Redacting the two fields the diagnostic
    prints keeps the diagnostic.

    **Idempotent by construction**, because `configure_logging()` is documented safe to call more
    than once: the bound function delegates to `type(handler).handleError` — the class
    implementation — never to whatever this attribute held before. A second call therefore rebinds
    an equivalent function instead of stacking a wrapper around the first, and a `Handler` subclass
    with its own `handleError` still gets its own behaviour.
    """

    def handle_error(record: logging.LogRecord) -> None:
        """Print logging's own diagnostic for `record` with its credentials removed.

        **Nothing here may raise, and the diagnostic must print even if the scrubbing fails.**
        `Handler.handleError` is the one method in the logging stack that is defensive to the point
        of re-raising only `RecursionError`, because anything it lets escape surfaces at the
        application's own `logger.info(...)` line — logging crashing its caller. The first version
        of this wrapper reintroduced exactly that: `record.args` was treated as a tuple *or*
        anything-else-with-`.items()`, so a bare string or a `Mapping`-registered object without
        `items` raised `AttributeError` out through `emit` into the caller, **and** swallowed the
        diagnostic on the way. That is the property `SecretRedactingFilter.filter` spends a
        paragraph guaranteeing, given up one level down.

        So: the scrub is attempted, any failure drops the arguments rather than propagating, and
        the delegation sits outside the `try` where it always runs.

        The token names are read per call rather than captured at install time. Captured, a second
        `configure_logging()` left one handler with two inventories — the filter kept the first and
        this closure held the second — with the *stale* one on the ordinary path.
        """
        msg, args = record.msg, record.args
        try:
            record.msg = _redacted_for_diagnostic(msg, redaction._connector_token_envs)
            if isinstance(args, tuple):
                record.args = tuple(
                    _redacted_for_diagnostic(arg, redaction._connector_token_envs) for arg in args
                )
            elif isinstance(args, Mapping):
                record.args = {
                    key: _redacted_for_diagnostic(value, redaction._connector_token_envs)
                    for key, value in args.items()
                }
            elif args is not None:
                record.args = _redacted_for_diagnostic(args, redaction._connector_token_envs)
        except Exception:
            # An unscrubbable argument is dropped, never printed: this runs because formatting
            # already failed once, so the value is exactly the kind that might carry a secret.
            record.args = None
        try:
            type(handler).handleError(handler, record)
        finally:
            # Restored, because the record is not ours. The logger hands the same object to every
            # handler in turn, so a later handler must format the caller's own values — a `%d`
            # argument left replaced by its rendered text would spread one handler's failure to
            # all of them.
            record.msg, record.args = msg, args

    handler.handleError = handle_error  # type: ignore[method-assign]


# Set once per process: `metrics.set_meter_provider` refuses a second call and warns, and the only
# thing this flag has to be right about is not producing that warning on a re-entry.
_NOOP_METERS_INSTALLED = False


def _install_noop_meter_provider() -> None:
    """Make "telemetry off" mean a no-op provider, not the *absence* of one.

    **This is the fix for the front door's memory leak**, and the distinction it turns on is the
    whole finding: with no meter provider set, the OpenTelemetry API does not discard instrument
    calls — it *proxies* them, and it keeps every proxy forever so it can back them if a provider
    arrives later. `_ProxyMeterProvider._meters` and `_ProxyMeter._instruments` are module-level
    lists that only ever grow — `_ProxyMeterProvider` in the `opentelemetry.metrics` internals.

    MAF creates one duration histogram per exposed MCP function, and this system rebuilds its
    connector tool surface every turn — so a turn with telemetry *off* leaked 35 `_ProxyMeter`s,
    35 `_ProxyHistogram`s, 70 locks and 35 lists, permanently. Measured with the front door's own
    load (`chemclaw.cli.leak_probe`): **+178 live objects and +20.7 KB of RSS per turn before,
    +3.3 objects and +2.7 KB after** — and what remains is the session LRU filling toward its cap,
    which is bounded by construction. Over the 162-round soak that is the 549 MB → 1,066 MB the
    review recorded and could not name.

    Idempotent by a module flag rather than by asking the API what is installed, because
    `get_meter_provider()` has the side effect of resolving and caching one.
    """
    global _NOOP_METERS_INSTALLED
    if _NOOP_METERS_INSTALLED:
        return
    from opentelemetry import metrics

    metrics.set_meter_provider(metrics.NoOpMeterProvider())
    _NOOP_METERS_INSTALLED = True


# The `service.name` every span is attributed to when the deployment does not say otherwise.
#
# Not a `Settings` field, and that is the one place this module does not follow "config, never a
# literal": OpenTelemetry already owns this value under the standard `OTEL_SERVICE_NAME`, which
# `_build_tracer_provider` honours, and a `CHEMCLAW_OTEL_SERVICE_NAME` beside it would be a second
# spelling of one thing — two answers to "which service is this trace from". A deployment
# that wants the front door and each worker to appear as separate services sets `OTEL_SERVICE_NAME`
# per Deployment; unset, every Chemclaw process reports as one service, which is what the previous
# bootstrap did too (it reported them all as `agent_framework`).
_DEFAULT_SERVICE_NAME = "chemclaw"

# Set once per process, for the same reason `_NOOP_METERS_INSTALLED` is: `trace.set_tracer_provider`
# refuses a second call and warns, and — worse — building a second provider would start a second
# `BatchSpanProcessor` export thread and a second gRPC channel that the API then silently discards.
# A flag rather than asking the API what is installed, because `get_tracer_provider()` has the side
# effect of resolving and caching one.
_TRACING_INSTALLED = False


def _build_tracer_provider(exporter: "SpanExporter") -> "TracerProvider":
    """Assemble the span pipeline: a resource that names the service, batching, then `exporter`.

    Separate from `configure_telemetry` because installing a provider is a once-per-process global
    act while *building* one is an ordinary object graph — so this half can be driven by a test
    against a real in-memory exporter, which is the only way to prove that a span emitted through
    the provider reaches the exporter carrying the resource attributes a collector groups by.

    `BatchSpanProcessor` rather than `SimpleSpanProcessor` for the reason the previous bootstrap
    also chose it: a synchronous export sits on the caller's thread, and this pipeline is fed from
    the event loop that serves every SSE stream.
    """
    from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            # The standard variable wins when a deployment sets it; passed explicitly rather than
            # left to `Resource.create`'s own env detection so the two halves of "what names this
            # service" are in one expression instead of split across a `setdefault` elsewhere.
            SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME") or _DEFAULT_SERVICE_NAME,
            # The Git SHA the pod was built from (`CHEMCLAW_DEPLOYMENT_REVISION`), the same value
            # every audit record is stamped with — so a trace and the audit record of the same turn
            # name the same build.
            SERVICE_VERSION: settings.deployment_revision,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def configure_telemetry() -> None:
    """Install the process's OpenTelemetry span pipeline; install no-op meters when it is off.

    Off unless `CHEMCLAW_OTEL_ENABLED=true`. When on, this builds a `TracerProvider` with an OTLP
    span exporter behind a `BatchSpanProcessor` and installs it as the global provider, so the
    first-party spans in `core/tracing.py` — the turn, the tool call, and the `traceparent` that
    joins a connector's work to the turn that asked for it — are actually exported. Called once
    per process at each worker's entrypoint, after `configure_logging`.

    **This used to be one line into MAF** (`agent_framework.observability.configure_otel_providers`)
    and that is why it is written out here: the OTel SDK and the OTLP exporter are declared
    dependencies *of this module*, but the code that turned them into a live pipeline belonged to a
    package the LangGraph rebuild removes. Nothing in `langchain`, `langgraph` or `langsmith`
    replaces it, and — the part that makes this the phase's sharpest edge — **no test would have
    failed** if tracing had simply stopped: every span helper degrades silently to a no-op by
    design, which is right for a turn and wrong for a deployment. The tests beside this function
    exist to make the silence audible.

    **What went with MAF, and what answers the same question now.** MAF's chat-client
    instrumentation recorded `gen_ai.client.token.usage` — an OTel *metric*, a histogram, despite
    the name reading like a span attribute — labelled by request model, response model, provider and
    token type. Measured across the installed venv it is emitted by exactly one module, MAF's own
    `observability`; nothing in `langchain`, `langgraph` or `langsmith` emits it. That metric is not
    fabricated here. What replaces it is a different signal in the pipeline that *is* here:
    `_instrument_llm_calls` puts one span per model call, carrying `llm.token_count.*` and
    `llm.model_name`, behind `CHEMCLAW_OTEL_LLM_SPANS`. `core/metrics.py`'s Prometheus token
    counters are unchanged and still carry `profile` rather than model, deliberately (D-152).
    `docs/guides/runbook.md` says the same where an operator would look.

    **Traces only, deliberately, and the other two signals have homes.** Metrics are
    `core/metrics.py`'s Prometheus text surface, scraped per pod, because a trace is sampled and
    per-request and cannot answer "what is p95 right now" for an alert; logs are JSON on stdout
    (`JsonFormatter`), collected by the cluster's log stack. Installing OTLP pipelines for either
    would be a second, unread copy of a signal that already has a collector.

    **So "on" also installs the no-op meter provider**, not just "off" — see
    `_install_noop_meter_provider`. With no meter provider set, the OTel *API* proxies every
    instrument and retains the proxy forever, so a process that exports traces and never installs a
    meter provider leaks exactly as the disabled path did. One line covers both.

    **Idempotent.** The front door, the workers and the CLI each call this at their entrypoint, and
    tests call it repeatedly; a second call must not start a second export thread and a second gRPC
    channel that the API would then discard. The flag makes the second call a no-op.

    `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` **governs again**, and it is the same question it always
    governed: whether prompts and completions ride on a span. It lost its only consumer with MAF's
    instrumentation and spent a phase as a warned-about knob; `_instrument_llm_calls` gives it back
    rather than adding a second flag beside it, because `CHEMCLAW_OTEL_LLM_SPANS` is the only thing
    that puts content within reach. It still governs nothing when that is off — and the warning
    below still says so out loud in exactly that case, rather than letting an
    enabled-but-ineffective privacy switch read as an effective one.

    Raises:
        RuntimeError: `CHEMCLAW_OTEL_ENABLED=true` but the OpenTelemetry SDK / OTLP exporter is not
            installed — a directive message rather than the import error, for the admin who flips
            the flag on an install without the extras.
    """
    global _TRACING_INSTALLED
    if not settings.otel_enabled:
        _install_noop_meter_provider()
        return
    if _TRACING_INSTALLED:
        return
    # Bridge our one config value to the standard OTLP variable the exporter reads (F6-T5), so the
    # collector endpoint stays a single `CHEMCLAW_OTEL_ENDPOINT` like every other endpoint.
    # `setdefault`, so a deployment that sets the standard variable directly — or the per-signal
    # `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` — keeps winning, exactly as before.
    if settings.otel_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", settings.otel_endpoint)
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    except ImportError as exc:  # SDK/exporter extras not installed
        raise RuntimeError(
            "CHEMCLAW_OTEL_ENABLED=true but the OpenTelemetry SDK/OTLP exporter is not installed"
        ) from exc

    # No endpoint argument: the exporter resolves it from the standard variables itself, which is
    # what keeps `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, the headers and the protocol settings working
    # without this module re-implementing OTel's own configuration precedence.
    provider = _build_tracer_provider(OTLPSpanExporter())
    trace.set_tracer_provider(provider)
    _instrument_llm_calls(provider)
    _TRACING_INSTALLED = True
    _install_noop_meter_provider()
    if settings.otel_include_sensitive_data:
        _warn_about_sensitive_data()


def _warn_about_sensitive_data() -> None:
    """Say what `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` is doing — in *both* directions.

    **The dangerous direction is the one that used to be silent, and that asymmetry was the
    defect.** The flag spent a phase governing nothing, and this module warned about exactly that:
    an enabled-but-ineffective privacy switch reads as an effective one. Giving the flag a consumer
    back inverted which case needs saying. A deployment that set it while it was inert — and the
    config comment kept it precisely "because a deployment may still have it in its values file" —
    gets `CHEMCLAW_OTEL_LLM_SPANS` switched on by the shipped chart and starts exporting a chemist's
    question and the model's answer to the collector, with nobody having decided that in this
    release. A flag that changes meaning between versions has to announce the meaning it now has.

    Not an error, and not a refusal: a deployment is entitled to make this choice, and failing to
    start over a telemetry setting would be worse than the setting. What it is not entitled to is
    making it without noticing, so the line names the endpoint the content is going to.
    """
    logger = logging.getLogger(__name__)
    if not settings.otel_llm_spans:
        logger.warning(
            "CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA is set but has no effect while "
            "CHEMCLAW_OTEL_LLM_SPANS is off: no first-party span carries turn content, so there is "
            "nothing for it to govern"
        )
        return
    logger.warning(
        "CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA is set and CHEMCLAW_OTEL_LLM_SPANS is on: prompts "
        "and completions — a chemist's question and the model's answer — are being attached to "
        "spans and exported to %s. Whatever stores traces behind that collector now holds the "
        "class of data SECURITY.md describes for the audit trail, and must be covered by the same "
        "retention, access-control and PII policy. Unset it unless that was a decision somebody "
        "made for this release.",
        settings.otel_endpoint
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "the configured OTLP endpoint"),
    )


def _instrument_llm_calls(provider: Any) -> None:
    """Attach OpenInference's LangChain instrumentation, content suppressed unless asked.

    A span per model call, plus the chain and tool spans around it, over the same OTLP exporter the
    provider was just built with. Off unless `CHEMCLAW_OTEL_LLM_SPANS=true`, and a no-op then — the
    instrumentation is not imported at all, so a deployment that does not want it pays nothing.

    **Content is `otel_include_sensitive_data`'s decision, which is how that flag stops being
    dead.** It had exactly one consumer, the agent framework's own instrumentation, and this asks
    the identical question — so it gets the flag back rather than a second knob beside it. Off (the
    default) sets every `TraceConfig` hide flag, and `core/tracing.py`'s rule survives intact:
    identifiers and counts, never a question, an argument or an answer.

    **Suppression costs none of what this was added for**, which is why it is a default rather than
    a trade-off. Measured against a real compiled graph with a scripted model, scanning *every*
    exported attribute value for the question and answer text: unsuppressed, five attributes carry
    content (`input.value`, `output.value` and the three message contents); suppressed, **zero** —
    while `llm.token_count.prompt`/`.completion`/`.total` and `llm.provider` are byte-identical
    across the two runs. OpenInference's own `mask()` touches input, output, message, prompt,
    choice, embedding, tool and invocation-parameter keys and nothing else, which is why.

    **Not guarded against a second call**, deliberately: `configure_telemetry` already returns early
    on `_TRACING_INSTALLED`, and the instrumentor is a `BaseInstrumentor` singleton that logs
    "Attempting to instrument while already instrumented" rather than raising. A guard here would be
    a second answer to a question one flag already answers.

    Args:
        provider: The tracer provider built for this process, passed explicitly rather than read
            back from the global — the global is set one line above and reading it back would make
            this depend on that ordering.

    Raises:
        RuntimeError: `CHEMCLAW_OTEL_LLM_SPANS=true` but the instrumentation is not installed — the
            same directive message the SDK check raises, for the admin who flips the flag on an
            install without it.
    """
    if not settings.otel_llm_spans:
        return
    try:
        from openinference.instrumentation import TraceConfig
        from openinference.instrumentation.langchain import LangChainInstrumentor
    except ImportError as exc:
        raise RuntimeError(
            "CHEMCLAW_OTEL_LLM_SPANS=true but openinference-instrumentation-langchain is not "
            "installed"
        ) from exc
    LangChainInstrumentor().instrument(tracer_provider=provider, config=_trace_config(TraceConfig))


def _trace_config(trace_config: Any) -> Any:
    """The OpenInference `TraceConfig` for this deployment — everything hidden, or nothing.

    Two settings rather than eleven. Every hide flag is set together because they answer one
    question ("may turn content leave this pod?") and a deployment that could answer it differently
    per attribute would be one that had not answered it: a span carrying the *prompt* but not the
    completion is still a span carrying a chemist's question.

    The list is written out rather than derived from the dataclass's fields, because deriving it
    would silently adopt whatever a future version adds — including a field whose default is the
    permissive one. A new hide flag upstream should require a decision here, not inherit one.

    Args:
        trace_config: The `TraceConfig` class. Taken as an argument rather than imported here
            because the import is lazy in `_instrument_llm_calls` — the caller has already resolved
            it, and importing again would put a second `ImportError` site in the module for one
            dependency. It also leaves this a pure function of (settings, class), which is what lets
            `tests/test_llm_spans.py` check the flag set without building a tracer provider.

    Returns:
        The configuration to hand the instrumentor.
    """
    if settings.otel_include_sensitive_data:
        return trace_config()
    return trace_config(
        hide_inputs=True,
        hide_outputs=True,
        hide_input_messages=True,
        hide_output_messages=True,
        hide_input_text=True,
        hide_output_text=True,
        hide_input_images=True,
        hide_prompts=True,
        hide_choices=True,
        hide_llm_invocation_parameters=True,
        hide_llm_tools=True,
        # The embedding trio was missing from the first version of this list and
        # `tests/test_llm_spans.py::test_every_hide_flag_is_set_together` is what found it — which
        # is the whole reason that test compares against the dataclass's fields rather than against
        # a list written twice. `hide_embeddings_text` is the one that mattered: the text being
        # embedded is a chemist's question or a note's body, so leaving it unset would have put
        # content on a span under the configuration whose entire purpose is that it does not.
        # `hide_embedding_vectors` and `hide_embeddings_vectors` are upstream's spelling and its
        # alias; both are set because either might be the one a given release reads.
        hide_embedding_vectors=True,
        hide_embeddings_vectors=True,
        hide_embeddings_text=True,
    )


# The settings whose *values* must never appear in a log line. Redaction works by matching the
# actual secret this process holds rather than by guessing at token-shaped strings: a pattern like
# `[A-Za-z0-9]{32,}` both misses a short key and mangles a molecule id, while an exact value match
# catches the secret no matter which route put it there — an exception message, an httpx error, a
# `repr` of a settings object — and cannot false-positive on anything else.
#
# Listed rather than derived from a name pattern (`*_token`, `*_secret`), because deriving would
# silently include `calc_server_token_env` and `budget_max_tokens_per_user` — a variable *name* and
# an integer — and silently *exclude* the next secret whose name does not match. A list is one line
# per addition and is visible in review, which is what a credential inventory should be.
_SECRET_SETTINGS = (
    "llm_api_key",
    # The fallback endpoint's own credential, absent from this list until 2026-08-26 — the one
    # credential in `Settings` that no redaction covered. It is a separate field precisely so a
    # second endpoint can hold a *different* key, so matching the primary's value would not have
    # caught it.
    "llm_fallback_api_key",
    "temporal_api_key",
    "postgres_dsn",
    "postgres_migration_dsn",
    "session_store_dsn",
    "note_webhook_secret",
    # Not a credential to an external system, which is why it was missed — but it is the HMAC key
    # `agent/framing.py` derives `ENVELOPE_TAG` from, and the agent instructions say only an
    # envelope carrying exactly that tag marks retrieved content as data. Anyone who learns it and
    # can place text into any retrieval source closes the envelope from inside, and their text is
    # read as instructions: it defeats the prompt-injection mitigation it exists to make durable.
    "framing_envelope_secret",
)

# The git push credential for the knowledge-sync sidecar (`deploy/knowledge-sync.sh`). It has no
# `Settings` field — nothing in this process reads it as config, only the sidecar script does —
# but `_helpers.tpl` ranges over every secret key for every component, so it sits in this process's
# environment regardless. A `Settings` field would be a config seam for a value nothing here
# configures; reading the one environment variable a redaction inventory actually needs is simpler
# than inventing one.
_KNOWLEDGE_REPO_TOKEN_ENV = "CHEMCLAW_KNOWLEDGE_REPO_TOKEN"

# Credential variable names contributed at runtime by something that reads its own configuration
# rather than this process's settings — today, a data source whose manifest names the environment
# variables holding its warehouse credentials (`ingest.eln.warehouse.connect`).
#
# **Names, never values**, so this stays consistent with everything above it: `_secret_values` reads
# `os.environ` fresh on every call, and caching a value here would keep redacting a rotated
# credential's *old* text while the new one flowed through unredacted.
#
# A set rather than a `Settings` field because the whole point of the manifest seam is that
# attaching a source costs no core edit — a source that had to add a config field to be redactable
# would have given that property back. Registration is idempotent and additive; nothing removes.
_RUNTIME_SECRET_ENVS: set[str] = set()


def register_secret_env(name: str) -> None:
    """Add an environment variable to the redaction inventory for the life of this process.

    For a credential this process holds but does not configure: a manifest names the variable, the
    thing that reads it says so here, and every log line from then on has its value scrubbed. Call
    it where the variable is read, so the registration cannot drift from the use.
    """
    if name:
        _RUNTIME_SECRET_ENVS.add(name)


# Below this length a "secret" is more likely to be a placeholder, an empty default, or a string
# that occurs in ordinary prose — redacting it would corrupt every line containing that substring.
_MIN_REDACTABLE = 8

_REDACTED = "***"

# A credential carried in a URL's userinfo — `scheme://user:secret@host`, which is how a token
# reaches a git remote and how a password reaches a DSN. Matched structurally rather than by value
# because this is the one credential class the inventory above *cannot* cover: a git helper, a
# sidecar or a remote configured outside this process holds the token, so it is nowhere in
# `os.environ` here and the substring pass has nothing to look for. The user is kept and only the
# secret replaced, so a redacted line still says which remote and which principal failed.
#
# Two spellings, because the password form is not the common one for a token: `scheme://user:pw@`
# carries a principal *and* a secret, while `scheme://token@` is the whole userinfo and is how a
# PAT reaches a git remote (scheme, then the token, then `@`, then the host). Only the first was
# matched,
# so the more common credential form passed through verbatim. The user is still kept in the
# two-part form, so a redacted line says which remote and which principal failed; in the one-part
# form there is no principal to keep and the whole of it is the credential.
_URL_USERINFO = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.\-]{0,63}://)([^/\s:@]{0,512})(?::([^/\s@]{0,512}))?@"
)


# Credentials this process does **not** hold, matched by shape rather than by value.
#
# The value inventory above can only redact what this process configured, which leaves out the
# whole class of credentials that merely *pass through*: the caller's own Entra bearer token, a
# third-party PAT quoted in an upstream error, a warehouse DSN in libpq `key=value` form (the
# userinfo pattern only sees the URL spelling). Measured, eleven realistic shapes reached the
# stream verbatim.
#
# Pattern matching was rejected once, for a good reason — a false positive corrupts a log line, and
# an over-eager rule that ate molecule ids or note slugs would be worse than the leak. The first
# version of these rules proved the point on itself: `[^\s&,;"']{8,}` after a key name ate the
# *source lines of this repository*, which is the one text guaranteed to appear inside the
# tracebacks the whole mechanism exists to protect —
#
#     access_token = response.json().get("access_token")   ->  access_token = ***"access_token")
#     api_key=settings.llm_api_key or _KEYLESS_PLACEHOLDER  ->  api_key=*** or _KEYLESS_PLACEHOLDER
#     Basic authentication rejected by the proxy            ->  Basic *** rejected by the proxy
#
# and `password=None)` became `password=***`. The innocent-content test passed throughout, because
# it pinned *identifiers* (SMILES, note slugs, ADR ids) and no source line and no prose.
#
# So a key-name anchor is not enough on its own: the value has to look like a credential too. Two
# extra requirements do that, and they are what separates a token from an expression:
#
# 1. `_OPAQUE` excludes the characters code and prose put there — quotes, parentheses, commas,
#    semicolons — so `response.json().get(` and `settings.llm_api_key or` cannot match.
# 2. A digit is required somewhere in the value. Real credentials are drawn from a random alphabet
#    and effectively always contain one; English words and Python attribute paths do not.
#
# The cost is a token of pure letters (rare, and still covered by the value inventory when this
# process holds it). That trade is the right way round: an unreadable traceback is a permanent loss
# of the incident evidence, while this floor is a backstop under the inventory, not the mechanism.
#
# `Basic` is dropped entirely. It is an ordinary English word, and unlike `Bearer` it is not
# followed by anything with usable structure.
#
# Every tail is *bounded*. Unbounded `{8,}` made the JWT rule quadratic — each `-eyJ` in the input
# is a fresh word-boundary start whose tail rescans the remainder — measured at 46.7 ms for 10 KB
# of `-eyJ` rising to 11.78 s for 160 KB. This runs inside `handler.handle()`, which holds the
# logging lock, so that is a denial of service on every thread's logging, reachable by anything that
# can get text into a log line.
#
# `(?P<keep>...)` is the part a reader still needs — the label, so a redacted line says *which*
# credential failed rather than becoming an anonymous `***`.
#
# The characters a credential is made of. No quotes, parens, commas or semicolons: those are what a
# repr, a call expression or a libpq string puts around a value, never inside one.
_OPAQUE = r"[A-Za-z0-9_\-.~+/=]"
# Not preceded by a token character. `\b` is not enough: it matches between `-` and `e`, so every
# `-eyJ` in a hostile string is a fresh start position whose tail rescans the remainder — which is
# what made the JWT rule quadratic. A real credential is preceded by a space, a quote, `=` or `:`,
# never by another token character, so this costs nothing and removes the amplifier.
_NOT_MID_TOKEN = r"(?<![A-Za-z0-9_\-.])"
# "Contains a digit" — the cheap discriminator between a token and an identifier.
#
# **Bounded, for the same reason `_NOT_MID_TOKEN` exists.** Written as `_OPAQUE*\d` this was the
# JWT rule's defect wearing a different hat: `_OPAQUE` matches no whitespace, so a whitespace-free
# run of `password=` gives an anchor every nine bytes and each anchor's lookahead rescans the whole
# remainder — quadratic, and reached *unauthenticated* through the uvicorn access log, which puts
# the raw request URL into a line this filter redacts. Measured before the bound: 18 KB → 0.5 s,
# 36 KB → 2.0 s, 72 KB → 8.1 s (2x input, 4x time), and end to end a 115 KB request line stalled
# the pod for 21 s — long enough for two consecutive readiness failures on the shipped chart, with
# the stdlib logging lock held the whole time.
#
# The bound is what makes the work linear: every anchor scans at most 255 characters instead of the
# rest of the line. It costs nothing real — a credential longer than 255 characters with its first
# digit past position 255 is not a shape any of these rules is written for, and the rules' own
# `{6,255}` / `{8,255}` tails already say so.
_HAS_DIGIT = r"(?=" + _OPAQUE + r"{0,255}\d)"

_STRUCTURAL_SECRETS: tuple["re.Pattern[str]", ...] = (
    # GitHub tokens: `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` (classic, 36 chars) and the fine-grained
    # `github_pat_` form. Both are vendor-assigned prefixes that occur in nothing else, so these
    # two need no digit requirement — the prefix alone is decisive.
    re.compile(_NOT_MID_TOKEN + r"gh[pousr]_[A-Za-z0-9]{20,255}"),
    re.compile(_NOT_MID_TOKEN + r"github_pat_[A-Za-z0-9_]{20,255}"),
    # Anthropic and OpenAI keys, including the project-scoped spellings.
    re.compile(_NOT_MID_TOKEN + r"sk-(?:ant|proj|svcacct)-[A-Za-z0-9_\-]{16,255}"),
    re.compile(_NOT_MID_TOKEN + r"sk-[A-Za-z0-9]{32,255}"),
    # A JWT — three base64url segments separated by dots, the first starting `eyJ` because a JOSE
    # header always begins `{"`. This is the inbound Entra access token's shape.
    re.compile(
        _NOT_MID_TOKEN + r"eyJ[A-Za-z0-9_\-]{8,1024}\.[A-Za-z0-9_\-]{8,4096}\."
        r"[A-Za-z0-9_\-]{1,1024}"
    ),
    # libpq key/value connection strings and the environment spelling: `password=`, `PGPASSWORD=`,
    # and the `repr` of a config dict (`'password': '...'`). The URL form is `_URL_USERINFO`'s.
    re.compile(
        r"(?P<keep>\b(?:PG)?PASSWORD[\"']?\s*[=:]\s*[\"']?)" + _HAS_DIGIT + _OPAQUE + r"{6,255}",
        re.IGNORECASE,
    ),
    # A credential in a query string, a header, or a rendered dict. Anchored on the key name so the
    # bare words "token" or "secret" in prose cannot trigger it, and on the value's shape so an
    # assignment in a source line cannot.
    re.compile(
        r"(?P<keep>\b(?:access_token|refresh_token|api[_-]?key|client_secret)"
        r"[\"']?\s*[=:]\s*[\"']?)" + _HAS_DIGIT + _OPAQUE + r"{8,255}",
        re.IGNORECASE,
    ),
    # `Authorization: Bearer <opaque>` / `Token <opaque>` — the JWT rule covers the structured case;
    # an opaque bearer has no internal structure, so the scheme is the anchor and the digit
    # requirement is what keeps "Bearer token was rejected" intact.
    re.compile(r"(?P<keep>\b(?:Bearer|Token)\s+)" + _HAS_DIGIT + _OPAQUE + r"{16,4096}"),
)


def _redact_structural(match: "re.Match[str]") -> str:
    """Replace a structurally-matched credential, keeping the label that names it."""
    return f"{match.groupdict().get('keep') or ''}{_REDACTED}"


def _redact_userinfo(match: "re.Match[str]") -> str:
    """Replace a URL's credential, keeping the scheme and (where there is one) the user."""
    scheme, first, second = match.group(1), match.group(2), match.group(3)
    if second is None:
        return f"{scheme}{_REDACTED}@"
    return f"{scheme}{first}:{_REDACTED}@"


def _dsn_password(value: str) -> str:
    """The password inside a `scheme://user:password@host` DSN, or `""`.

    Extracted so the redaction inventory and the published-defaults set below agree by
    construction: they must decide the same thing about the same string, and two spellings of
    "the password part" is exactly how they would stop.
    """
    if "://" not in value or "@" not in value:
        return ""
    userinfo = value.split("://", 1)[1].split("@", 1)[0]
    return userinfo.split(":", 1)[1] if ":" in userinfo else ""


@lru_cache(maxsize=1)
def _published_values() -> frozenset[str]:
    """Every secret-shaped value this repository *commits* — and therefore does not have to hide.

    A value anyone can read in `core/config/` is not a credential, and redacting it does nothing
    but corrupt logs. The dev Postgres DSN's password is the literal string `chemclaw`, so
    treating it as a secret replaced the product's own name with `***` in every dev and CI log
    line that happened to contain it — including messages with nothing to do with the database.

    **The derived values matter as much as the defaults themselves**, which is the half a first
    attempt missed. `tests/conftest.py` repoints `postgres_dsn` at an isolated schema, so in CI
    the DSN is *not* the shipped default and is redacted correctly — while the password inside it
    still is `chemclaw`. Comparing only whole values passed locally, where no Postgres means no
    repointing, and failed in CI. So the defaults' passwords are published too.

    Cached: `model_fields` defaults are fixed at import and cannot change at runtime.
    """
    published: set[str] = set()
    for name in _SECRET_SETTINGS:
        field = type(settings).model_fields.get(name)
        default = _secret_text(getattr(field, "default", None))
        if default:
            published.add(default)
            if password := _dsn_password(default):
                published.add(password)
    return frozenset(published)


def _secret_text(value: object) -> str:
    """The string inside a settings value, whether it is a `str` or a `SecretStr`, else `""`.

    **The type change that would have silently disabled this module.** Nine `Settings` fields are
    redacted by exact value match, and both readers here tested `isinstance(value, str)` — which a
    `SecretStr` is not. Converting the credentials to `SecretStr`
    (`D-2026-08-26-a-credential-is-a-type-not-a-convention`) would therefore have skipped every one
    of them: `str(SecretStr("k"))` is `"**********"`, so the filter would have gone on matching
    asterisks against log lines and reporting success. Two guarantees that look like one, where the
    stronger-looking one turns the other off.

    `str` is still accepted, and not only for the DSNs: a test that monkeypatches a plain string
    onto the settings object must still be redacted, because a filter that only covers correctly
    typed values covers less than the one it replaced.
    """
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value if isinstance(value, str) else ""


def redact_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Return `text` with every credential this process can recognize replaced by `***`.

    The redaction `SecretRedactingFilter` applies to a log line, exposed so anything that
    *persists* an error message can apply the same one. The PR-gate is the case that forced it:
    a failed submission stores git's stderr in `note_proposals.reason`, a compliance table nobody
    prunes, and it bounded that text by truncating it — but truncation is not redaction, and a
    realistic token-bearing push failure measures well under any length worth keeping, so the
    credential was stored verbatim and in full.

    `extra_secrets` is for values a caller resolved itself (the filter's per-connector bearer-token
    variable names), keeping the lazy `connectors` import out of this module.
    """
    redacted = text
    for secret in _secret_values(extra_secrets):
        redacted = redacted.replace(secret, _REDACTED)
    # A *callable* replacement, not a `\1`-style template. A template is compiled lazily by the
    # `re` machinery on first use, and that compilation does `import re` — on the logging path,
    # which `tests/test_filtering_a_record_never_imports_anything` forbids for the reason recorded
    # there: an import from inside a filter re-entered the filter under Temporal's sandbox and
    # wedged the worker. The test caught this the first time it ran.
    redacted = _URL_USERINFO.sub(_redact_userinfo, redacted)
    for pattern in _STRUCTURAL_SECRETS:
        redacted = pattern.sub(_redact_structural, redacted)
    return redacted


def _secret_values(connector_token_envs: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The distinct secret values this process actually holds, longest first.

    Longest first so a DSN is redacted before the password inside it — replacing the shorter one
    first would leave a mangled DSN whose remaining half still names the host and user.

    `connector_token_envs` is `SecretRedactingFilter`'s resolved list of per-connector bearer-token
    variable names (`manifest.auth.token_env`) — passed in rather than looked up here, so this
    function stays free of the lazy `connectors` import that resolving them requires (see the
    filter's `__init__`). Read fresh from `os.environ` on every call, exactly like the `Settings`
    values below: none of these are expected to rotate mid-process, but nothing here assumes it.
    """
    values = set()
    published = _published_values()

    def _consider(candidate: str) -> None:
        """Add `candidate` unless it is too short to match safely, or published in this repo."""
        if len(candidate) >= _MIN_REDACTABLE and candidate not in published:
            values.add(candidate)

    for name in _SECRET_SETTINGS:
        value = _secret_text(getattr(settings, name, ""))
        _consider(value)
        # A DSN's password is also worth matching on its own: libpq accepts several spellings and a
        # connection error may quote only the credential rather than the whole string. Considered
        # independently of the DSN, because the two can differ in whether they are published: a
        # schema-scoped test DSN is not the shipped default, but the password inside it still is.
        _consider(_dsn_password(value))
    for env_name in (
        _KNOWLEDGE_REPO_TOKEN_ENV,
        *connector_token_envs,
        *sorted(_RUNTIME_SECRET_ENVS),
    ):
        value = os.environ.get(env_name, "")
        if len(value) >= _MIN_REDACTABLE:
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


# Renders `exc_info` for `SecretRedactingFilter`. Module scope so the logging path constructs
# nothing per record; a bare `Formatter` because only its `formatException` is used.
_EXC_RENDERER = logging.Formatter()


class SecretRedactingFilter(logging.Filter):
    """Replace any configured secret's value with `***` in a record's rendered message.

    A filter rather than a formatter because a deployment may install its own formatter, and
    redaction must not be something a formatting choice can switch off. It runs on the *rendered*
    message so a secret passed as a `%s` argument is caught too — `logger.info("dsn=%s", dsn)`
    keeps the secret in `record.args` until formatting, which is exactly how one escapes a filter
    that only inspects `record.msg`.

    `_SECRET_SETTINGS` is the inventory this class's own module docstring calls out as "visible in
    review" — and two real credentials the process holds sat outside it structurally: the
    knowledge-sync git token (no `Settings` field; `_secret_values` reads it directly) and each
    connector's bearer token, resolved from `manifest.endpoint.auth.token_env` per enabled HTTP
    connector. The latter needs `chemclaw.connectors`, which `core` may not import at module scope
    (`tests/test_layering.py`) — the same constraint `ContextFilter.__init__` already solved for
    `chemclaw.agent`, and for the same reason: resolved **once, here in `__init__`**, a safe,
    single process entrypoint, never from `filter()`, which runs on every record and could be
    reached while another module is mid-import.
    """

    def __init__(self) -> None:
        """Resolve the connector bearer-token variable names once, tolerating discovery failure.

        A broken connector manifest is a real misconfiguration and every other consumer of
        `connectors.registry.enabled()` fails loudly on it — but that failure belongs to whichever
        of those consumers hits it first, not to logging setup. So this degrades to redacting
        nothing *extra* rather than blocking `configure_logging()`, and says so at **ERROR**, under
        the marker `degraded[log_redaction]` and on a counter: a redaction inventory that quietly
        stopped covering connectors would be a worse outcome than a boot that proceeds without
        them, and it is the one *security* degradation in this file. The comment beside the handler
        argues the severity in full. (This sentence said "at WARNING" for one commit after the
        handler below stopped doing that — prose is evidence about what its author believed.)
        """
        super().__init__()
        self._connector_token_envs: tuple[str, ...] = ()
        try:
            from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint
            from chemclaw.connectors.registry import enabled

            self._connector_token_envs = tuple(
                manifest.endpoint.auth.token_env
                for manifest in enabled()
                if isinstance(manifest.endpoint, HttpEndpoint)
                and isinstance(manifest.endpoint.auth, BearerAuth)
            )
        except Exception:
            # ERROR and counted, and the one degradation in this file that is a *security*
            # degradation rather than a functional one: the process keeps logging, and keeps
            # logging connector bearer tokens in the clear for its whole lifetime. The state is
            # unbounded in time and its trigger is correlated with its consequence — what breaks
            # this resolution is a bad connector manifest, and a bad connector manifest is exactly
            # what produces the connector failures whose tracebacks carry the token. A WARNING in
            # container startup output is the line nobody reads; `degraded[log_redaction]` is the
            # stable marker to alert on. Safe to log from inside a filter's constructor: the filter
            # is not installed yet, so there is no recursion, and `record_metric` swallows anything
            # the registry could raise.
            degraded(
                logging.getLogger(__name__),
                "log_redaction",
                "could not resolve connector bearer-token env names for log redaction; "
                "connector credentials will NOT be scrubbed from log lines for the life of "
                "this process",
            )

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and always keep the record.

        All three places a record carries text, not just the message. The traceback is the one
        that mattered: `logger.exception(...)` / `exc_info=True` renders the exception at *format*
        time, so a filter that only rewrote the message left every credential in the inventory
        readable in the very log lines a failure produces — measured leaking both an API key and a
        DSN password verbatim. That is also the worst case, because an exception is exactly when a
        connection string or an auth header ends up inside the error text.

        `exc_info` is rendered here rather than left to the formatter, because redaction cannot be
        applied to a string that does not exist yet. `logging.Formatter.format` reuses a populated
        `exc_text` instead of re-rendering, so ours is what is emitted — including under a
        deployment's own formatter, which is the same reason this is a filter and not a formatter.

        **Nothing this method does may raise**, which is why the whole of it sits in a try. Filters
        run inside `Handler.handle` but *outside* the try/except that wraps `emit()`, so an
        exception here is not logging's to report — it lands in whoever called `logger.info(...)`.
        The `exc_info=False` crash below is one instance of that; measurement found five, every
        field this method touches (a `%`-format mismatch, a `msg` whose `__str__` raises, a
        pass-through `exc_info` tuple, a non-string `exc_text` or `stack_info`). Keeping the record
        is the right answer to all five rather than merely the safe-looking one: each is a
        malformation `Formatter.format` hits too, so the handler routes it to `handleError` ->
        stderr, which is exactly what happens with no filter installed. Returning `False` would
        trade a crash for a silently dropped log line, which is worse than either.

        That route is *also* how an unredacted credential reached stderr, because `handleError`
        prints the record's original `msg` and `args`. Fail-open is still right; what changed is
        that `configure_logging` binds a redacting `handleError` on every handler it sweeps, so the
        record this method gave up on is scrubbed by the path that reports it.
        """
        try:
            self._redact(record)
        except Exception:
            # See the note above: a record this filter cannot process is one the formatter cannot
            # process either, so it goes on to be reported by logging's own error path.
            pass
        return True

    def _redact(self, record: logging.LogRecord) -> None:
        """Rewrite every text field of `record` in place.

        Called only from `filter`, which owns the guarantee that a malformed record is reported by
        logging rather than raised at the caller — so this body is free to be written for the
        well-formed case.
        """
        message = record.getMessage()
        redacted = redact_secrets(message, self._connector_token_envs)
        if redacted != message:
            # Collapsed to a plain message: the args have been folded in, and leaving them would
            # let a formatter re-render the original.
            record.msg = redacted
            record.args = None
        # Truthiness, not `is not None`, and the difference is a crash. `Logger._log` stores
        # whatever it was handed: `logger.error(..., exc_info=False)` puts the *bool* on the record,
        # so `is not None` sent `False` into `formatException`, which subscripts it —
        # `TypeError: 'bool' object is not subscriptable`, which reached production code through
        # `metrics_bridge.degraded(..., exc_info=False)`: the one site passing it is
        # `skill_manifest`, inside the `except` whose whole purpose is to skip a malformed
        # `SKILL.md` and continue, so a single bad manifest made `build_langgraph_agent` raise.
        # `logging`'s own `Formatter.format` has always tested truthiness here; matching it is both
        # the fix and the reason no other formatter had this problem. Still load-bearing now that
        # `filter` catches: `exc_info=False` is a *well-formed* call, and letting it raise into that
        # catch would skip the two redaction steps below and leak a credential in `stack_info`.
        if record.exc_info and record.exc_text is None:
            # `_EXC_RENDERER` is built once at module scope: constructing a `Formatter` here would
            # be per-record work, and `formatException` reaches `traceback`, which `logging` has
            # already imported — so nothing on this path imports anything (see `ContextFilter`).
            record.exc_text = _EXC_RENDERER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, self._connector_token_envs)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info, self._connector_token_envs)


class ContextFilter(logging.Filter):
    """Attach the turn's correlation id, actor and session to every record.

    The three getters are resolved **once, here in `__init__`**, and `filter` then does nothing but
    call them. Not a style preference: a filter runs at arbitrary moments, including from inside
    another module's import and from inside Temporal's workflow sandbox, which hooks `__import__`
    and logs a warning when sandboxed code touches something restricted. An import on the logging
    path closes that into a loop — the import trips a restriction, the restriction logs, the log
    re-enters this filter, which imports again into a now half-initialised module. That is not a
    hypothetical: it deadlocked the workflow worker until the test run's global timeout fired.

    The getters themselves are imported at module scope, which is the strongest form of the same
    guarantee: they are resolved before this class can be constructed, let alone run. That was not
    available until the R2 layering move — `identity_context` and `session_context` lived in
    `chemclaw.agent`, so importing them here would have made `core.logging`, which every entrypoint
    imports first, depend on the conversation layer. They are stdlib-only kernel neighbours now.
    `RedactionFilter` above still resolves the connector registry lazily, because that one is a
    real sibling and the rule holds for it.
    """

    def __init__(self) -> None:
        """Bind the ambient-identity getters so `filter` never has to resolve a name."""
        super().__init__()
        self._actor = get_current_actor
        self._correlation_id = get_current_correlation_id
        self._session_id = get_current_session_id

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp the ambient identity onto the record and always keep it."""
        record.correlation_id = self._correlation_id() or "-"
        record.actor = self._actor() or "-"
        record.session_id = self._session_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so a log stack parses rather than guesses.

    The fields are the ones a query actually starts from: when, how bad, from where, and the three
    identifiers that join a line to the audit trail (`correlation_id`), to a conversation
    (`session_id`) and to a person (`actor`). An exception is rendered into `exception` rather than
    trailing after the line, because a multi-line traceback in a line-delimited format is how a
    stack trace becomes forty unparseable entries.

    **`exception` is taken from `record.exc_text`, never re-rendered from `exc_info`.** That is the
    whole point of `SecretRedactingFilter` rendering the traceback itself: re-rendering here would
    reach past the redaction into the original exception and emit the credential the filter had
    already replaced. It did, and only in production — the chart sets `CHEMCLAW_LOG_JSON=true`
    while the tests ran the plain formatter, so a measured leak of an API key and a DSN password
    lived in the one path no test took. The `redact_secrets` fallback covers the case where this
    formatter is used on a handler that carries no filter: it cannot see the per-connector bearer
    tokens the filter resolves, but it must not be the reason a secret is emitted.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a compact JSON object."""
        payload: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
            "correlation_id": getattr(record, "correlation_id", "-"),
            "actor": getattr(record, "actor", "-"),
            "session_id": getattr(record, "session_id", "-"),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        elif record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        if record.stack_info:
            # Redacted here as well as by the filter. Without this, adding the field created a
            # *new* unredacted channel in exactly the no-filter case the `exception` fallback was
            # added for — before this commit `stack_info` was dropped entirely, so the fix would
            # have introduced the leak it was closing.
            payload["stack"] = redact_secrets(record.stack_info)
        return json.dumps(payload, default=str)
