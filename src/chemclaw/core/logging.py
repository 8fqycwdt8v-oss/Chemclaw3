"""Application-wide logging setup — one config-driven switch, plus what a log line has to carry.

`configure_logging()` is called once at each entrypoint and wires the root logger to
`CHEMCLAW_LOG_LEVEL` / `CHEMCLAW_LOG_FORMAT`; application modules only call
`logging.getLogger(__name__)`. Every line carries the correlation, actor and session ids
(`ContextFilter`) so it can be joined to the audit trail and traces, can be rendered as JSON, and
has credentials scrubbed (`SecretRedactingFilter`).

The audit trail's tool arguments are deliberately not redacted: that trail is the attributable
"who did what to which inputs" record (`SECURITY.md`). Redaction here targets credentials.
"""

import json
import logging
import os
import re
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.session_context import get_current_session_id

if TYPE_CHECKING:
    # Annotations only: the SDK is imported inside `configure_telemetry`, since telemetry is off by
    # default and a missing extra must be catchable there.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter


def configure_logging() -> None:
    """Configure the root logger from config (level + format).

    Safe to call more than once: `force=True` replaces the root's handlers rather than stacking
    them.
    """
    logging.basicConfig(
        level=settings.log_level.upper(),
        format=settings.log_format,
        force=True,
    )
    # Filters go on handlers, not loggers: a logger's filter is not consulted for records propagated
    # from children. One filter pair is built and shared, because constructing the redactor reads
    # the connector registry and logs its degradation once per startup.
    handlers = _handlers_that_reach_an_output_stream()
    # `ContextFilter` is installed before `SecretRedactingFilter` is constructed: that constructor
    # may log `degraded[log_redaction]`, and the format needs `%(correlation_id)s`, which only
    # `ContextFilter` supplies. Otherwise the one security degradation line fails to format.
    context = ContextFilter()
    for handler in handlers:
        if not any(isinstance(f, ContextFilter) for f in handler.filters):
            handler.addFilter(context)
    redaction = SecretRedactingFilter()
    for handler in handlers:
        # A non-propagating logger's handlers are not reset by `force=True`, so without this guard
        # each call would stack another filter pair on them.
        installed = next((f for f in handler.filters if isinstance(f, SecretRedactingFilter)), None)
        if installed is None:
            handler.addFilter(redaction)
            installed = redaction
        # The filter is fail-open, so an unredactable record reaches logging's own stderr error
        # path; this makes that path print a redacted copy. Passed `installed` (the filter actually
        # on the handler), so both paths share one token inventory across repeated calls.
        _install_redacting_handle_error(handler, installed)
        if settings.log_json:
            handler.setFormatter(JsonFormatter())


def log_event(
    logger: logging.Logger,
    event: str,
    message: str,
    *args: object,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: object,
) -> None:
    """Emit one record that is both readable prose and a queryable row.

    `event` is a short dotted literal (`turn.finished`, `job.failed`) that a query starts from; it
    lands in `fields.event` and, under the `%` format, as the message prefix. `logger` is the
    caller's so the record says where it happened.

    Args:
        logger: the calling module's logger.
        event: the dotted event name; must be a literal at the call site.
        message: a `%`-style format string. Lazy, as every log call here is.
        *args: the format arguments for `message`.
        level: the log level; INFO, since a lifecycle record is not a fault.
        exc_info: attach the active exception (for a `*.failed` event inside an `except`).
        **fields: the structured payload. Values should be scalars — a log stack indexes those.
    """
    # G003 suppressed: interpolating eagerly would raise a format mismatch out of the logging call
    # instead of through logging's own error path. Concatenating the prefix keeps one lazy format.
    logger.log(
        level,
        "%s: " + message,  # noqa: G003
        event,
        *args,
        exc_info=exc_info,
        extra={"event": event, **fields},
    )


def _handlers_that_reach_an_output_stream() -> list[logging.Handler]:
    """Every handler a record can reach — the root's, plus any non-propagating logger's own.

    The front door runs `exec uvicorn ... --factory`, and uvicorn gives `uvicorn` a handler with
    `propagate: false`; its `uvicorn.error` logs unhandled exceptions, exactly the records that
    carry DSNs or auth headers. Sweeping the logger manager covers it without the entrypoint
    knowing.

    The sweep is one-shot at `configure_logging()` time. It reaches uvicorn's loggers only because
    uvicorn configures them before the app factory runs; a library that creates a non-propagating
    logger later is not covered.
    """
    handlers: list[logging.Handler] = list(logging.getLogger().handlers)
    # `logging.lastResort` is what a non-propagating logger with no handlers falls back to, so it
    # gets the same redaction.
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    # Snapshot with a single C-level `list()` copy: other threads may create loggers concurrently,
    # and iterating the live view can raise "dictionary changed size during iteration"
    # mid-configuration.
    known = list(logging.root.manager.loggerDict.values())
    for existing in known:
        # `PlaceHolder` entries are not loggers and carry no handlers.
        if isinstance(existing, logging.Logger) and not existing.propagate:
            handlers.extend(existing.handlers)
    return handlers


