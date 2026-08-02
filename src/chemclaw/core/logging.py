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
or confidential chemistry, and that this is *intentional* — GxP requires an attributable "who did
what to which inputs" record. Redacting that would break the requirement the trail exists to meet.
The row asked for redaction and the honest reading of it is credentials, which have no such
justification and which nothing was protecting.
"""

import json
import logging
import os
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.session_context import get_current_session_id


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
    for handler in logging.getLogger().handlers:
        handler.addFilter(ContextFilter())
        handler.addFilter(SecretRedactingFilter())
        if settings.log_json:
            handler.setFormatter(JsonFormatter())


def configure_telemetry() -> None:
    """Enable OpenTelemetry export if configured — a no-op by default.

    Off unless `CHEMCLAW_OTEL_ENABLED=true`. When on, it calls MAF's `configure_otel_providers`
    exactly once, which reads the standard `OTEL_EXPORTER_OTLP_*` environment variables for the
    collector endpoint. That call needs the OpenTelemetry SDK + OTLP exporter extras installed;
    if they are missing we re-raise with a clear message naming the missing dependency, so an
    admin who flips the flag without the extras gets a directive error rather than a cryptic one.
    Called once per process at each worker's entrypoint, after `configure_logging`.
    """
    if not settings.otel_enabled:
        return
    # Bridge our one config value to the standard OTLP env var MAF/OTel read (F6-T5), so the
    # collector endpoint stays a single `CHEMCLAW_OTEL_ENDPOINT` like every other endpoint.
    if settings.otel_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", settings.otel_endpoint)
    from agent_framework.observability import configure_otel_providers

    try:
        configure_otel_providers(enable_sensitive_data=settings.otel_include_sensitive_data)
    except ImportError as exc:  # SDK/exporter extras not installed
        raise RuntimeError(
            "CHEMCLAW_OTEL_ENABLED=true but the OpenTelemetry SDK/OTLP exporter is not installed"
        ) from exc


# The settings whose *values* must never appear in a log line. Redaction works by matching the
# actual secret this process holds rather than by guessing at token-shaped strings: a pattern like
# `[A-Za-z0-9]{32,}` both misses a short key and mangles a molecule id, while an exact value match
# catches the secret no matter which route put it there — an exception message, an httpx error, a
# `repr` of a settings object — and cannot false-positive on anything else.
#
# Listed rather than derived from a name pattern (`*_token`, `*_secret`), because deriving would
# silently include `entra_token_endpoint` and `budget_max_tokens_per_user` — a URL and an integer —
# and silently *exclude* the next secret whose name does not match. A list is one line per addition
# and is visible in review, which is what a credential inventory should be.
_SECRET_SETTINGS = (
    "llm_api_key",
    "hpc_api_token",
    "hpc_artifact_store_token",
    "temporal_api_key",
    "postgres_dsn",
    "session_store_dsn",
    "note_webhook_secret",
    "audit_anchor_secret",
)

# The git push credential for the knowledge-sync sidecar (`deploy/knowledge-sync.sh`). It has no
# `Settings` field — nothing in this process reads it as config, only the sidecar script does —
# but `_helpers.tpl` ranges over every secret key for every component, so it sits in this process's
# environment regardless. A `Settings` field would be a config seam for a value nothing here
# configures; reading the one environment variable a redaction inventory actually needs is simpler
# than inventing one.
_KNOWLEDGE_REPO_TOKEN_ENV = "CHEMCLAW_KNOWLEDGE_REPO_TOKEN"

# Below this length a "secret" is more likely to be a placeholder, an empty default, or a string
# that occurs in ordinary prose — redacting it would corrupt every line containing that substring.
_MIN_REDACTABLE = 8

_REDACTED = "***"


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
    for name in _SECRET_SETTINGS:
        value = getattr(settings, name, "")
        if isinstance(value, str) and len(value) >= _MIN_REDACTABLE:
            values.add(value)
            # A DSN's password is also worth matching on its own: libpq accepts several spellings
            # and a connection error may quote only the credential rather than the whole string.
            if "://" in value and "@" in value:
                userinfo = value.split("://", 1)[1].split("@", 1)[0]
                if ":" in userinfo:
                    password = userinfo.split(":", 1)[1]
                    if len(password) >= _MIN_REDACTABLE:
                        values.add(password)
    for env_name in (_KNOWLEDGE_REPO_TOKEN_ENV, *connector_token_envs):
        value = os.environ.get(env_name, "")
        if len(value) >= _MIN_REDACTABLE:
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


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
        nothing *extra* rather than blocking `configure_logging()`, and says so at WARNING: a
        redaction inventory that quietly stopped covering connectors would be a worse outcome than
        a boot that proceeds without them.
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
            logging.getLogger(__name__).warning(
                "could not resolve connector bearer-token env names for log redaction; "
                "connector credentials will not be scrubbed from log lines this process",
                exc_info=True,
            )

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and always keep the record."""
        secrets = _secret_values(self._connector_token_envs)
        if not secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in secrets:
            redacted = redacted.replace(secret, _REDACTED)
        if redacted != message:
            # Collapsed to a plain message: the args have been folded in, and leaving them would
            # let a formatter re-render the original.
            record.msg = redacted
            record.args = None
        return True


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
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a compact JSON object."""
        payload: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
            "actor": getattr(record, "actor", "-"),
            "session_id": getattr(record, "session_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
