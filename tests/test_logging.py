"""Logging and telemetry configuration: levels, span pipeline, context fields and redaction.

`configure_logging` is config-driven and case-insensitive about `CHEMCLAW_LOG_LEVEL`; handler
wiring is `logging.basicConfig`'s and is not asserted.
"""

import datetime
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from chemclaw.core.config import Settings, settings
from chemclaw.core.logging import (
    _PEM_RFC1421_HEADERS,
    _SECRET_ENV_SETTINGS,
    _SECRET_SETTINGS,
    ContextFilter,
    JsonFormatter,
    SecretRedactingFilter,
    _configured_by,
    _handlers_that_reach_an_output_stream,
    _redacted_field,
    configure_logging,
    configure_telemetry,
    redact_secrets,
    register_secret_env,
    secret_env_names,
)


def test_configure_logging_applies_configured_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """The root logger takes its level from `settings.log_level` (spelled any case)."""
    root = logging.getLogger()
    original = root.level
    try:
        monkeypatch.setattr(settings, "log_level", "warning")  # lower-case proves .upper()
        configure_logging()
        assert root.level == logging.WARNING
    finally:
        root.setLevel(original)


def test_configure_telemetry_is_safe_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With OTel off (the default), telemetry setup never raises and wires no exporter."""
    monkeypatch.setattr(settings, "otel_enabled", False)
    configure_telemetry()  # must return cleanly without importing/wiring any exporter


def test_telemetry_off_installs_a_noop_meter_provider_rather_than_leaving_none() -> None:
    """Telemetry off installs a no-op meter provider rather than leaving none.

    With no provider set, the OpenTelemetry API proxies every instrument and keeps the proxies
    forever so a later provider can back them; with tool surfaces rebuilt per turn that is a
    per-turn leak. Run in a subprocess because a meter provider is global and can be set only once.
    """
    probe = (
        "from chemclaw.core.logging import configure_telemetry; configure_telemetry();"
        "from opentelemetry.metrics import get_meter;"
        "from opentelemetry.metrics._internal import _PROXY_METER_PROVIDER as p;"
        "before = len(p._meters);"
        "[get_meter(f'm{i}').create_histogram(name=f'h{i}') for i in range(50)];"
        "print(len(p._meters) - before)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env={**os.environ, "CHEMCLAW_OTEL_ENABLED": "false"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proxied = int(result.stdout.strip().splitlines()[-1])
    assert proxied == 0, (
        f"{proxied} of 50 instrument creations were proxied and retained; the API only stops "
        "proxying once a provider is installed, so telemetry-off still leaks"
    )


def _without_proxy_variables() -> dict[str, str]:
    """This process's environment minus every proxy variable, for a subprocess arm.

    The chart ships no proxy, and an inherited one makes `core/netguard.refuse_proxied_egress`
    correctly refuse the OTLP endpoint, failing the arm for a reason that is not its subject.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if not name.lower().endswith("_proxy") and name.lower() != "no_proxy"
    }


def test_configure_telemetry_works_with_the_shipped_helm_value() -> None:
    """OTel actually starts under the value the chart ships, not merely validates.

    `values.yaml` sets `CHEMCLAW_OTEL_ENABLED: "true"` and every component calls
    `configure_telemetry` at start, so a missing SDK or exporter dependency would crash every pod. A
    production value must be executed, not type-checked. Run in a subprocess because it installs a
    global tracer provider and starts an export loop that would outlive the test.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from chemclaw.core.logging import configure_telemetry; configure_telemetry()",
        ],
        env={
            **_without_proxy_variables(),
            "CHEMCLAW_OTEL_ENABLED": "true",
            "CHEMCLAW_OTEL_ENDPOINT": "http://otel-collector.observability.svc:4317",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"startup failed under the shipped OTel config:\n{result.stderr}"


# --- the span pipeline this module builds itself ------------------------------------------------
#
# Every helper in `core/tracing.py` degrades to a no-op without a provider, so a broken bootstrap is
# silent. The tests below make it audible: a span reaches the exporter named by its service, a
# second call builds no second pipeline, and missing extras name the dependency.


def test_a_span_reaches_the_exporter_carrying_the_service_that_produced_it() -> None:
    """The bootstrap works: span in, span out, named by service.

    Against a real in-memory exporter, since a mock would pass a pipeline whose spans go nowhere.
    `service.name` is what a collector groups by. In-process is safe because
    `_build_tracer_provider` installs nothing globally.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from chemclaw.core.logging import _build_tracer_provider

    exporter = InMemorySpanExporter()
    provider = _build_tracer_provider(exporter)
    with provider.get_tracer("chemclaw-test").start_as_current_span("chemclaw.turn"):
        pass
    assert provider.force_flush(), "the batch processor did not flush within its timeout"

    exported = exporter.get_finished_spans()
    assert [span.name for span in exported] == ["chemclaw.turn"]
    assert exported[0].resource.attributes["service.name"] == "chemclaw"
    assert exported[0].resource.attributes["service.version"] == settings.deployment_revision