def _redacted_for_diagnostic(value: object, extra_secrets: tuple[str, ...]) -> str:
    """One field of logging's own error diagnostic, rendered and scrubbed, never raising.

    `repr` for a non-string, as `handleError` would print, and `***` if rendering raises: this runs
    on a record that already failed to render, so hostile `__str__`/`__repr__` is expected input.
    """
    try:
        return redact_secrets(value if isinstance(value, str) else repr(value), extra_secrets)
    except Exception:
        return _REDACTED


def _install_redacting_handle_error(
    handler: logging.Handler, redaction: "SecretRedactingFilter"
) -> None:
    r"""Bind a `handleError` on `handler` that scrubs the record before stderr sees it.

    The redacting filter is fail-open, so a record it cannot process keeps its original `msg` and
    `args`; formatting then fails and `Handler.handleError` writes both verbatim to stderr — where a
    credential in `args` would leak. Redacting those two fields keeps the diagnostic, unlike setting
    `logging.raiseExceptions = False`, which would hide every handler failure in the process.

    Idempotent: the bound function delegates to `type(handler).handleError`, never to the previous
    attribute, so a second call rebinds rather than stacks, and subclass behaviour is preserved.
    """

    def handle_error(record: logging.LogRecord) -> None:
        """Print logging's own diagnostic for `record` with its credentials removed.

        Nothing here may raise, since anything escaping surfaces at the caller's log line. The scrub
        is attempted, any failure drops the arguments, and the delegation sits outside the `try` so
        it always runs. Token names are read per call so the filter and this path share one
        inventory.
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
            # Restored: the same record goes to every handler, and each must format the caller's own
            # values.
            record.msg, record.args = msg, args

    handler.handleError = handle_error  # type: ignore[method-assign]


# Set once per process: `metrics.set_meter_provider` refuses a second call and warns, and the only
# thing this flag has to be right about is not producing that warning on a re-entry.
_NOOP_METERS_INSTALLED = False


def _install_noop_meter_provider() -> None:
    """Make "telemetry off" mean a no-op provider, not the *absence* of one.

    With no meter provider set, the OpenTelemetry API proxies every instrument call and retains each
    proxy forever, so per-turn instrument creation leaks memory without bound. Idempotent by a
    module flag, because `get_meter_provider()` itself resolves and caches a provider.
    """
    global _NOOP_METERS_INSTALLED
    if _NOOP_METERS_INSTALLED:
        return
    from opentelemetry import metrics

    metrics.set_meter_provider(metrics.NoOpMeterProvider())
    _NOOP_METERS_INSTALLED = True


# The `service.name` spans carry when `OTEL_SERVICE_NAME` is unset. Not a `Settings` field because
# OpenTelemetry already owns that variable; set it per Deployment to separate services.
_DEFAULT_SERVICE_NAME = "chemclaw"

# Set once per process: a second provider would start another export thread and channel that the API
# discards. A flag, because `get_tracer_provider()` resolves and caches one as a side effect.
_TRACING_INSTALLED = False


def _build_tracer_provider(exporter: "SpanExporter") -> "TracerProvider":
    """Assemble the span pipeline: a resource that names the service, batching, then `exporter`.

    Separate from the global install so a test can drive it against an in-memory exporter.
    `BatchSpanProcessor` because a synchronous export would sit on the event loop serving every
    stream.
    """
    from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            # The standard variable wins when set; resolved explicitly so the service name is
            # decided in one
            # expression.
            SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME") or _DEFAULT_SERVICE_NAME,
            # The build's Git SHA, the same value stamped on every audit record.
            SERVICE_VERSION: settings.deployment_revision,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def configure_telemetry() -> None:
    """Install the process's OpenTelemetry span pipeline; install no-op meters when it is off.

    Off unless `CHEMCLAW_OTEL_ENABLED=true`. When on, installs a global `TracerProvider` with an
    OTLP span exporter behind a `BatchSpanProcessor`, so the first-party spans in `core/tracing.py`
    are exported; span helpers degrade silently to no-ops otherwise, so tests guard this pipeline.
    Called once per entrypoint, after `configure_logging`; idempotent.

    Traces only: metrics are `core/metrics.py`'s Prometheus surface and logs go to stderr. The no-op
    meter provider is installed in both states, since the OTel API leaks proxies without one.
    `_instrument_llm_calls` adds model-call spans behind `CHEMCLAW_OTEL_LLM_SPANS`, and
    `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` decides whether those carry prompts and completions.

    Raises:
        RuntimeError: `CHEMCLAW_OTEL_ENABLED=true` but the OpenTelemetry SDK / OTLP exporter is not
            installed — a directive message rather than the import error.
    """
    global _TRACING_INSTALLED
    if not settings.otel_enabled:
        _install_noop_meter_provider()
        return
    if _TRACING_INSTALLED:
        return
    # Bridge `CHEMCLAW_OTEL_ENDPOINT` to the standard OTLP variable; `setdefault` so a directly set
    # standard or per-signal variable wins.
    if settings.otel_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", settings.otel_endpoint)
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    except ImportError as exc:  # SDK/exporter extras not installed
        raise RuntimeError(
            "CHEMCLAW_OTEL_ENABLED=true but the OpenTelemetry SDK/OTLP exporter is not installed"
        ) from exc

    # No endpoint argument: the exporter resolves endpoint, headers and protocol from the standard
    # variables with OTel's own precedence.
    provider = _build_tracer_provider(OTLPSpanExporter())
    trace.set_tracer_provider(provider)
    _instrument_llm_calls(provider)
    _TRACING_INSTALLED = True
    _install_noop_meter_provider()
    if settings.otel_include_sensitive_data:
        _warn_about_sensitive_data()


def _warn_about_sensitive_data() -> None:
    """Say what `CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` is doing — in *both* directions.

    When content export is on, the line names the endpoint the prompts and completions go to, so the
    choice is never made unnoticed; it is not a refusal. When it is off, first-party spans carry no
    turn content, which holds because `core/tracing.exportable_detail` scrubs span exception detail.
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

    A span per model call plus the chain and tool spans around it, over the provider's exporter. Off
    unless `CHEMCLAW_OTEL_LLM_SPANS=true`, and then not even imported. Whether content rides on the
    spans is `otel_include_sensitive_data`'s decision; suppressed (the default), token counts, model
    and provider remain while every input, output and message value is hidden. No second-call guard:
    `configure_telemetry` already returns early on `_TRACING_INSTALLED`.

    Args:
        provider: The tracer provider built for this process, passed explicitly rather than read
            back from the global.

    Raises:
        RuntimeError: `CHEMCLAW_OTEL_LLM_SPANS=true` but the instrumentation is not installed.
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

    Every hide flag is set together because they answer one question: may turn content leave this
    pod? The list is written out rather than derived from the dataclass, so a new upstream flag
    requires a decision here; `tests/test_llm_spans.py` compares it against the fields.

    Args:
        trace_config: The `TraceConfig` class, already resolved by the caller's lazy import,
            which keeps this a pure function testable without a tracer provider.

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
        # `hide_embeddings_text` matters most: embedded text is a chemist's question or a note's
        # body. `hide_embedding_vectors` and `hide_embeddings_vectors` are upstream's spelling and
        # its alias; both are set because a given release may read either.
        hide_embedding_vectors=True,
        hide_embeddings_vectors=True,
        hide_embeddings_text=True,
    )


