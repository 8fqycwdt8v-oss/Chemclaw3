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
import re
from functools import lru_cache
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


def configure_telemetry() -> None:
    """Enable OpenTelemetry export if configured; install a no-op provider when it is not.

    Off unless `CHEMCLAW_OTEL_ENABLED=true`. When on, it calls MAF's `configure_otel_providers`
    exactly once, which reads the standard `OTEL_EXPORTER_OTLP_*` environment variables for the
    collector endpoint. That call needs the OpenTelemetry SDK + OTLP exporter extras installed;
    if they are missing we re-raise with a clear message naming the missing dependency, so an
    admin who flips the flag without the extras gets a directive error rather than a cryptic one.
    Called once per process at each worker's entrypoint, after `configure_logging`.

    **Off is not "do nothing"** — see `_install_noop_meter_provider`. Returning early was the
    default path in every deployment, and it is what leaked.
    """
    if not settings.otel_enabled:
        _install_noop_meter_provider()
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
    "postgres_migration_dsn",
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
_URL_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/\s:@]*)(?::([^/\s@]*))?@")


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
        default = getattr(field, "default", None)
        if isinstance(default, str) and default:
            published.add(default)
            if password := _dsn_password(default):
                published.add(password)
    return frozenset(published)


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
    return _URL_USERINFO.sub(_redact_userinfo, redacted)


def _plain(value: object) -> str:
    """The string a setting holds, whether it is a `str` or a `SecretStr`.

    **This function is the fix for a leak this repository introduced to itself.** Six credentials
    became `SecretStr` so a forgotten `.get_secret_value()` renders as `**********` instead of the
    key. `SecretStr` is not a `str` subclass, so the `isinstance(value, str)` guard that used to
    skip a non-string field started skipping *every one of them* — and `_secret_values()` returned
    an empty tuple, so `redact_secrets` passed a live `sk-live-...` through verbatim into any log
    line or traceback that quoted it.

    The mitigation that hid it is the same one that caused it: masked `repr` means the credential
    rarely reaches a log by accident, so the one path that mattered — a provider echoing the key in
    an auth error, git stderr persisted into `note_proposals.reason` — was the path nobody looked
    at. `tests/test_secret_settings.py` asserted the *names* were in the inventory and passed with
    the redaction dead.

    Anything that is neither shape yields `""`, which `_secret_values` skips: a non-credential type
    in the inventory is a mistake to notice, not a value to stringify into the match set.
    """
    if isinstance(value, str):
        return value
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else ""


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
        value = _plain(getattr(settings, name, ""))
        if not value:
            continue
        _consider(value)
        # A DSN's password is also worth matching on its own: libpq accepts several spellings and a
        # connection error may quote only the credential rather than the whole string. Considered
        # independently of the DSN, because the two can differ in whether they are published: a
        # schema-scoped test DSN is not the shipped default, but the password inside it still is.
        _consider(_dsn_password(value))
    for env_name in (
        _KNOWLEDGE_REPO_TOKEN_ENV,
        # The fleet's shared connector credential, named by config rather than held as a field —
        # so it is invisible to `_SECRET_SETTINGS` above, which matches settings whose *value* is
        # the secret. Read here rather than in the filter's `__init__` because, unlike the
        # per-connector bearer tokens beside it, resolving this name needs no `connectors` import:
        # it is one `Settings` field, and reading it per call keeps a rotated token covered.
        settings.connector_token_env,
        *connector_token_envs,
        *sorted(_RUNTIME_SECRET_ENVS),
    ):
        if not env_name:
            continue  # the deployment names no such credential
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
        """
        message = record.getMessage()
        redacted = redact_secrets(message, self._connector_token_envs)
        if redacted != message:
            # Collapsed to a plain message: the args have been folded in, and leaving them would
            # let a formatter re-render the original.
            record.msg = redacted
            record.args = None
        if record.exc_info is not None and record.exc_text is None:
            # `_EXC_RENDERER` is built once at module scope: constructing a `Formatter` here would
            # be per-record work, and `formatException` reaches `traceback`, which `logging` has
            # already imported — so nothing on this path imports anything (see `ContextFilter`).
            record.exc_text = _EXC_RENDERER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, self._connector_token_envs)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info, self._connector_token_envs)
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