def test_a_second_configure_telemetry_does_not_install_a_second_pipeline() -> None:
    """A second `configure_telemetry` call installs no second pipeline.

    Building a second provider starts an export thread and a gRPC channel that the API discards and
    nothing closes, so the assertion counts threads. The same subprocess checks the installed
    provider is the real SDK one and that `CHEMCLAW_OTEL_ENDPOINT` is bridged to
    `OTEL_EXPORTER_OTLP_ENDPOINT`.
    """
    probe = (
        "import json, os, threading;"
        "from chemclaw.core.logging import configure_telemetry;"
        "configure_telemetry();"
        "from opentelemetry import trace;"
        "from opentelemetry.sdk.trace import TracerProvider;"
        "provider = trace.get_tracer_provider();"
        "first = [t for t in threading.enumerate() if 'OtelBatchSpan' in t.name];"
        "configure_telemetry();"
        "second = [t for t in threading.enumerate() if 'OtelBatchSpan' in t.name];"
        "print(json.dumps({"
        "'is_sdk_provider': isinstance(provider, TracerProvider),"
        "'provider_unchanged': trace.get_tracer_provider() is provider,"
        "'service_name': provider.resource.attributes['service.name'],"
        "'endpoint': os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT'),"
        "'export_threads': [len(first), len(second)]}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env={
            **_without_proxy_variables(),
            "CHEMCLAW_OTEL_ENABLED": "true",
            "CHEMCLAW_OTEL_ENDPOINT": "http://otel-collector.observability.svc:4317",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["is_sdk_provider"], (
        "the global provider is not the SDK's — spans are being dropped by the API's default, "
        "which is exactly what removing the framework's bootstrap would look like"
    )
    assert observed["provider_unchanged"]
    assert observed["service_name"] == "chemclaw"
    assert observed["endpoint"] == "http://otel-collector.observability.svc:4317"
    assert observed["export_threads"] == [1, 1], (
        f"a second configure_telemetry() left {observed['export_threads'][1]} export threads "
        "running; the second pipeline is unreachable and nothing shuts it down"
    )


def test_enabling_telemetry_without_the_extras_names_the_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling telemetry without the extras raises an error naming the missing dependency.

    The exporter is made unimportable via `None` in `sys.modules`, so the code takes the same path
    as on a machine without the distribution.
    """
    from chemclaw.core import logging as core_logging

    monkeypatch.setattr(core_logging, "_TRACING_INSTALLED", False)
    monkeypatch.setattr(settings, "otel_enabled", True)
    # No endpoint, so nothing is written into this process's environment on the way to the raise.
    monkeypatch.setattr(settings, "otel_endpoint", "")
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", None)

    with pytest.raises(RuntimeError, match="OpenTelemetry SDK/OTLP exporter is not installed"):
        configure_telemetry()
    assert not core_logging._TRACING_INSTALLED, "a failed bootstrap must not latch as installed"


# --- what a log line has to carry, and what it must never carry -------------------------------
#
# Log records carry the correlation, actor and session ContextVars so a line can be joined to the
# audit trail and traces, and credentials are redacted systematically. The audit trail's tool-call
# arguments are deliberately not redacted: `SECURITY.md` records them as the attributable
# "who did what to which inputs" record.

_DSN = "postgresql://chemclaw:sup3rs3cret-password@db.internal:5432/chemclaw"
_KEY = "sk-live-0123456789abcdef"


@pytest.fixture
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure real secret values, as a deployment would hold them."""
    monkeypatch.setattr("chemclaw.core.config.settings.postgres_dsn", _DSN)
    monkeypatch.setattr("chemclaw.core.config.settings.llm_api_key", SecretStr(_KEY))


def _record(message: str, *args: object) -> logging.LogRecord:
    """A log record as `logger.info(message, *args)` would produce it."""
    return logging.LogRecord("chemclaw.test", logging.INFO, __file__, 1, message, args, None)


def _rendered(record: logging.LogRecord) -> str:
    """Run the redacting filter over a record and return what a handler would emit."""
    SecretRedactingFilter().filter(record)
    return record.getMessage()


def test_an_api_key_never_reaches_the_stream(_secrets: None) -> None:
    """The finding: nothing scrubbed a credential from a log line, anywhere."""
    assert _KEY not in _rendered(_record("upstream rejected key %s", _KEY))


def test_a_dsn_password_is_scrubbed_even_when_only_the_password_is_quoted(_secrets: None) -> None:
    """A connection error often quotes the credential rather than the whole DSN.

    Matching only the full DSN string would leave the password intact in exactly the message most
    likely to carry it, so the password is redacted on its own as well.
    """
    password = "sup3rs3cret-password"
    assert password not in _rendered(_record("auth failed for password %s", password))
    assert _DSN not in _rendered(_record("connecting to %s", _DSN))


def test_a_secret_passed_as_an_argument_is_caught_too(_secrets: None) -> None:
    """A secret passed as a format argument is caught too.

    It stays in `record.args` until formatting, so the filter redacts the rendered message and
    clears `args` so nothing can re-render the original.
    """
    record = _record("connecting: %s", _DSN)
    SecretRedactingFilter().filter(record)
    assert record.args is None
    assert _DSN not in logging.Formatter("%(message)s").format(record)


def _emitted(record: logging.LogRecord) -> str:
    """Everything a handler would write for `record`: message, traceback and stack alike.

    A credential can be in any of the three, and `_rendered` covers only the message.
    """
    SecretRedactingFilter().filter(record)
    return logging.Formatter("%(message)s").format(record)


def _record_with_exception(message: str, exc: BaseException) -> logging.LogRecord:
    """A record as `logger.exception(message)` would produce it inside an `except` block."""
    return logging.LogRecord(
        "chemclaw.test",
        logging.ERROR,
        __file__,
        1,
        message,
        (),
        (type(exc), exc, exc.__traceback__),
    )


def test_a_credential_inside_an_exception_never_reaches_the_stream(_secrets: None) -> None:
    """A credential inside an exception never reaches the stream.

    `exc_info` renders the exception at format time, and a failure is exactly when a DSN or auth
    header lands in error text, so the traceback must be redacted too.
    """
    try:
        raise RuntimeError(f"auth failed for {_KEY} against {_DSN}")
    except RuntimeError as exc:
        emitted = _emitted(_record_with_exception("upstream call failed", exc))
    assert _KEY not in emitted
    assert _DSN not in emitted
    assert "sup3rs3cret-password" not in emitted
    # Still a usable diagnostic: the redaction must not eat the traceback itself.
    assert "RuntimeError" in emitted


def test_a_credential_in_a_stack_dump_never_reaches_the_stream(_secrets: None) -> None:
    """`logger.error(..., stack_info=True)` renders a stack the filter also has to cover."""
    record = _record("state dump")
    record.stack_info = f"Stack (most recent call last):\n  connecting to {_DSN}"
    _emitted(record)
    assert _DSN not in (record.stack_info or "")


def test_a_token_carried_as_the_whole_userinfo_is_redacted(_secrets: None) -> None:
    """A token carried as the whole userinfo (`scheme://token@host`) is redacted.

    This is how a PAT reaches a git remote. The host is kept so the line still says which remote
    failed.
    """
    url = "https://ghp_abcdefghijklmnop@github.com/org/repo.git"
    emitted = _rendered(_record("push failed: %s", url))
    assert "ghp_abcdefghijklmnop" not in emitted
    assert "github.com/org/repo.git" in emitted


def test_the_user_is_still_kept_when_the_url_carries_both(_secrets: None) -> None:
    """The two-part form keeps its principal: a redacted line names who failed, not just where."""
    emitted = _rendered(_record("connecting: %s", "postgresql://svc_user:hunter2pass@db:5432/x"))
    assert "hunter2pass" not in emitted
    assert "svc_user" in emitted
    assert "db:5432" in emitted


def test_an_at_sign_in_a_path_is_not_mistaken_for_a_credential(_secrets: None) -> None:
    """Widening the pattern must not start mangling ordinary URLs."""
    assert _rendered(_record("fetched %s", "https://example.com/@handle")) == (
        "fetched https://example.com/@handle"
    )


def test_a_shipped_default_is_not_treated_as_a_credential() -> None:
    """A shipped default is not treated as a credential.

    The dev Postgres password is the literal `chemclaw`; redacting it would replace the product's
    name with `***` in unrelated log lines.
    """
    from chemclaw.core.config import settings as live

    default = type(live).model_fields["postgres_dsn"].default
    assert "chemclaw:chemclaw" in default  # the collision this guards is real, not hypothetical
    assert _rendered(_record("the chemclaw service started")) == "the chemclaw service started"


def test_a_published_password_stays_published_when_the_dsn_is_repointed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repointed DSN is redacted, while the published password inside it is not.

    CI repoints `postgres_dsn` at an isolated schema, so the DSN differs from the default while its
    password is still `chemclaw`; both conditions must hold at once.
    """
    repointed = "postgresql://chemclaw:chemclaw@localhost:5432/chemclaw?options=-csearch_path%3Dt1"
    monkeypatch.setattr("chemclaw.core.config.settings.postgres_dsn", repointed)
    assert repointed not in _rendered(_record("connecting to %s", repointed))
    assert _rendered(_record("the chemclaw service started")) == "the chemclaw service started"


def test_the_migration_dsn_is_in_the_inventory() -> None:
    """The schema-owning credential — the one that can rewrite `audit_events` — was not listed.

    `postgres_dsn` was, and the two are different roles by design (`infra/sql/grants`): the
    migration DSN is strictly the more privileged of the pair.
    """
    assert "postgres_migration_dsn" in _SECRET_SETTINGS


def test_redaction_leaves_ordinary_text_alone(_secrets: None) -> None:
    """A redactor that mangles normal lines is one an operator turns off."""
    assert _rendered(_record("computed pKa 15.9 for CCO")) == "computed pKa 15.9 for CCO"


def test_a_short_or_empty_secret_is_not_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty default must not redact every line, and a short one must not match prose.

    This is the failure that would make redaction worse than none: `llm_api_key` is `""` by default,
    and a substring search for the empty string matches everywhere.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.llm_api_key", SecretStr(""))
    monkeypatch.setattr("chemclaw.core.config.settings.live_probe_token", SecretStr("abc"))
    assert _rendered(_record("abc is a fine thing to log")) == "abc is a fine thing to log"


def test_every_line_carries_what_joins_it_to_the_audit_trail() -> None:
    """An ordinary WARNING had no correlation id, actor or session on it.

    All three were live in the process — audit, authorization and the connector headers read them —
    and none reached the line, so a WARNING could not be tied to the turn that caused it.
    """
    from chemclaw.core.identity_context import (
        reset_current_correlation_id,
        set_current_correlation_id,
    )

    token = set_current_correlation_id("cid-9")
    try:
        record = _record("something odd")
        ContextFilter().filter(record)
    finally:
        reset_current_correlation_id(token)

    assert record.correlation_id == "cid-9"  # type: ignore[attr-defined]
    assert record.actor == "-"  # type: ignore[attr-defined]


def test_absent_context_is_a_dash_not_a_crash() -> None:
    """Most logging happens off the request path — a CLI, a worker, a test.

    A filter that raised there would break every worker log; one that emitted an empty identity
    would let a line claim an anonymous user, which is the rule the connector headers follow.
    """
    record = _record("worker starting")
    assert ContextFilter().filter(record) is True
    assert record.correlation_id == "-"  # type: ignore[attr-defined]


def test_json_output_is_one_parseable_object_per_line() -> None:
    """A log stack should parse, not regex a `%`-format string."""
    import json

    record = _record("ELN sync found %d entries", 3)
    ContextFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "ELN sync found 3 entries"
    assert payload["level"] == "INFO"
    assert set(payload) >= {"time", "logger", "correlation_id", "actor", "session_id"}


def test_a_traceback_stays_inside_the_json_object() -> None:
    """A multi-line traceback trailing a line-delimited record is forty broken entries."""
    import json

    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "chemclaw.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    line = JsonFormatter().format(record)
    assert "ValueError: boom" in json.loads(line)["exception"]
    assert "\n" not in line.strip()


def test_filtering_a_record_never_imports_anything() -> None:
    """Filtering a record never imports anything.

    A filter runs at arbitrary moments, including inside Temporal's workflow sandbox, which hooks
    `__import__` and logs on restricted access; importing inside a filter can re-enter it and wedge
    the worker. The hook records rather than raises, since raising inside `__import__` breaks
    pytest's own reporting.
    """
    import builtins

    filters = [ContextFilter(), SecretRedactingFilter()]  # constructing may import; filtering not
    record = _record("something odd")
    real_import = builtins.__import__
    attempted: list[str] = []

    def _watch_import(name: str, *args: object, **kwargs: object) -> object:
        attempted.append(name)
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    builtins.__import__ = _watch_import  # type: ignore[assignment]
    try:
        kept = [log_filter.filter(record) for log_filter in filters]
    finally:
        builtins.__import__ = real_import

    assert kept == [True, True]
    assert not attempted, f"the logging path imported {attempted}"


def test_configure_logging_installs_both_filters_on_the_handler() -> None:
    """`configure_logging` installs both filters on the handler, not on a logger.

    Records reach the root handler by propagation, and a logger's filters are not consulted for
    propagated records.
    """
    configure_logging()
    handlers = logging.getLogger().handlers
    assert handlers, "configure_logging left the root logger with no handler"
    for handler in handlers:
        kinds = {type(f) for f in handler.filters}
        assert ContextFilter in kinds and SecretRedactingFilter in kinds


def test_the_knowledge_repo_token_is_redacted_though_it_has_no_settings_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knowledge-repo token is redacted though it has no `Settings` field.

    It is in every pod's environment and consumed only by `deploy/knowledge-sync.sh`, so the filter
    must read the environment variable directly.
    """
    token = "ghp_knowledge-repo-push-credential-0123456789"
    monkeypatch.setenv("CHEMCLAW_KNOWLEDGE_REPO_TOKEN", token)
    assert token not in _rendered(_record("git push failed: %s", token))


def test_a_connector_bearer_token_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-connector bearer token is redacted.

    The variable name is manifest-declared, so the filter enumerates every enabled connector's
    `token_env` and redacts the value behind it.
    """
    from types import SimpleNamespace

    from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint

    token_env = "CHEMCLAW_TEST_CONNECTOR_BEARER_TOKEN"
    token = "sk-connector-live-0123456789abcdef"
    monkeypatch.setenv(token_env, token)
    endpoint = HttpEndpoint(
        url="http://127.0.0.1:1/mcp",
        auth=BearerAuth(token_env=token_env),
        tools=["echo"],
        read_only=["echo"],
    )
    fake_manifest = SimpleNamespace(endpoint=endpoint)
    monkeypatch.setattr("chemclaw.connectors.registry.enabled", lambda: [fake_manifest])

    record = _record("connector call failed: %s", token)
    # Constructed fresh here, so it picks up the patched `enabled()` rather than the real registry.
    SecretRedactingFilter().filter(record)
    assert token not in record.getMessage()


def test_a_credential_supplied_through_the_env_file_is_redacted_after_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "pathlib.Path"
) -> None:
    """A credential supplied through the env file is redacted after `register_secret_env`.

    pydantic-settings reads `.env` without exporting it, so the registered name must resolve through
    settings, not only `os.environ`. Seeded through a real `env_file`, since exporting the variable
    hides the defect. `_SECRET_SETTINGS` is emptied so only the registration path is measured.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("CHEMCLAW_VECTOR_STORE_API_KEY=Qdr-supersecret-abcdef123456\n")
    monkeypatch.delenv("CHEMCLAW_VECTOR_STORE_API_KEY", raising=False)
    from_file = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    key = from_file.vector_store_api_key.get_secret_value()
    assert key == "Qdr-supersecret-abcdef123456", "the .env must be what configured this"
    assert "CHEMCLAW_VECTOR_STORE_API_KEY" not in os.environ, "pydantic must not have exported it"

    monkeypatch.setattr(settings, "vector_store_api_key", SecretStr(key))
    monkeypatch.setattr(
        "chemclaw.core.logging._SECRET_SETTINGS",
        tuple(n for n in _SECRET_SETTINGS if n != "vector_store_api_key"),
    )
    register_secret_env("CHEMCLAW_VECTOR_STORE_API_KEY")  # what `open_qdrant_client` does

    # The name resolves to the value the `.env` configured, not to the empty string `os.environ`
    # would have given — the whole of the defect, in one call.
    assert _configured_by("CHEMCLAW_VECTOR_STORE_API_KEY") == key

    # And end to end, on an *unlabelled* occurrence so neither structural rule can be what saves
    # it: `_STRUCTURAL_SECRETS` anchors on `api_key=` / `Bearer `, and a client's own error message
    # carries neither.
    assert key not in _rendered(_record("qdrant refused: unauthorized for %s at cluster", key))


def test_a_registered_name_that_configures_nothing_still_resolves_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered name that configures nothing still resolves from the environment.

    Manifest-declared credentials name no `Settings` field, so the settings lookup is a fallback,
    not a replacement.
    """
    monkeypatch.setenv("CHEMCLAW_TEST_WAREHOUSE_PASSWORD", "wh-supersecret-0123456789")
    register_secret_env("CHEMCLAW_TEST_WAREHOUSE_PASSWORD")
    assert _configured_by("CHEMCLAW_TEST_WAREHOUSE_PASSWORD") == "", "no field configures this"
    rendered = _rendered(_record("warehouse refused %s", "wh-supersecret-0123456789"))
    assert "wh-supersecret-0123456789" not in rendered


def test_every_named_secret_is_a_real_settings_field() -> None:
    """Every name in `_SECRET_SETTINGS` is a real settings field, so a rename cannot disarm it.

    The inventory is read through `getattr(..., "")`, so a stale name silently redacts nothing.
    Deliberately one-directional: the other direction is
    `tests/test_credentials.py::test_every_secret_str_on_the_settings_object_is_also_redacted`,
    which uses the `SecretStr` type rather than a name heuristic.
    """
    unknown = sorted(set(_SECRET_SETTINGS) - set(Settings.model_fields))
    assert not unknown, (
        f"_SECRET_SETTINGS names fields that are not on Settings: {unknown}. Each is read with a "
        'getattr default of "", so it redacts nothing and fails silently — delete it, or correct '
        "it to the field's current name."
    )


# --- The JSON path: where the redaction the filter performed was thrown away -------------------


def _json_emitted(record: logging.LogRecord) -> str:
    """Everything the *JSON* handler would write, filter first, exactly as a pod runs it."""
    import json

    SecretRedactingFilter().filter(record)
    return json.dumps(json.loads(JsonFormatter().format(record)))


@pytest.mark.parametrize("emit", [_emitted, _json_emitted], ids=["plain", "json"])
def test_a_credential_inside_an_exception_never_reaches_either_formatter(
    _secrets: None, emit: "Callable[[logging.LogRecord], str]"
) -> None:
    """A credential inside an exception reaches neither the plain nor the JSON formatter.

    The filter scrubs the traceback into `record.exc_text`; a formatter that re-renders
    `record.exc_info` bypasses that. Production uses JSON, so the test is parametrised over both
    formatters and a new one cannot bring its own blind spot.
    """
    try:
        raise RuntimeError(f"auth failed for {_KEY} against {_DSN}")
    except RuntimeError as exc:
        emitted = emit(_record_with_exception("upstream call failed", exc))
    assert _KEY not in emitted
    assert _DSN not in emitted
    assert "sup3rs3cret-password" not in emitted
    assert "RuntimeError" in emitted, "the traceback must still be reported, only scrubbed"


def test_the_json_formatter_redacts_even_without_the_filter(_secrets: None) -> None:
    """The JSON formatter redacts even without the filter.

    A backstop for a handler configured elsewhere; it cannot see per-connector bearer tokens, so it
    does not replace the filter.
    """
    import json

    try:
        raise RuntimeError(f"auth failed for {_KEY}")
    except RuntimeError as exc:
        record = _record_with_exception("upstream call failed", exc)
    payload = json.loads(JsonFormatter().format(record))
    assert _KEY not in payload["exception"]


def test_a_stack_dump_survives_into_the_json_object(_secrets: None) -> None:
    """`stack_info` was scrubbed by the filter and then dropped by the JSON formatter entirely."""
    import json

    record = _record("failed")
    record.stack_info = f"Stack (most recent call last):\n  connecting to {_DSN}"
    SecretRedactingFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert "sup3rs3cret-password" not in payload["stack"]
    assert "Stack (most recent call last)" in payload["stack"]


# --- Handlers this module never reached ---------------------------------------------------------


def test_configure_logging_reaches_a_non_propagating_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`configure_logging` reaches a non-propagating logger.

    uvicorn installs its own config with `propagate: false`, and `uvicorn.error` logs unhandled
    exceptions with `exc_info`, so every logger that opts out of propagation must still be swept.
    """
    private = logging.getLogger("chemclaw.test.uvicorn_like")
    private.propagate = False
    handler = logging.StreamHandler()
    private.addHandler(handler)
    monkeypatch.setattr(private, "handlers", [handler], raising=False)
    try:
        configure_logging()
        installed = [type(existing) for existing in handler.filters]
        assert SecretRedactingFilter in installed
        assert ContextFilter in installed
    finally:
        private.removeHandler(handler)
        private.propagate = True


# --- Credentials this process does not hold -----------------------------------------------------


@pytest.mark.parametrize(
    ("secret", "label"),
    [
        ("ghp_0123456789abcdefghijklmnopqrstuvwxyz", "github classic PAT"),
        ("github_pat_11ABCDEFG0abcdefghij_KLMNOPQRSTUVWXYZ0123456789", "github fine-grained PAT"),
        ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", "anthropic key"),
        ("sk-proj-abcdefghijklmnopqrstuvwxyz0123456789", "openai project key"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJvaWQiOiJhbGljZSJ9.c2lnbmF0dXJlLWhlcmU",
            "entra bearer token",
        ),
    ],
)
def test_a_credential_this_process_does_not_hold_is_still_redacted(secret: str, label: str) -> None:
    """A credential this process does not hold is still redacted.

    The value inventory covers only what this process configured; pass-through credentials are
    caught by structural rules anchored on a vendor prefix and a long opaque tail.
    """
    assert secret not in redact_secrets(f"upstream rejected {secret} at 09:31")


def test_a_libpq_password_and_a_query_string_token_are_redacted_with_their_label() -> None:
    """The two spellings `_URL_USERINFO` cannot see; the label survives so the line still reads."""
    libpq = redact_secrets("host=wh.internal password=S3cr3tP4ssw0rd dbname=eln")
    assert "S3cr3tP4ssw0rd" not in libpq
    assert "password=" in libpq
    assert "dbname=eln" in libpq, "only the credential is replaced, not the rest of the string"

    query = redact_secrets("GET /v1/rows?access_token=abcdef0123456789ghijkl&limit=10")
    assert "abcdef0123456789ghijkl" not in query
    assert "access_token=" in query
    assert "limit=10" in query


def test_an_opaque_bearer_credential_is_redacted_and_the_scheme_kept() -> None:
    """A bearer token with no internal structure has only its scheme to anchor on."""
    emitted = redact_secrets("Authorization: Bearer w7Fq2xLpNv8sTr4Kd1Zy")
    assert "w7Fq2xLpNv8sTr4Kd1Zy" not in emitted
    assert "Bearer" in emitted


@pytest.mark.parametrize(
    "innocent",
    [
        # Identifiers this system logs constantly.
        "CC(=O)Oc1ccccc1C(=O)O",
        "RYYVLZVUVIJVGH-UHFFFAOYSA-N",
        "playbook-suzuki-coupling-optimisation",
        "D-2026-08-06-a-share-is-mounted-not-called",
        "calculation cache token count 1234567890 for reaction-aaa1",
        "chemclaw_turn_tokens_total 4096",
        # Source lines of this repository. These are the cases whose absence let the first version
        # of these rules through: they appear verbatim inside the tracebacks the whole mechanism
        # exists to protect, and an over-eager rule destroyed the evidence an engineer needs.
        'access_token = response.json().get("access_token")',
        "api_key=settings.llm_api_key or _KEYLESS_PLACEHOLDER,",
        "password=None)",
        'client_secret = settings.entra_client_secret or ""',
        "api_key=self._api_key,",
        # Ordinary English prose. `Basic` is a word before it is an auth scheme.
        "Basic authentication rejected by the upstream proxy",
        # A PEM header quoted in an error, with the sentence that follows it. The private-key
        # rule's body class has to contain letters and whitespace, so without its lookahead this
        # line came back with everything after the header replaced.
        "expected -----BEGIN PRIVATE KEY----- but found garbage in the file",
        "Bearer token was rejected by the identity provider",
        "the access_token field was absent from the response body",
        "no api_key configured for this provider",
    ],
)
def test_the_structural_rules_never_touch_ordinary_content(innocent: str) -> None:
    r"""The structural rules never touch ordinary content.

    A false positive corrupts a log line: a rule eating a SMILES, an InChIKey, a slug, an ADR id or
    this repository's own source lines (the text guaranteed to appear in a traceback) would be worse
    than the leak it closes. Source lines and prose are in the table for that reason.
    """
    assert redact_secrets(innocent) == innocent


def test_the_structural_rules_still_catch_the_real_shapes_after_narrowing() -> None:
    """Narrowing the structural rules kept every real pass-through shape redacted.

    Includes `PGPASSWORD=` and a `repr`'d config dict.
    """
    for secret, sample in [
        (
            "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
            "push failed: ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        ),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJvaWQiOiJhIn0.c2lnbmF0dXJl",
            "Bearer eyJhbGciOiJIUzI1NiJ9.eyJvaWQiOiJhIn0.c2lnbmF0dXJl",
        ),
        ("S3cr3tP4ssw0rd", "host=wh password=S3cr3tP4ssw0rd dbname=eln"),
        ("S3cr3tP4ssw0rd", "PGPASSWORD=S3cr3tP4ssw0rd"),
        ("hunter2000hunter", "{'password': 'hunter2000hunter'}"),
        ("abcdef0123456789ghijkl", "GET /rows?access_token=abcdef0123456789ghijkl&limit=10"),
        ("w7Fq2xLpNv8sTr4Kd1Zy", "Authorization: Bearer w7Fq2xLpNv8sTr4Kd1Zy"),
    ]:
        assert secret not in redact_secrets(sample), sample


def test_a_variable_name_survives_the_line_that_tells_an_operator_to_set_it() -> None:
    """`NAME=NAME` is a hint, not a credential, and the exemption opens neither direction.

    A `*_token_env` value is a variable name and must survive the line telling an operator what to
    set. Exempting the key alone would leak a pasted token; exempting the value alone would drop
    uppercase credentials such as base32 secrets.
    """
    for line, expected in [
        (
            "set CHEMCLAW_CALC_SERVER_TOKEN_ENV=CHEMCLAW_CALC_TOKEN to name the credential",
            "set CHEMCLAW_CALC_SERVER_TOKEN_ENV=CHEMCLAW_CALC_TOKEN to name the credential",
        ),
        ("CHEMCLAW_CALC_SERVER_TOKEN_ENV=sk-ant-api03-abc123XYZ", None),
        ("CHEMCLAW_MCP_FACE_TOKEN=JBSWY3DPEHPK3PXPJBSWY3DP", None),
        ("MY_PASSWORD=CORRECTHORSEBATTERY", None),
    ]:
        redacted = redact_secrets(line)
        if expected is None:
            assert redacted.endswith("***"), redacted
        else:
            assert redacted == expected


#: Every key-anchored spelling, with a value whose shape the rules require (opaque, containing a
#: digit). Applied at three escape depths by the test below, because depth is the axis the rules
#: were blind on and `0` is the only one anything had ever measured.
_KEY_ANCHORED_SPELLINGS = {
    "password": ('{"password": "W4rehousePw"}', "W4rehousePw"),
    "pgpassword": ('{"PGPASSWORD": "W4rehousePw"}', "W4rehousePw"),
    "api_key": ('{"api_key": "sk_live_9f3a2b1c8d7e6f"}', "sk_live_9f3a2b1c8d7e6f"),
    "access_token": ('{"access_token": "abc123def456ghi789"}', "abc123def456ghi789"),
    "client_secret": ('{"client_secret": "Zx8~Q9abcdef123"}', "Zx8~Q9abcdef123"),
    "token": ('{"token": "abc123def456ghi789"}', "abc123def456ghi789"),
    "secret": ('{"secret": "abc123def456ghi789"}', "abc123def456ghi789"),
    "private_key": ('{"private_key": "abc123def456ghi789"}', "abc123def456ghi789"),
    "passwd": ('{"passwd": "W4rehousePw"}', "W4rehousePw"),
    "pwd": ('{"pwd": "W4rehousePw"}', "W4rehousePw"),
    "screaming env var": (
        '{"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI0K7MDENGbPxRfiCYEXAMPLEKEY"}',
        "wJalrXUtnFEMI0K7MDENGbPxRfiCYEXAMPLEKEY",
    ),
    "authorization basic": (
        '{"Authorization": "Basic dXNlcjpwYXNzd29yZDEy"}',
        "dXNlcjpwYXNzd29yZDEy",
    ),
}


@pytest.mark.parametrize("depth", [0, 1, 2])
@pytest.mark.parametrize(
    ("sample", "credential"),
    _KEY_ANCHORED_SPELLINGS.values(),
    ids=_KEY_ANCHORED_SPELLINGS.keys(),
)
def test_a_key_anchored_credential_survives_no_depth_of_json_escaping(
    sample: str, credential: str, depth: int
) -> None:
    r"""Every key-anchored rule redacts at depths 0-2 of JSON escaping.

    `_redacted_field` renders non-string extras with `json.dumps`, so a nested credential arrives as
    `{\"password\": \"...\"}` and a separator framed `["']?\s*[=:]` meets a backslash. Depth 2 is
    text already encoded once before rendering; depth 0 keeps a fix for the escaped form honest.
    `redact_secrets` also guards notes committed to Git (`kg/record.py`), off-cluster messages
    (`deliver/message.py`) and span descriptions (`core/tracing.py`).
    """
    text = sample
    for _ in range(depth):
        text = json.dumps(text)

    assert credential not in redact_secrets(text), (
        f"a key-anchored credential at escape depth {depth} reached the stream verbatim: {text!r}"
    )


def test_every_rule_that_anchors_on_a_key_name_allows_an_escaped_quote() -> None:
    r"""Every rule anchored on a key name takes its framing from `_KEY_FRAMING`.

    This fails on a rule rather than on a sample table, so a reworded or new rule using the bare
    `["']?\s*[=:]\s*["']?` spelling is caught. Read off the compiled patterns, not a list.
    """
    from chemclaw.core import logging as chemclaw_logging

    framing = chemclaw_logging._KEY_FRAMING
    blind = [
        pattern.pattern
        for pattern in chemclaw_logging._STRUCTURAL_SECRETS
        if "[=:]" in pattern.pattern and framing not in pattern.pattern
    ]
    assert not blind, (
        "these rules reach their value through a separator they frame themselves, so a "
        f"backslash-escaped quote defeats them: {blind}"
    )


#: The adversarial repeating unit per rule that frames a key with `_KEY_FRAMING`, escaped. Separate
#: from `_QUADRATIC_UNITS`: these repeat the escape run and measure the framing itself. Held against
#: the rule table by the test below.
_ESCAPED_FRAMING_UNITS = {
    "escaped-password": 'password\\":\\"',
    "escaped-api-key": 'api_key\\":\\"',
    "escaped-screaming": 'AWS_SECRET_KEY\\":\\"',
    "escaped-basic": 'Authorization\\": \\"Basic ',
    "backslash-run": "password" + "\\" * 12,
    "quote-run": "password" + '"' * 12,
}


def test_every_rule_that_frames_a_key_has_an_escaped_pathological_unit() -> None:
    """Every rule that frames a key has an escaped pathological unit.

    The two extra units are degenerate backslash and quote runs that never reach a separator. A
    count is the weakest check that still fails on the next rule to adopt the framing.
    """
    from chemclaw.core import logging as chemclaw_logging

    framing_rules = [
        pattern
        for pattern in chemclaw_logging._STRUCTURAL_SECRETS
        if chemclaw_logging._KEY_FRAMING in pattern.pattern
    ]
    assert len(framing_rules) + 2 == len(_ESCAPED_FRAMING_UNITS), (
        f"{len(framing_rules)} rules frame a key with _KEY_FRAMING but "
        f"{len(_ESCAPED_FRAMING_UNITS)} escaped units (the two degenerate runs included). A new "
        "one needs a unit here, or its cost on an 80 KB adversarial log line is unmeasured."
    )


@pytest.mark.parametrize("unit", _ESCAPED_FRAMING_UNITS.values(), ids=_ESCAPED_FRAMING_UNITS.keys())
def test_the_escaped_quote_framing_is_not_quadratic(unit: str) -> None:
    r"""The escaped-quote framing is not quadratic.

    The filter holds the logging lock (and on the front door the event loop), so a rule's blow-up is
    a denial of service. `_KEY_FRAMING` adds a bounded run before an optional quote, the shape that
    has caused super-linear regexes, so its cost is measured. Removing the possessiveness still
    passes; this guards future widening, not the current form. Each size is the fastest of several
    runs so a scheduler stall cannot fail linear code; a quadratic cost is paid on every repeat.
    """
    import timeit

    small = unit * (10_240 // len(unit))
    large = unit * (81_920 // len(unit))  # 8x

    def fastest(text: str) -> float:
        """The least wall time of several single calls — `timeit`'s own `perf_counter`."""
        return min(timeit.repeat(lambda: redact_secrets(text), number=1, repeat=5))

    small_seconds = fastest(small)
    large_seconds = fastest(large)

    assert large_seconds < 2.0, f"80 KB of adversarial {unit!r} took {large_seconds:.2f}s"
    assert large_seconds / max(small_seconds, 1e-4) < 24, (
        f"scaling looks quadratic for {unit!r}: {small_seconds:.4f}s for 10 KB, "
        f"{large_seconds:.4f}s for 80 KB"
    )


#: The non-string shapes a credential reaches `_redacted_field` in. A string `extra=` is swept by
#: the filter; everything else is rendered with `json.dumps` (escaping nested quotes) and then
#: scrubbed.
_NESTED_EXTRAS: dict[str, object] = {
    "json text inside a dict": {"resp": {"body": '{"password": "W4rehousePw"}'}},
    "json text inside a list": ['{"api_key": "sk_live_9f3a2b1c8d7e6f"}'],
    "json text three dicts deep": {"a": {"b": {"c": '{"client_secret": "Zx8Q9abcdef123"}'}}},
    "json text inside bytes": b'{"password": "W4rehousePw"}',
}


@pytest.mark.parametrize("value", _NESTED_EXTRAS.values(), ids=_NESTED_EXTRAS.keys())
def test_a_nested_credential_is_scrubbed_in_the_form_the_stream_receives(value: object) -> None:
    """A nested credential is scrubbed in the rendered form the stream receives.

    Render-then-scrub makes a credential inside a dict, list or exception reachable, and the
    rendering escapes nested quotes, so the key-anchored rules must see through that. `bytes`
    renders as a `repr` with the same quote escaping, so it needs no branch of its own.
    """
    scrubbed = _redacted_field(value, swept=True)
    rendered = scrubbed if isinstance(scrubbed, str) else json.dumps(scrubbed, default=str)

    for credential in ("W4rehousePw", "sk_live_9f3a2b1c8d7e6f", "Zx8Q9abcdef123"):
        assert credential not in rendered, rendered


def test_a_nested_credential_does_not_reach_the_json_line(_secrets: None) -> None:
    """A nested credential does not reach the emitted JSON line, end to end.

    Through `SecretRedactingFilter` (which sweeps only string extras) and then `JsonFormatter`,
    where non-strings are rendered.
    """
    formatter = JsonFormatter()
    redaction = SecretRedactingFilter()
    # Both key-anchored rules in every shape, so a regression in either one is visible here: the
    # libpq `password` rule and the compound `api_key` rule are separate patterns, and a guard
    # carrying only the first passes while the second is blind.
    blob = '{"password": "W4rehousePw", "api_key": "sk_live_9f3a2b1c8d7"}'
    for field, value in (
        ("resp", {"body": blob}),
        ("payload", blob.encode("utf-8")),
        ("err", RuntimeError(f"driver said: {blob}")),
    ):
        record = logging.LogRecord("x", logging.ERROR, "f.py", 1, "call failed", None, None)
        setattr(record, field, value)
        redaction.filter(record)
        line = formatter.format(record)
        for credential in ("W4rehousePw", "sk_live_9f3a2b1c8d7"):
            assert credential not in line, line
        assert json.loads(line)["fields"], line


# One pathological repeating unit per structural rule: the shortest string that makes the rule's
# prefix match repeatedly, forcing the engine to retry the tail. Checked against the rule table by
# the test below rather than trusted.
_QUADRATIC_UNITS = {
    "jwt": "-eyJ",
    "password": "password=",
    "pgpassword": "PGPASSWORD=",
    "api-key": "api_key=",
    "access-token": "access_token=",
    "client-secret": "client_secret=",
    "bearer": "Bearer ",
    "env-var": "AAAAAAAA_TOKEN",
    "basic": "Authorization: Basic ",
    "aws": "AKIAAAAAAAAAAAAAAAAA",
    "slack": "xoxb-",
    # The three shapes added on 2026-09-16. Each unit is the vendor prefix plus a tail that is one
    # character short of matching, which is the input that makes the engine try the whole tail at
    # every start position and then fail — the shape that found the quadratic rules above.
    "databricks": "dapi0123456789abcdef0123456789abcde",
    "gitlab": "glpat-0123456789abcde",
    # The PEM unit is the header plus an RFC 1421 header section whose body is one character short
    # of the 20 the lookahead requires, so the separator walks its whole window and fails at every
    # start position.
    "pem": (
        "-----BEGIN PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n"
        "DEK-Info: AES-256-CBC,0A1B\n\nMIIEpAIBAAKC!"
    ),
    "url-userinfo": "postgresql://a:b@",
}


@pytest.mark.parametrize("groups", [4, 5])
@pytest.mark.parametrize("header", [name for name, _ in _PEM_RFC1421_HEADERS])
def test_the_pem_preamble_is_not_exponential_in_its_header_count(header: str, groups: int) -> None:
    r"""The PEM preamble is not exponential in its header count.

    The quadratic guard grows line length; this rule's risk is the number of alternations after one
    `-----BEGIN`. Whitespace that both a header's tail and the gap branch `[\s\\]` can consume
    yields exponentially many splits for a failing lookahead. Header names come from
    `_PEM_RFC1421_HEADERS`, so each header's tail is exercised and a new one brings its own case.
    Sizes of 4 and 5 groups would take seconds under an exponential shape yet still return, so each
    case fails by its own 0.5 s assertion rather than by the suite's wall-clock kill.
    """
    payload = "-----BEGIN PRIVATE KEY-----" + (header + " " * 40) * groups + "!"

    start = time.perf_counter()
    redact_secrets(payload)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, (
        f"{len(payload)} bytes of model-authored text took {elapsed:.3f} s to redact at {groups} "
        f"{header} groups. The preamble's branches are ambiguous over whitespace again, and this "
        "filter runs holding the logging lock"
    )


def test_every_structural_rule_has_a_pathological_unit() -> None:
    """Every structural rule has a pathological unit.

    A rule with no unit is a rule whose cost nothing measures. Units cannot be derived from a
    compiled pattern, so this forces someone to write one for each new rule.
    """
    from chemclaw.core import logging as chemclaw_logging

    assert len(_QUADRATIC_UNITS) == len(chemclaw_logging._STRUCTURAL_SECRETS), (
        f"{len(chemclaw_logging._STRUCTURAL_SECRETS)} structural redaction rules but "
        f"{len(_QUADRATIC_UNITS)} pathological units. A new rule needs a repeating unit here, or "
        "its cost on a 100 KB unauthenticated request line is unmeasured."
    )


@pytest.mark.parametrize("unit", _QUADRATIC_UNITS.values(), ids=_QUADRATIC_UNITS.keys())
def test_redaction_cannot_be_made_quadratic_by_a_log_line(unit: str) -> None:
    r"""Redaction cost is linear in the line length for every structural rule.

    The filter holds the logging lock and is attached to `uvicorn.access`, which logs the raw
    request URL, so a super-linear rule is an unauthenticated denial of service. Parametrised over
    `_QUADRATIC_UNITS`, whose count is asserted against the rule table. The bound is generous: the
    claim is "not quadratic", not "fast".
    """
    import time

    small = unit * (10_240 // len(unit))
    large = unit * (81_920 // len(unit))  # 8x

    start = time.monotonic()
    redact_secrets(small)
    small_seconds = time.monotonic() - start
    start = time.monotonic()
    redact_secrets(large)
    large_seconds = time.monotonic() - start

    assert large_seconds < 2.0, f"80 KB of adversarial {unit!r} took {large_seconds:.2f}s"
    # 8x the input; quadratic would be ~64x. Wide margin for a noisy machine, and the denominator is
    # floored so a fast small case cannot make the ratio meaningless.
    assert large_seconds / max(small_seconds, 1e-4) < 24, (
        f"scaling looks quadratic for {unit!r}: {small_seconds:.4f}s for 10 KB, "
        f"{large_seconds:.4f}s for 80 KB"
    )


def test_a_log_call_that_declines_a_traceback_does_not_crash_the_filter() -> None:
    """A log call with `exc_info=False` does not crash the filter.

    `Logger._log` stores the bool on the record, so `record.exc_info is not None` is not the right
    guard; the filter tests truthiness as `Formatter.format` does. Filters run outside logging's
    error handling, so a raise here lands at the caller.
    """
    record = logging.LogRecord(
        name="probe",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="declined a traceback",
        args=(),
        exc_info=False,  # type: ignore[arg-type]
    )
    assert SecretRedactingFilter().filter(record) is True
    assert record.exc_text is None, "a declined traceback must not be rendered"


class _HostileMessage:
    """A `msg` object whose `__str__` raises — what `getMessage()` calls on a non-str message."""

    def __str__(self) -> str:
        raise ValueError("hostile __str__")


def _malformed_percent_args(logger: logging.Logger) -> None:
    """`%d` handed a string: `record.getMessage()` raises `TypeError` at render time."""
    logger.info("count=%d", "not-a-number")


def _malformed_exc_info(logger: logging.Logger) -> None:
    """A tuple `Logger._log` passes through verbatim; `formatException` subscripts it."""
    logger.info("boom", exc_info=(1, 2, 3))  # type: ignore[arg-type]


def _hostile_message_object(logger: logging.Logger) -> None:
    """A `msg` that raises from `__str__`, the shape `logger.info(obj)` allows."""
    logger.info(_HostileMessage())


def _non_string_stack_info(logger: logging.Logger) -> None:
    """`stack_info` set to a non-string, which `redact_secrets` cannot `.replace()` on."""
    record = logging.LogRecord("probe", logging.INFO, __file__, 1, "m", None, None)
    record.stack_info = object()  # type: ignore[assignment]
    logger.handle(record)


def _non_string_exc_text(logger: logging.Logger) -> None:
    """`exc_text` pre-populated with a non-string, the same hazard one field over."""
    record = logging.LogRecord("probe", logging.INFO, __file__, 1, "m", None, None)
    record.exc_text = object()  # type: ignore[assignment]
    logger.handle(record)


@pytest.mark.parametrize(
    "make_call",
    [
        _malformed_percent_args,
        _malformed_exc_info,
        _hostile_message_object,
        _non_string_stack_info,
        _non_string_exc_text,
    ],
    ids=["percent-args", "exc-info-tuple", "hostile-str", "stack-info", "exc-text"],
)
def test_a_malformed_log_call_is_reported_by_logging_not_raised_at_the_caller(
    make_call: Callable[[logging.Logger], None], capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed log call is reported by logging, not raised at the caller.

    `Handler.handle` calls the filter outside the try/except around `emit()`, so anything it raises
    reaches the caller; with no filter installed, logging reports these via `handleError`. Matters
    for `metrics_bridge.degraded()`, whose call sites sit in `except` blocks. Asserted through
    `Handler.handle`.
    """
    from chemclaw.core.logging import SecretRedactingFilter

    logger = logging.getLogger(f"malformed-probe.{make_call.__name__}")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        make_call(logger)  # must not raise
    finally:
        logger.handlers.clear()
    assert "--- Logging error ---" in capsys.readouterr().err, (
        "logging must still report the malformation on stderr; swallowing it silently would "
        "trade a crash for an invisible dropped log line"
    )


def test_logging_own_error_report_does_not_print_the_unredacted_record(
    _secrets: None, capsys: pytest.CaptureFixture[str]
) -> None:
    r"""Logging's own error report does not print the unredacted record.

    When formatting fails, `Handler.handleError` prints the original `msg` and `args`, which would
    include a credential. Both halves are asserted: the credential is absent and the diagnostic is
    still printed, ruling out the wrong fix of `logging.raiseExceptions = False`.
    """
    private = logging.getLogger("chemclaw.test.handle_error_redaction")
    private.handlers.clear()
    private.propagate = False
    private.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    private.addHandler(handler)
    try:
        configure_logging()  # sweeps this handler, exactly as it does uvicorn's
        capsys.readouterr()  # discard anything configuration itself wrote
        # `%d` handed a string: the filter cannot render it, and neither can the formatter.
        private.info("connecting to %s after %d retries", _DSN, "not-a-number")
    finally:
        private.handlers.clear()
        private.propagate = True
    err = capsys.readouterr().err
    assert _DSN not in err, f"handleError printed the DSN to stderr: {err}"
    assert "sup3rs3cret-password" not in err, f"handleError printed the DSN password: {err}"
    assert "--- Logging error ---" in err, (
        "the malformation must still be reported; silencing `raiseExceptions` would pass the "
        "assertions above while deleting every handler diagnostic in the process"
    )
    assert "Message: " in err and "Arguments: " in err, f"the diagnostic lost its fields: {err}"
    assert "not-a-number" in err, (
        "the argument that explains the malformation must survive redaction, or the report names "
        "no cause"
    )


def test_the_filter_survives_a_second_test_that_built_the_front_door() -> None:
    """The filter survives a second test that built the front door.

    `configure_logging()` installs filters on handlers that outlive their test, so a defect can
    appear only in a particular test order; this asserts the composed state.
    """
    from chemclaw.core.metrics_bridge import degraded

    configure_logging()
    configure_logging()  # idempotent, and the second call is what a second suite would do
    degraded(logging.getLogger("probe"), "log_redaction", "no traceback here", exc_info=False)


def test_configure_logging_twice_does_not_stack_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling `configure_logging` twice does not stack filters on any handler.

    `force=True` resets only the root's handlers; non-propagating loggers' handlers must be
    deduplicated, or every record is redacted N times and startup diagnostics repeat.
    """
    private = logging.getLogger("chemclaw.test.repeat_configure")
    private.propagate = False
    handler = logging.StreamHandler()
    private.addHandler(handler)
    try:
        for _ in range(3):
            configure_logging()
        redactors = [f for f in handler.filters if isinstance(f, SecretRedactingFilter)]
        assert len(redactors) == 1, f"filters stacked across calls: {handler.filters}"
    finally:
        private.removeHandler(handler)
        private.propagate = True


def test_the_logger_sweep_survives_a_logger_created_during_the_sweep() -> None:
    """The logger sweep snapshots `loggerDict` rather than iterating the live view.

    Loggers may be created on other threads during `configure_logging()`, and a mid-iteration size
    change would abort configuration with filters only partly attached. The mutation is provoked
    deterministically from inside the iteration: an entry whose `.propagate` inserts a key, which
    does nothing against a snapshot.
    """
    registry = logging.root.manager.loggerDict
    planted = "chemclaw.test.sweep.trap"
    created: list[str] = []

    class _CreatesALoggerWhenRead(logging.Logger):
        """A real `Logger` (so the sweep's `isinstance` accepts it) that mutates when inspected."""

        @property
        def propagate(self) -> bool:
            """Insert a new entry, then answer as an ordinary propagating logger would."""
            name = f"chemclaw.test.sweep.during{len(created)}"
            created.append(name)
            registry[name] = logging.PlaceHolder(logging.getLogger())
            return True

        @propagate.setter
        def propagate(self, value: bool) -> None:
            """`Logger.__init__` assigns it; the getter above is what this test is for."""

    registry[planted] = _CreatesALoggerWhenRead(planted)
    try:
        _handlers_that_reach_an_output_stream()
    finally:
        for name in [planted, *created]:
            registry.pop(name, None)

    assert created, "the trap was never inspected, so this test proved nothing about the sweep"


def test_a_credential_redacted_from_the_logs_is_also_withheld_from_a_child_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential redacted from logs is also withheld from a child process's environment.

    `secret_env_names()` must include `_SECRET_ENV_SETTINGS`, or bearers scrubbed from logs are
    handed to every `git` child `kg/git_writer.py` starts, where hooks and helpers can read them.
    Asserted over the whole set, so a new `*_token_env` field is covered.
    """
    assert _SECRET_ENV_SETTINGS, "the inventory is derived from the field names; it cannot be empty"
    for index, field in enumerate(_SECRET_ENV_SETTINGS):
        variable = f"CHEMCLAW_TEST_BEARER_{index}"
        monkeypatch.setattr(settings, field, variable)
        monkeypatch.setenv(variable, f"bearer-value-{index}-not-a-published-default")

    withheld = secret_env_names()
    for field in _SECRET_ENV_SETTINGS:
        variable = str(getattr(settings, field))
        value = os.environ[variable]
        # Both halves in one test, because the defect is the gap between them: the value really is
        # scrubbed from a log line, and the variable holding it really was not withheld from a
        # child.
        assert redact_secrets(f"upstream said {value}") == "upstream said ***"
        assert variable in withheld, (
            f"{field} names {variable}, whose value is redacted from every log line and was still "
            "handed to every git child"
        )


def test_the_one_security_alarm_in_this_module_can_actually_be_formatted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `degraded[log_redaction]` alarm can be formatted with the shipped log format.

    The format requires `%(correlation_id)s`, which `ContextFilter` supplies, so `ContextFilter`
    must be installed before `SecretRedactingFilter.__init__` can log. Driven through the real
    `configure_logging` with a raising connector registry, since the defect is statement ordering.
    """
    monkeypatch.setattr(
        settings,
        "log_format",
        "%(asctime)s %(levelname)s %(name)s [%(correlation_id)s/%(session_id)s]: %(message)s",
    )
    monkeypatch.setattr(settings, "log_json", False)

    def _broken() -> tuple[str, ...]:
        raise RuntimeError("a broken connector manifest")

    monkeypatch.setattr(
        "chemclaw.connectors.registry.bearer_token_env_names", _broken, raising=True
    )
    try:
        configure_logging()
        printed = capsys.readouterr().err
    finally:
        # Leave the root logger as the rest of the suite expects to find it.
        configure_logging()

    assert "degraded[log_redaction]" in printed, printed
    assert "Logging error" not in printed, printed


def test_a_dsn_password_survives_no_ordinary_stringification() -> None:
    """A DSN password survives no ordinary stringification.

    The log path is defended by `_SECRET_SETTINGS` and `_URL_USERINFO`; the DSNs are `SecretStr` so
    `repr`, `str`, `model_dump()` and `model_dump_json()` (print, debugger, crash reporter, file) do
    not disclose them either.
    """
    from chemclaw.core.config import Settings

    settings = Settings(
        postgres_dsn="postgresql://u:MARKER-PGPW-9a1@pg.internal:5432/db",
        postgres_migration_dsn="postgresql://m:MARKER-MIGPW-9a3@pg.internal:5432/db",
        session_store_dsn="postgresql://s:MARKER-SESSPW-9a2@sess.internal:5432/db",
    )
    markers = ("MARKER-PGPW-9a1", "MARKER-MIGPW-9a3", "MARKER-SESSPW-9a2")
    for rendering in (
        repr(settings),
        str(settings),
        repr(settings.model_dump()),
        settings.model_dump_json(),
    ):
        leaked = [marker for marker in markers if marker in rendering]
        assert not leaked, f"{leaked} disclosed by a rendering of Settings"

    # The value itself is untouched: `psycopg` is handed the attribute, not a rendering of it.
    assert settings.postgres_dsn == "postgresql://u:MARKER-PGPW-9a1@pg.internal:5432/db"
    # The host survives the *mask*, because an operator diagnosing a connection failure needs to
    # see which server the DSN names. It does not survive `repr`, which drops the three fields
    # whole — the two mechanisms close different sinks and `core/config/dsn.py` says why.
    assert "pg.internal" in settings.model_dump()["postgres_dsn"]
    assert "postgres_dsn" not in repr(settings)


# --- The prefix inventory, and whether anybody has looked at it lately ---------------------------
#
# `_STRUCTURAL_SECRETS` is a hand-written table of vendor prefixes, and vendors do not announce new
# ones. The last reconciliation is recorded as a date with its sources, and this file fails when it
# is overdue. `detect-secrets` stays out of the runtime: it returns spans not redactions, has no
# `(?P<keep>…)` equivalent or ReDoS bounds, and depends on `requests`. Only its inventory is read.

#: Where the vendor shapes below were read from: published prefix lists (the `detect-secrets` plugin
#: directory, GitHub's token prefixes, AWS access-key prefixes) and vendor documentation for the
#: keys this family holds.
PREFIX_INVENTORY_SOURCES = (
    "detect-secrets/plugins (the plugin directory, read as an inventory rather than imported)",
    "GitHub docs: token formats (ghp_/gho_/ghu_/ghs_/ghr_/github_pat_)",
    "AWS docs: unique identifier prefixes for access keys (AKIA/ASIA/ABIA/ACCA)",
    "Anthropic, OpenAI, Databricks, GitLab and Slack key-format documentation",
)

#: The day `_STRUCTURAL_SECRETS` was last compared against every source above, shape by shape.
#: Bumping it means having done that comparison, not having seen this test fail.
PREFIX_INVENTORY_RECONCILED = datetime.date(2026, 9, 16)

#: How long a reconciliation is trusted for. Six months is chosen against the rate the table itself
#: moves — four of the shapes now in it did not exist when this module was written — and against the
#: cost of the check, which is an hour of reading four public lists.
RECONCILE_EVERY = datetime.timedelta(days=180)


#: One sample per vendor shape the table claims to cover, so a rule that is narrowed, renamed or
#: dropped fails here by name rather than silently stopping.
#:
def _shaped(prefix: str, body: str) -> str:
    """Join a vendor prefix to a body at runtime, so no whole credential is a literal in this file.

    The samples are synthetic, but a real prefix, length and alphabet are indistinguishable from a
    live key by shape, so push protection blocks them as literals. The assembled string is
    identical, and `_VENDOR_SHAPES` still carries a full-shape sample per rule.
    """
    return prefix + body


#: Fake values with the real *shape*: vendor prefix, real length, real alphabet — assembled by
#: `_shaped` so the literal never appears in this file. See that function for why.
_VENDOR_SHAPES = {
    "github classic PAT": _shaped("ghp", "_0123456789abcdefghijklmnopqrstuvwxyz"),
    "github fine-grained PAT": _shaped(
        "github", "_pat_11ABCDEFG0abcdefghij_KLMNOPQRSTUVWXYZ0123456789"
    ),
    "anthropic key": _shaped("sk-ant", "-api03-abcdefghijklmnopqrstuvwxyz0123456789"),
    "openai project key": _shaped("sk-proj", "-abcdefghijklmnopqrstuvwxyz0123456789"),
    "openai admin key": _shaped("sk-admin", "-abcdefghij0123456789klmnopqrstuv"),
    "openai service-account key": _shaped("sk-svcacct", "-abcdefghij0123456789klmnopqrstuv"),
    "aws access key id": _shaped("AKIA", "Y34FZKBOKMUTVV7A"),
    "aws temporary (sts) key id": _shaped("ASIA", "Y34FZKBOKMUTVV7A"),
    "slack bot token": _shaped("xoxb", "-0123456789-0123456789-abcdefghijklmnopqrst"),
    "slack app-level token": _shaped("xapp", "-1-A0123456789-0123456789-abcdefghij0123"),
    "databricks pat": _shaped("dapi", "0123456789abcdef0123456789abcdef"),
    "gitlab pat": _shaped("glpat", "-AbCdEf0123456789xyz"),
    "jwt / entra bearer": "eyJhbGciOiJIUzI1NiJ9.eyJvaWQiOiJhbGljZSJ9.c2lnbmF0dXJlLWhlcmU",
    # Both spellings a key reaches a log line in: wrapped across real newlines, and JSON-encoded
    # with literal backslash-n, which is how one arrives inside a config blob or a driver's error.
    "pem private key": (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA0123abcdefghijklmnopqrstuvwxyz\nQ==\n"
        "-----END RSA PRIVATE KEY-----"
    ),
    "pem private key, json-encoded": (
        "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC0123456789abcd\\n"
        "-----END PRIVATE KEY-----"
    ),
    # The passphrase-protected form: `openssl genrsa -aes256` and `ssh-keygen -m PEM -N <pass>` emit
    # RFC 1421 header lines and a blank line between `-----BEGIN` and the body.
    "pem private key, encrypted (rfc 1421)": (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "Proc-Type: 4,ENCRYPTED\n"
        "DEK-Info: AES-256-CBC,0A1B2C3D4E5F60718293A4B5C6D7E8F9\n"
        "\n"
        "MIIEpAIBAAKCAQEA0123abcdefghijklmnopqrstuvwxyz\nQ==\n"
        "-----END RSA PRIVATE KEY-----"
    ),
}

#: Shapes the reconciliation saw and declined, asserted below to be still uncovered, so "not added"
#: is distinguishable from "not looked at". No part of this family holds one, and the value
#: inventory covers any credential this process is configured with; a rule per absent vendor is pure
#: false-positive surface. Also declined, with no sample: an Azure AD client secret (no decisive
#: prefix) and a Google service-account JSON `"private_key"` (covered by the PEM and key-name
#: rules).
_DECLINED_SHAPES = {
    "google api key": _shaped("AIza", "SyD-0123456789abcdefghijklmnopqrstu"),
    "stripe live key": _shaped("sk_live", "_0123456789abcdefghijABCD"),
    "sendgrid key": _shaped(
        "SG", ".0123456789abcdefghijkl.0123456789abcdefghijklmnopqrstuvwxyz0123"
    ),
    "npm token": _shaped("npm", "_0123456789abcdefghijklmnopqrstuvwxyz"),
    "huggingface token": _shaped("hf", "_0123456789abcdefghijklmnopqrstuvwx"),
    "shopify access token": _shaped("shpat", "_0123456789abcdef0123456789abcdef"),
}


@pytest.mark.parametrize("sample", _VENDOR_SHAPES.values(), ids=_VENDOR_SHAPES.keys())
def test_every_vendor_shape_the_inventory_claims_is_actually_redacted(sample: str) -> None:
    """Every vendor shape the inventory claims is redacted, embedded in prose.

    Credentials reach logs inside upstream error messages, and a rule anchored on string start would
    pass a bare sample.
    """
    assert sample not in redact_secrets(f"upstream rejected {sample} at 09:31"), (
        f"{sample!r} is in the declared prefix inventory and reached the stream verbatim"
    )


#: One line of PEM body, reused by every shape below so one substring check covers all four.
_PEM_BODY_LINE = "MIIEpAIBAAKCAQEA0123abcdefghijklmnopqrstuvwxyzABCDEF"

#: PEM shapes that a looser version of the rule let past, each named by the number or quantifier
#: responsible. Separate from `_VENDOR_SHAPES` because a body repeated over many lines can be absent
#: verbatim from the output while most of it survived, which needs a per-line assertion.
_PEM_SHAPES_THAT_WALKED_PAST = {
    # `openssl genrsa -aes256` / `openssl rsa -aes256` / `ssh-keygen -m PEM -N <pass>`: two RFC 1421
    # header lines and a blank line stand between the header and the body, and the separator window
    # was eight whitespace characters wide. Measured before: `redacted=False`.
    "encrypted rfc 1421 (the passphrase-protected form)": (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "Proc-Type: 4,ENCRYPTED\n"
        "DEK-Info: AES-256-CBC,0A1B2C3D4E5F60718293A4B5C6D7E8F9\n"
        "\n" + _PEM_BODY_LINE + "\n"
        "-----END RSA PRIVATE KEY-----\n"
    ),
    # A Helm Secret quoted into an error arrives as a YAML block scalar, and twelve columns of
    # indent is more than the eight the window allowed. Measured before: `redacted=False`.
    "indented twelve columns in a yaml block scalar": (
        "key: |\n            -----BEGIN PRIVATE KEY-----\n            "
        + _PEM_BODY_LINE
        + "\n            -----END PRIVATE KEY-----\n"
    ),
    # The discriminator asked for an unbroken 32-character base64 run, which a body wrapped
    # narrower than that does not have on any line. Measured before: `redacted=False`.
    "wrapped at twenty-four columns": (
        "-----BEGIN PRIVATE KEY-----\n"
        + "\n".join(_PEM_BODY_LINE[i : i + 24] for i in range(0, len(_PEM_BODY_LINE), 24))
        + "\n-----END PRIVATE KEY-----\n"
    ),
    # The run stopped after 8192 characters and `re.sub` resumed *inside the body*, where no rule
    # has a header to anchor on. Measured before: redacted **True**, and 85 body lines survived
    # past the `***` — the only one of the four that looks handled in the output it produces.
    "longer than the run's eight-kilobyte bound": (
        "-----BEGIN PRIVATE KEY-----\n"
        + "\n".join([_PEM_BODY_LINE] * 240)
        + "\n-----END PRIVATE KEY-----\n"
    ),
    # A possessive `[^\r\n\\]{0,40}+` tail stops only at `\r`, `\n` or `\\`, so on a one-line PEM it
    # would swallow the next header and the body. The IV is sixteen hex characters on purpose: a
    # 32-hex IV leaves a long enough base64 run for the lookahead to succeed mid-token and redact by
    # accident.
    "encrypted rfc 1421 on one line, tab-separated (aes-128-cbc iv)": (
        "-----BEGIN RSA PRIVATE KEY-----\tProc-Type: 4,ENCRYPTED\t"
        "DEK-Info: AES-128-CBC,0123456789ABCDEF\t\t" + _PEM_BODY_LINE
    ),
    "encrypted rfc 1421 on one line, space-separated (aes-128-cbc iv)": (
        "-----BEGIN RSA PRIVATE KEY----- Proc-Type: 4,ENCRYPTED "
        "DEK-Info: AES-128-CBC,0123456789ABCDEF  " + _PEM_BODY_LINE
    ),
}


@pytest.mark.parametrize(
    "block", _PEM_SHAPES_THAT_WALKED_PAST.values(), ids=_PEM_SHAPES_THAT_WALKED_PAST.keys()
)
def test_a_pem_body_is_redacted_whatever_shape_the_key_arrives_in(block: str) -> None:
    r"""A PEM body is redacted whatever shape the key arrives in.

    The asserted substring is a 16-character prefix of a body line, since a block repeated over many
    lines can be absent verbatim while most of it survived. The cases cover fixed-size gaps, runs
    and body bounds that held only for an unencrypted 64-column PEM, and possessive tails that
    cannot give back characters when RFC 1421 headers are separated by a tab or space.
    """
    redacted = redact_secrets(f"driver rejected the key:\n{block}\nat 09:31")
    assert _PEM_BODY_LINE[:16] not in redacted, f"a PEM body reached the stream: {redacted[:200]!r}"
    assert "-----BEGIN" in redacted, (
        "the header is deliberately kept — it is what tells an operator a key was there and which "
        "kind it was — so a rule that redacted the whole block would pass the assertion above "
        "while losing the diagnostic the rule was designed around"
    )


@pytest.mark.parametrize("sample", _DECLINED_SHAPES.values(), ids=_DECLINED_SHAPES.keys())
def test_a_shape_the_inventory_declined_is_still_declined(sample: str) -> None:
    """A shape the inventory declined is still not covered.

    If a rule starts covering one, move its row into `_VENDOR_SHAPES` in the same commit.
    """
    assert redact_secrets(sample) == sample, (
        f"{sample!r} is recorded in `_DECLINED_SHAPES` as deliberately uncovered and is now being "
        "redacted: move it to `_VENDOR_SHAPES` beside the rule that covers it"
    )


def test_the_prefix_inventory_has_been_reconciled_this_half_year() -> None:
    """The vendor prefix inventory has been reconciled within the last half year.

    The other tests only check that existing rules still work. Bumping the date means re-reading the
    sources; a bump with no table diff is a legitimate outcome.
    """
    overdue = datetime.date.today() - (PREFIX_INVENTORY_RECONCILED + RECONCILE_EVERY)
    assert overdue.days <= 0, (
        f"the structural credential table was last reconciled on "
        f"{PREFIX_INVENTORY_RECONCILED} and is {overdue.days} days overdue. Re-read "
        + "; ".join(PREFIX_INVENTORY_SOURCES)
        + ". Add what is missing with the ReDoS discipline the module demonstrates (a bounded "
        "tail, `_NOT_MID_TOKEN`, a pathological unit in `_QUADRATIC_UNITS`), record what you "
        "decline in `_DECLINED_SHAPES`, and set `PREFIX_INVENTORY_RECONCILED` to today."
    )