# The settings whose values must never appear in a log line. Redaction matches the actual secret
# values this process holds rather than token-shaped strings, so it catches a secret on any route
# and cannot false-positive. Listed by hand rather than derived from name patterns, which would
# include non-secrets and miss the next oddly named secret.
_SECRET_SETTINGS = (
    "llm_api_key",
    # The fallback endpoint's own credential, which may differ from the primary's.
    "llm_fallback_api_key",
    "temporal_api_key",
    # The external vector store's key and the live lane's bearer. `tests/test_credentials.py` also
    # checks credential-shaped `Settings` names against this list.
    "vector_store_api_key",
    "live_probe_token",
    "postgres_dsn",
    "postgres_migration_dsn",
    "session_store_dsn",
    # The HMAC key `agent/framing.py` derives `ENVELOPE_TAG` from. Anyone who learns it can close a
    # retrieval envelope from inside and have their text read as instructions.
    "framing_envelope_secret",
)

# Settings holding a credential's variable *name* (for example `calc_server_token_env`), whose named
# variable holds a bearer this process sends. The name itself must stay readable in log lines that
# tell an operator what to set; the value it points at is redacted. Derived from the suffix, unlike
# `_SECRET_SETTINGS`, because `_token_env` exactly means "names a variable". Field names are read
# once at import; only the environment reads happen per record.
_TOKEN_ENV_SUFFIX = "_token_env"
_SECRET_ENV_SETTINGS: tuple[str, ...] = tuple(
    sorted(name for name in type(settings).model_fields if name.endswith(_TOKEN_ENV_SUFFIX))
)

# The knowledge-sync sidecar's git push credential. No `Settings` field reads it, but the chart puts
# every secret key into every component's environment, so it is redacted here.
_KNOWLEDGE_REPO_TOKEN_ENV = "CHEMCLAW_KNOWLEDGE_REPO_TOKEN"

# Credential variable names registered at runtime by code that reads its own configuration, such as
# a data source whose manifest names its warehouse credentials. Names, never values: values are read
# fresh from `os.environ` per call so a rotated credential is redacted immediately. A set, not a
# `Settings` field, so attaching a source needs no core edit. Additive only.
_RUNTIME_SECRET_ENVS: set[str] = set()


def register_secret_env(name: str) -> None:
    """Add an environment variable to the redaction inventory for the life of this process.

    For a credential this process holds but does not configure. Call it where the variable is read,
    so the registration cannot drift from the use.
    """
    if name:
        _RUNTIME_SECRET_ENVS.add(name)


def _named_token_env_vars() -> frozenset[str]:
    """The environment variables the `*_token_env` settings point at, empty values dropped.

    The one derivation shared by the log filter and `secret_env_names()`, so a bearer scrubbed from
    logs is also withheld from child processes.
    """
    named = (str(getattr(settings, field, "") or "") for field in _SECRET_ENV_SETTINGS)
    return frozenset(variable for variable in named if variable)


def secret_env_names() -> frozenset[str]:
    """The `CHEMCLAW_*` environment-variable names this process holds a secret *value* under.

    Both inventories: `_SECRET_SETTINGS` (values) and the `*_token_env` targets
    (`_named_token_env_vars`). Used for least privilege: child processes (the KG git commands in
    `kg/git_writer.py`) get none of these, so a git remote, credential helper or hook cannot read
    them. `_KNOWLEDGE_REPO_TOKEN_ENV` is deliberately excluded because git legitimately needs it to
    push; runtime-registered connector tokens are excluded because they belong to MCP sessions.
    """
    prefix = str(type(settings).model_config.get("env_prefix", ""))
    settings_values = frozenset(f"{prefix}{name}".upper() for name in _SECRET_SETTINGS)
    return settings_values | _named_token_env_vars()


def _configured_by(env_name: str) -> str:
    """The `Settings` value that environment variable configures, or `""` if it configures none.

    pydantic-settings reads `.env` itself without exporting it, so a registered name can be empty in
    `os.environ` while `Settings` holds the credential; this resolves the value from `Settings`. The
    prefix comes from `model_config` rather than a second spelling of `CHEMCLAW_`.
    """
    prefix = str(type(settings).model_config.get("env_prefix", ""))
    if not env_name.startswith(prefix):
        return ""
    return _secret_text(getattr(settings, env_name[len(prefix) :].lower(), ""))


# Below this length a "secret" is more likely to be a placeholder, an empty default, or a string
# that occurs in ordinary prose — redacting it would corrupt every line containing that substring.
_MIN_REDACTABLE = 8

_REDACTED = "***"

# A credential in a URL's userinfo: `scheme://user:secret@host` (DSNs, git remotes) or
# `scheme://token@host` (a PAT on a git remote). Matched structurally because such a token is often
# held outside this process, where the value inventory cannot see it. The user is kept in the
# two-part form so a redacted line still names the principal; in the one-part form the whole
# userinfo is the credential.
_URL_USERINFO = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.\-]{0,63}://)([^/\s:@]{0,512})(?::([^/\s@]{0,512}))?@"
)


# Credentials this process does not hold (a caller's bearer, a third-party PAT in an upstream error,
# a libpq `key=value` DSN), matched by shape rather than value.
#
# A false positive corrupts a log line, and tracebacks quote source lines such as `access_token =
# response.json().get("access_token")`, so a key-name anchor is not enough: the value must look like
# a credential. `_OPAQUE` excludes quotes, parentheses, commas and semicolons, and a digit is
# required (random credentials effectively always contain one; words and attribute paths do not). A
# pure-letter token is the accepted miss; the value inventory is the primary mechanism. `Basic`
# alone is not a rule: it is an ordinary word.
#
# Every tail is bounded: this runs inside `handler.handle()` under the logging lock, and an
# unbounded tail is a quadratic denial of service reachable by anything that can get text into a log
# line. `(?P<keep>...)` preserves the label so a redacted line says which credential failed.
#
# The characters a credential is made of. No quotes, parens, commas or semicolons: those are what a
# repr, a call expression or a libpq string puts around a value, never inside one.
_OPAQUE = r"[A-Za-z0-9_\-.~+/=]"
# Not preceded by a token character. `\b` would match between `-` and `e`, making every `-eyJ` a new
# start position whose tail rescans the remainder (quadratic). A real credential is preceded by a
# space, quote, `=` or `:`.
_NOT_MID_TOKEN = r"(?<![A-Za-z0-9_\-.])"

#: The RFC 1421 header lines an encrypted traditional-format PEM carries between `-----BEGIN` and
#: the body (`Proc-Type: 4,ENCRYPTED`, `DEK-Info: <cipher>,<iv>`), each with the bound on its tail.
#: Literals rather than a widened class, because a class containing `-` would walk through
#: `-----END`. The one declaration of the set: the regex, its repeat bound and the pathological
#: payloads in `tests/test_logging.py` all derive from it.
_PEM_RFC1421_HEADERS: tuple[tuple[str, int], ...] = (("Proc-Type:", 40), ("DEK-Info:", 96))
_PEM_RFC1421 = "|".join(rf"{name}[^\r\n\\]{{0,{bound}}}" for name, bound in _PEM_RFC1421_HEADERS)

#: One step of the whitespace gap between a PEM header and its body: a JSON escape as a unit, or one
#: whitespace or backslash character. Header lines are not a branch here; see `_PEM_PREAMBLE`.
_PEM_GAP = r"(?:\\[nrt]|[\s\\])"

#: Everything the format allows between `-----BEGIN … PRIVATE KEY-----` and the body: a whitespace
#: gap, then at most one of each RFC 1421 header line with its own gap after it.
#:
#: The header lines are bounded in number rather than folded into the repeated gap. A header tail
#: and the gap both match whitespace, so inside an enclosing repetition a failing lookahead
#: enumerates their split once per attacker-supplied group — exponential backtracking under the
#: logging lock, reachable from model-authored text and remote error messages. Bounded, the
#: ambiguity is paid at most `len(_PEM_RFC1421_HEADERS)` times.
#:
#: The tails are greedy, not possessive: a possessive tail on a single-line PEM swallows the next
#: header and the body and then fails, leaking the key (`_PEM_SHAPES_THAT_WALKED_PAST` in the
#: tests).
_PEM_PREAMBLE = (
    _PEM_GAP
    + r"{0,64}(?:(?:"
    + _PEM_RFC1421
    + r")"
    + _PEM_GAP
    + rf"{{0,64}}){{0,{len(_PEM_RFC1421_HEADERS)}}}"
)
# "Contains a digit" — the cheap discriminator between a token and an identifier. Bounded to 255
# characters so each anchor scans a fixed window; unbounded, a whitespace-free run of `password=`
# (reachable unauthenticated via the access log's request line) is quadratic under the logging lock.
_HAS_DIGIT = r"(?=" + _OPAQUE + r"{0,255}\d)"

# The framing between a key name and its value: optional quote, separator, optional quote. Each
# quote may be backslash-escaped, because a credential nested in a non-string `extra=` value is
# scrubbed after `json.dumps` and arrives as `{\"password\": \"...\"}`; `redact_secrets` also runs
# over note bodies, outgoing messages and span descriptions that carry JSON quoted inside JSON. Up
# to four backslashes covers text already encoded once. The possessive quantifiers are a margin, not
# the control: both runs are constant-bounded with nothing repeating around them, so the rule is
# linear either way.
_KEY_FRAMING = r"(?:\\{0,4}+[\"'])?\s*+[=:]\s*+(?:\\{0,4}+[\"'])?"

_STRUCTURAL_SECRETS: tuple["re.Pattern[str]", ...] = (
    # GitHub tokens (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` and fine-grained `github_pat_`). The
    # vendor-assigned prefix is decisive, so no digit requirement.
    re.compile(_NOT_MID_TOKEN + r"gh[pousr]_[A-Za-z0-9]{20,255}"),
    re.compile(_NOT_MID_TOKEN + r"github_pat_[A-Za-z0-9_]{20,255}"),
    # Anthropic and OpenAI keys, including project-scoped and `sk-admin-` spellings (the bare-tail
    # rule below cannot reach a second hyphen). `tests/test_logging.py` holds the prefix inventory.
    re.compile(_NOT_MID_TOKEN + r"sk-(?:ant|proj|svcacct|admin)-[A-Za-z0-9_\-]{16,255}"),
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
        r"(?P<keep>\b(?:PG)?PASSWORD" + _KEY_FRAMING + r")" + _HAS_DIGIT + _OPAQUE + r"{6,255}",
        re.IGNORECASE,
    ),
    # A credential in a query string, a header, or a rendered dict. Anchored on the key name so the
    # bare words in prose cannot trigger it, and on the value's shape so a source-line assignment
    # cannot. The generic names (`token`, `secret`, `private_key`, `passwd`, `pwd`) are included
    # because warehouse drivers choose their own keyword spellings and quote them back in errors.
    re.compile(
        r"(?P<keep>\b\w*?(?:access_token|refresh_token|api[_-]?key|client_secret|token|secret"
        r"|private_key|passwd|pwd)" + _KEY_FRAMING + r")" + _HAS_DIGIT + _OPAQUE + r"{8,255}",
        re.IGNORECASE,
    ),
    # `Authorization: Basic <base64>`. The `Authorization` + framing + `Basic` anchor is
    # unambiguous, so no digit is required (base64 of `user:password` may have none). `_KEY_FRAMING`
    # as separator so the rendered-dict spelling `{"Authorization": "Basic ..."}` is caught too.
    re.compile(
        r"(?P<keep>\bAuthorization" + _KEY_FRAMING + r"Basic\s+)[A-Za-z0-9+/=]{8,4096}",
        re.IGNORECASE,
    ),
    # The SCREAMING_CASE environment spelling (`AWS_SECRET_ACCESS_KEY=…`, `PASSWORD=…`), which the
    # key-name rule cannot reach because `_` is a word character. The casing and `=` carry the
    # specificity, so no digit is required.
    #
    # One greedy run behind a lookahead: nested quantifiers over the same alphabet backtrack
    # exponentially, and this is reachable unauthenticated through the access log's request line.
    # The lookahead asserts a credential word lies in the bounded run; the capture `[A-Z][A-Z0-9_]*`
    # has one way to match.
    #
    # Carve-outs: a digits-only value is token accounting (`MAX_TOKENS=40960000`), not a secret; and
    # a `*_ENV=NAME` pair is a variable name by this tree's convention. Both key and value
    # conditions are required, so a token pasted into a `*_TOKEN_ENV` variable is still redacted.
    re.compile(
        r"(?<![A-Za-z0-9_])"
        r"(?=[A-Z0-9_]{0,128}?(?:SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|CREDENTIAL))"
        r"(?P<keep>[A-Z][A-Z0-9_]*" + _KEY_FRAMING + r")"
        r"(?![0-9]{1,255}(?![A-Za-z0-9_\-]))"
        r"(?!(?<=_ENV=)[A-Z][A-Z0-9_]*(?![A-Za-z0-9_\-]))" + _OPAQUE + r"{8,255}"
    ),
    # Vendor-issued shapes whose prefix is the anchor. AWS access-key ids carry four prefixes:
    # `AKIA`, `ASIA` (temporary STS credentials, what a workload actually runs with), `ABIA` and
    # `ACCA`.
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    # Slack tokens, including the app-level `xapp-` form.
    re.compile(r"\b(?:xox[baprs]|xapp)-[A-Za-z0-9-]{10,255}"),
    # A Databricks personal access token: `dapi` plus 32 lowercase hex, optional `-N` suffix. A
    # warehouse driver quotes it back in authentication errors. Fixed width, nothing to backtrack
    # over.
    re.compile(_NOT_MID_TOKEN + r"dapi[0-9a-f]{32}(?:-\d{1,2})?"),
    # A GitLab access token, as git or a CI runner quotes it bare in an error (`_URL_USERINFO`
    # covers the in-URL form).
    re.compile(_NOT_MID_TOKEN + r"glpat-[A-Za-z0-9_\-]{16,255}"),
    # A PEM private key: the header is kept (it tells an operator which kind of key was there) and
    # the body redacted.
    #
    # The rule does not look for the END line: a greedy, unbounded run of a class excluding `-`
    # stops at `-----END` by itself, and since nothing follows it, it cannot backtrack. A finite
    # bound would leak the tail of a longer block. The lookahead requiring an unbroken base64 run
    # after the header keeps it off prose such as `expected -----BEGIN PRIVATE KEY----- but found
    # garbage`.
    #
    # Covered shapes: the RFC 1421 encrypted form with `Proc-Type`/`DEK-Info` lines (named
    # explicitly rather than widening the class), deep indents (YAML block scalars, 64-column
    # separator window), narrow wraps (20-character required run), and the JSON-encoded spelling,
    # where `\\[nrt]` is consumed as a unit and `\\` is in the classes.
    re.compile(
        r"(?P<keep>-----BEGIN (?:[A-Z]{1,16} ){0,3}PRIVATE KEY-----)"
        r"(?=" + _PEM_PREAMBLE + r"[A-Za-z0-9+/=]{20})"
        r"(?:" + _PEM_RFC1421 + r"|[\s\\A-Za-z0-9+/=])*"
    ),
    # `Authorization: Bearer <opaque>` / `Token <opaque>`. The scheme is the anchor; the digit
    # requirement keeps "Bearer token was rejected" intact.
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

    Shared by the redaction inventory and the published-defaults set so both decide the same thing
    about the same string.
    """
    if "://" not in value or "@" not in value:
        return ""
    userinfo = value.split("://", 1)[1].split("@", 1)[0]
    return userinfo.split(":", 1)[1] if ":" in userinfo else ""


@lru_cache(maxsize=1)
def _published_values() -> frozenset[str]:
    """Every secret-shaped value this repository *commits* — and therefore does not have to hide.

    A value readable in `core/config/` is not a credential, and redacting it (the dev DSN's password
    is `chemclaw`) would corrupt unrelated log lines. Default DSNs' passwords are published too,
    because a test DSN repointed at another schema still carries the same password. Cached: defaults
    are fixed at import.
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

    `str(SecretStr(...))` is asterisks, so an `isinstance(value, str)` check would silently skip
    every `SecretStr` credential. Plain `str` stays accepted so a monkeypatched test value is still
    redacted.
    """
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value if isinstance(value, str) else ""


def redact_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Return `text` with every credential this process can recognize replaced by `***`.

    The redaction `SecretRedactingFilter` applies, exposed so anything that persists or sends text
    (for example `kg.record` on a note body) applies the same one; truncation is not redaction.
    `extra_secrets` is for values a caller resolved itself (the filter's connector bearer
    variables), keeping the lazy `connectors` import out of this module.
    """
    redacted = text
    for secret in _secret_values(extra_secrets):
        redacted = redacted.replace(secret, _REDACTED)
    # A callable replacement, not a `\1` template: a template is compiled lazily and imports `re` on
    # the logging path, which can re-enter the filter under Temporal's sandbox and wedge the worker.
    redacted = _URL_USERINFO.sub(_redact_userinfo, redacted)
    for pattern in _STRUCTURAL_SECRETS:
        redacted = pattern.sub(_redact_structural, redacted)
    return redacted


def _secret_values(connector_token_envs: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The distinct secret values this process actually holds, longest first.

    Longest first so a DSN is redacted before the password inside it. `connector_token_envs` is the
    filter's resolved list of connector bearer variables, passed in to keep the `connectors` import
    out of here. Not memoised: a value that becomes secret mid-process must be redacted on the very
    next line.
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
        # A DSN's password is matched on its own too: a connection error may quote only the
        # password, and the DSN and its password can differ in whether they are published defaults.
        _consider(_dsn_password(value))
    for env_name in (
        _KNOWLEDGE_REPO_TOKEN_ENV,
        *connector_token_envs,
        *sorted(_RUNTIME_SECRET_ENVS),
    ):
        # Environment first (where a rotated credential lands); `_configured_by` covers the `.env`
        # posture.
        _consider(os.environ.get(env_name, "") or _configured_by(env_name))
    # Bearers named by `*_token_env` settings: `os.environ` only. Their variables are not `Settings`
    # fields, so `extra="forbid"` refuses them in `.env` and they can only arrive via the
    # environment; skipping `_configured_by` keeps this cheap per record.
    for variable in _named_token_env_vars():
        _consider(os.environ.get(variable, ""))
    return tuple(sorted(values, key=len, reverse=True))


# Renders `exc_info` for `SecretRedactingFilter`. Module scope so the logging path constructs
# nothing per record; a bare `Formatter` because only its `formatException` is used.
_EXC_RENDERER = logging.Formatter()


class SecretRedactingFilter(logging.Filter):
    """Replace any configured secret's value with `***` in a record's rendered message.

    A filter rather than a formatter, so a deployment's own formatter cannot switch redaction off.
    It runs on the rendered message so secrets passed as `%s` arguments are caught. Connector bearer
    variables come from `chemclaw.connectors`, which `core` may not import at module scope; they are
    resolved once in `__init__`, never from `filter()`, which runs per record and may be reached
    mid-import.
    """

    def __init__(self) -> None:
        """Resolve the connector bearer-token variable names once, tolerating discovery failure.

        A broken connector manifest degrades this to redacting nothing extra rather than blocking
        `configure_logging()`, reported at ERROR as `degraded[log_redaction]` with a counter, since
        it is a security degradation.
        """
        super().__init__()
        self._connector_token_envs: tuple[str, ...] = ()
        try:
            from chemclaw.connectors.registry import bearer_token_env_names

            self._connector_token_envs = bearer_token_env_names()
        except Exception:
            # ERROR and counted: the process would log connector bearer tokens in the clear for its
            # lifetime, and a bad manifest is exactly what produces connector failures whose
            # tracebacks carry tokens. `degraded[log_redaction]` is the marker to alert on. Safe
            # here: this filter is not installed yet, and `record_metric` swallows registry errors.
            degraded(
                logging.getLogger(__name__),
                "log_redaction",
                "could not resolve connector bearer-token env names for log redaction; "
                "connector credentials will NOT be scrubbed from log lines for the life of "
                "this process",
            )

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and always keep the record.

        Covers the message, the traceback and `extra=` fields. `exc_info` is rendered here because
        the traceback text does not exist until format time; `Formatter.format` then reuses our
        `exc_text`, even under a deployment's own formatter.

        Nothing here may raise: filters run outside the try around `emit()`, so an exception lands
        in the caller's `logger.info(...)`. A malformed record is kept, so `Formatter.format` routes
        it to `handleError`, which `configure_logging` makes redacting. Dropping it would silently
        lose a line.
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

        Only called from `filter`, which handles malformed records, so this is written for the
        well-formed case.
        """
        message = record.getMessage()
        redacted = redact_secrets(message, self._connector_token_envs)
        if redacted != message:
            # Collapsed to a plain message: the args have been folded in, and leaving them would
            # let a formatter re-render the original.
            record.msg = redacted
            record.args = None
        # Truthiness, not `is not None`, matching `Formatter.format`: `exc_info=False` is a
        # well-formed call that stores the bool, and `formatException(False)` would raise, skipping
        # the redaction below.
        if record.exc_info and record.exc_text is None:
            # `_EXC_RENDERER` is built once at module scope so nothing on this path constructs or
            # imports.
            record.exc_text = _EXC_RENDERER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, self._connector_token_envs)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info, self._connector_token_envs)
        # `extra=` fields: a handler format string may reference them directly. Strings only here; a
        # non-string extra is redacted where `JsonFormatter` renders it, since the text to scrub
        # does not exist until `json.dumps` runs, and walking nested structures per record is too
        # costly.
        for key, value in structured_fields(record).items():
            if isinstance(value, str):
                redacted = redact_secrets(value, self._connector_token_envs)
                if redacted != value:
                    setattr(record, key, redacted)
        # The identity fields, when a caller's header supplied them (`_CLAIMED_MARK`). Excluded
        # from the sweep above for cost, which is safe only for values this process bound itself.
        if record.__dict__.get(_CLAIMED_MARK):
            for key in _IDENTITY_FIELDS:
                value = record.__dict__.get(key)
                if isinstance(value, str):
                    record.__dict__[key] = redact_secrets(value, self._connector_token_envs)
        record.__dict__[_REDACTED_MARK] = True


# Every attribute `logging` itself puts on a record; anything else arrived through `extra=` or a
# filter. A literal rather than probed, because conditional attributes (`exc_text`, `stack_info`,
# `taskName`) would be missed and then published as caller fields.
_LOGRECORD_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
    # `ContextFilter`'s own three: stamped by this module and promoted to top-level keys, so
    # excluding them avoids redundant redaction passes per record.
    | {"correlation_id", "actor", "session_id"}
)

# Set once `SecretRedactingFilter._redact` has swept a record, so `JsonFormatter` skips a second
# pass under the logging lock. The formatter still redacts when the mark is absent (a handler with
# no filter).
_REDACTED_MARK = "_chemclaw_redacted"

# Set by `ContextFilter` when identity fields came from a *claimed* caller; only then are those
# fields swept, since header-supplied values are whatever the sender wrote.
_CLAIMED_MARK = "_chemclaw_claimed_caller"

_IDENTITY_FIELDS = ("correlation_id", "actor", "session_id")

_claimed_caller: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "chemclaw_log_claimed_caller", default=None
)


def bind_claimed_caller(actor: str, session_id: str, correlation_id: str) -> object:
    """Make a caller's *claimed* identity the log attribution for this context; returns a token.

    A connector pod learns who it serves from `X-Chemclaw-*` headers; binding them here lets its log
    lines carry the turn's ids. A separate variable from the core identity ones on purpose: those
    are read by authorization gates, and an unauthenticated header must never become an identity.
    Only the log filter reads this.
    """
    return _claimed_caller.set((actor, session_id, correlation_id))


def reset_claimed_caller(token: object) -> None:
    """Undo the matching `bind_claimed_caller`."""
    _claimed_caller.reset(token)  # type: ignore[arg-type]


def structured_fields(record: logging.LogRecord) -> dict[str, object]:
    """The fields a caller attached with `extra=`, and nothing `logging` put there itself.

    One definition shared by the redaction filter (which scrubs them) and the JSON formatter (which
    publishes them), so they cannot disagree.
    """
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _LOGRECORD_RESERVED and not key.startswith("_")
    }


class ContextFilter(logging.Filter):
    """Attach the turn's correlation id, actor and session to every record.

    Nothing on the filter path may import: a filter can run inside another module's import or inside
    Temporal's workflow sandbox, where an import that trips a restriction logs, re-enters this
    filter and deadlocks. The getters are therefore stdlib-only kernel neighbours imported at module
    scope.
    """

    def __init__(self) -> None:
        """Bind the ambient-identity getters so `filter` never has to resolve a name."""
        super().__init__()
        self._actor = get_current_actor
        self._correlation_id = get_current_correlation_id
        self._session_id = get_current_session_id
        self._claimed = _claimed_caller.get

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp the ambient identity onto the record, without overwriting an explicit one.

        `setdefault`: a caller passing `correlation_id` via `extra=` does so because the ambient
        value is wrong at that moment (for example the audit sink-failure marker written after turn
        teardown). An identity this process bound itself wins; a connector's claimed caller
        (`bind_claimed_caller`) fills only what is absent and marks the record for redaction.
        """
        claimed = self._claimed()
        actor, session_id, correlation_id = claimed or ("", "", "")
        record.__dict__.setdefault(
            "correlation_id", self._correlation_id() or correlation_id or "-"
        )
        record.__dict__.setdefault("actor", self._actor() or actor or "-")
        record.__dict__.setdefault("session_id", self._session_id() or session_id or "-")
        if claimed is not None:
            record.__dict__[_CLAIMED_MARK] = True
        return True


def _redacted_field(value: object, swept: bool) -> object:
    r"""One `extra=` value, scrubbed in whatever form it will actually be written in.

    A string the filter already swept passes through (`swept`). Anything else is rendered with
    `json.dumps(default=str)` first and scrubbed after, because the rendered text is what reaches
    the stream and a credential inside a dict, list or exception is only reachable there;
    `_KEY_FRAMING` handles the escaped quotes this produces. Rendered text is returned as a string
    when redaction changed it; an unchanged structure is returned as itself so a log stack can index
    it. A value that cannot be rendered becomes `***` rather than raising inside `format()`.
    """
    if isinstance(value, str):
        return value if swept else redact_secrets(value)
    try:
        rendered = json.dumps(value, default=str)
    except Exception:
        # `Exception`, not `(TypeError, ValueError)`: `default=str` may hit a hostile `__repr__`
        # that raises anything, and an escaping error would drop the whole record.
        return _REDACTED
    scrubbed = redact_secrets(rendered)
    if scrubbed == rendered:
        return value
    return scrubbed


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so a log stack parses rather than guesses.

    Fields: time, level, logger, message, and the three join keys (`correlation_id`, `session_id`,
    `actor`); a traceback goes into `exception` rather than trailing lines. `exception` is taken
    from `record.exc_text`, never re-rendered from `exc_info`, which would bypass the filter's
    redaction. The `redact_secrets` fallback covers a handler with no filter, minus the connector
    bearers only the filter resolves.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a compact JSON object."""
        swept = record.__dict__.get(_REDACTED_MARK, False)
        message = record.getMessage()
        payload: dict[str, Any] = {
            # ISO-8601 UTC with an explicit offset, so joins with `audit_events.ts` and span
            # timestamps need no guess at the pod's zone.
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            # Where the line was emitted. `logger` names a module, and a module here is routinely
            # a thousand lines; `source` is what turns a log search into a code location.
            "source": f"{record.module}.{record.funcName}:{record.lineno}",
            # Two concurrent turns in one pod are separable by `correlation_id` when it is set and
            # by nothing at all when it is not — which, in every worker process, is most lines.
            "process": record.process,
            "thread": record.threadName,
            "message": message if swept else redact_secrets(message),
        }
        # A claimed caller's identity is scrubbed here too when no filter swept the record — the
        # same no-filter fallback `message` gets, for a field a request header could set.
        scrub = not swept and record.__dict__.get(_CLAIMED_MARK, False)
        for key in _IDENTITY_FIELDS:
            value = getattr(record, key, "-")
            payload[key] = redact_secrets(value) if scrub and isinstance(value, str) else value
        # The caller's own fields, nested under `fields` so a caller cannot shadow `level`, `time`
        # or `correlation_id`.
        fields = structured_fields(record)
        if fields:
            payload["fields"] = {
                key: _redacted_field(value, swept) for key, value in fields.items()
            }
        if record.exc_text:
            payload["exception"] = record.exc_text
        elif record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        if record.stack_info:
            # Redacted here too, so `stack_info` is not an unredacted channel when no filter is
            # installed.
            payload["stack"] = record.stack_info if swept else redact_secrets(record.stack_info)
        return json.dumps(payload, default=str)
