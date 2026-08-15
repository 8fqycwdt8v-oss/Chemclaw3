"""The logging switch applies the configured level (admin-troubleshooting, P0).

Proves `configure_logging` is genuinely config-driven — an admin raising `CHEMCLAW_LOG_LEVEL`
changes the root logger's threshold — and is case-insensitive, without asserting on any
specific handler wiring (which `logging.basicConfig` owns).
"""

import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable

import pytest

from chemclaw.core.config import Settings, settings
from chemclaw.core.logging import (
    _SECRET_SETTINGS,
    ContextFilter,
    JsonFormatter,
    SecretRedactingFilter,
    _handlers_that_reach_an_output_stream,
    configure_logging,
    configure_telemetry,
    redact_secrets,
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
    """Off must mean a no-op provider, not the *absence* of one — the front door's memory leak.

    With no meter provider set, the OpenTelemetry API does not discard instrument calls: it
    **proxies** them and keeps every proxy forever, so it can back them if a provider arrives
    later (`_ProxyMeterProvider._meters` and `_ProxyMeter._instruments` are module-level lists
    that only ever grow). MAF creates one duration histogram per exposed MCP function and this
    system rebuilds its connector tool surface every turn, so a turn with telemetry off leaked
    35 `_ProxyMeter`s, 35 `_ProxyHistogram`s, 70 locks and 35 lists — permanently.

    Measured end to end with `chemclaw.cli.leak_probe` against the real front door: **+178 live
    objects and +20.7 KB of RSS per turn before, +3.3 objects and +2.7 KB after** — and what
    remains is the session LRU filling toward its cap, which is bounded by construction.

    The test this replaces asserted that disabled telemetry "does nothing", which was true and
    was the defect.

    **In a subprocess, deliberately**, and for the same reason as the test below it: a meter
    provider is global and can be set exactly once, so proving this in-process would decide the
    question for every test that ran afterwards.
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


def test_configure_telemetry_works_with_the_shipped_helm_value() -> None:
    """OTel must actually start under the value the chart ships, not merely validate.

    `deploy/helm/chemclaw/values.yaml` sets `CHEMCLAW_OTEL_ENABLED: "true"`, and
    `configure_telemetry` is called unconditionally at process start by the front door
    (`api/app.py::_lifespan`), the background worker and every connector worker. The OTel
    SDK and OTLP exporter were not declared dependencies, so that call raised and *every* Python
    component CrashLoopBackOff'd on first deploy.

    The existing chart test only constructed `Settings(**helm_values)` — which succeeds, because
    the value is a perfectly valid bool. That is the gap this closes: a production value has to be
    *executed*, not type-checked. Any regression that drops the SDK from the dependency closure
    fails here instead of in the cluster.

    **In a subprocess, deliberately.** `configure_telemetry` installs a *global* tracer provider
    and starts a background export loop. Run in-process, this test would leave every later test in
    the session exporting spans to a collector that is not there — which it did, filling the run
    with `Failed to export traces` errors. The thing under test is a process startup path, so a
    process is the honest place to test it.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from chemclaw.core.logging import configure_telemetry; configure_telemetry()",
        ],
        env={
            **os.environ,
            "CHEMCLAW_OTEL_ENABLED": "true",
            "CHEMCLAW_OTEL_ENDPOINT": "http://otel-collector.observability.svc:4317",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"startup failed under the shipped OTel config:\n{result.stderr}"


# --- the span pipeline this module now builds itself ------------------------------------------
#
# `configure_telemetry` used to be one line into the agent framework
# (`agent_framework.observability.configure_otel_providers`), and removing that framework would
# have stopped tracing for the whole process **without failing a single test**: every helper in
# `core/tracing.py` degrades to a no-op when no provider is installed, which is right for a turn and
# is exactly what makes the silence undetectable. The three tests below are what makes it audible: a
# span reaches the exporter carrying the service that produced it, a second call does not build a
# second pipeline, and the missing-extras path still names the dependency. ("Off stays off" is the
# pair above, which predates this and is unchanged.)


def test_a_span_reaches_the_exporter_carrying_the_service_that_produced_it() -> None:
    """The bootstrap has to *work*, not merely import: span in, span out, named by service.

    Driven against a real in-memory exporter rather than a mock, because the failure this replaces
    is "nothing is exported at all" — and a mock asserting that `BatchSpanProcessor` was constructed
    would pass against a pipeline whose spans go nowhere. `service.name` is asserted because it is
    what a collector groups by: a provider that exports spans attributed to `unknown_service` is a
    trace nobody can find, which is the shape the previous bootstrap actually shipped (it named
    every Chemclaw process `agent_framework`).

    In-process is safe here precisely because `_build_tracer_provider` is the half that installs
    nothing globally.
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
    """Called twice it must be a no-op — the CLI, the workers and the tests all call it.

    The observable cost of getting this wrong is not a warning: `trace.set_tracer_provider` refuses
    the second provider and logs, but the *building* of it has already started a second
    `BatchSpanProcessor` export thread and opened a second gRPC channel, both of which the API then
    discards and neither of which anything closes. So the assertion is on threads — a real,
    countable consequence — rather than on whether a flag was read.

    The same subprocess also pins the two things a first-party bootstrap has to get right and that
    nothing else would notice: the installed provider is the real SDK one (not the API's no-op
    default, which is what "tracing silently stopped" looks like), and `CHEMCLAW_OTEL_ENDPOINT` is
    bridged to the standard `OTEL_EXPORTER_OTLP_ENDPOINT` the OTLP exporter resolves itself.
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
            **os.environ,
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
    """An admin who flips the flag on an install without the extras gets a directive error.

    The OTLP exporter is blocked by putting `None` in `sys.modules` — the import system's own way
    of making a module unimportable — rather than by patching a fake over it, so the code under
    test takes the identical path it would on a machine where the distribution is absent.
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
# Two findings in one readiness row, and they are not the same problem.
#
# *Nothing joined.* One `%`-format string, no JSON option, and no filter injecting the
# correlation/actor/session ContextVars — which already existed and were already read by audit,
# authorization and the connector headers. So an ordinary WARNING sat beside the audit trail and
# the traces and could be tied to neither.
#
# *Nothing redacted.* `core/db.py::_redact` strips a password from a DSN before it is echoed, in
# exactly one place — the tell that the concern is real and unsystematised.
#
# What is deliberately *not* redacted is the audit trail's arguments. `SECURITY.md` states that the
# trail records tool-call arguments, that they are user free text and may hold PII, and that this is
# **intentional**: the trail exists to be an attributable "who did what to which inputs" record.
# Redacting it would break the very thing it is for.

_DSN = "postgresql://chemclaw:sup3rs3cret-password@db.internal:5432/chemclaw"
_KEY = "sk-live-0123456789abcdef"


@pytest.fixture
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure real secret values, as a deployment would hold them."""
    monkeypatch.setattr("chemclaw.core.config.settings.postgres_dsn", _DSN)
    monkeypatch.setattr("chemclaw.core.config.settings.llm_api_key", _KEY)


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
    """`logger.info("dsn=%s", dsn)` keeps the secret in `record.args` until formatting.

    A filter inspecting only `record.msg` would pass this untouched and a formatter would then
    render the credential — precisely how one escapes a naive redactor. The filter runs on the
    *rendered* message and clears `args` so nothing can re-render the original.
    """
    record = _record("connecting: %s", _DSN)
    SecretRedactingFilter().filter(record)
    assert record.args is None
    assert _DSN not in logging.Formatter("%(message)s").format(record)


def _emitted(record: logging.LogRecord) -> str:
    """Everything a handler would write for `record`: message, traceback and stack alike.

    `_rendered` above returns only `getMessage()`, which is exactly the blind spot this group of
    tests exists for — a credential can be in the message, in the exception, or in a stack dump,
    and only the first was ever redacted.
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
    """The finding: the filter rewrote the message and never touched the traceback.

    `logger.exception(...)` / `exc_info=True` renders the exception at *format* time, so every
    credential in the inventory was readable in the log lines a failure produces — which is the
    worst case, because a failure is exactly when a DSN or an auth header ends up in the error
    text. Measured leaking both an API key and a DSN password verbatim before the fix.
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
    """`scheme://token@host` — how a PAT reaches a git remote — was passing through verbatim.

    Only `scheme://user:password@host` was matched, so the *more common* credential form for a
    token was the one that escaped. The host is kept, because a redacted line still has to say
    which remote failed.
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
    """A value committed to this repository is not a secret, and redacting it only corrupts logs.

    The dev Postgres default is `postgresql://chemclaw:chemclaw@localhost:5432/chemclaw`, whose
    password is the literal string `chemclaw` — long enough to pass the length floor. Treating it
    as a credential replaced the product's own name with `***` in every dev and CI log line that
    mentioned it, including lines with nothing to do with the database.
    """
    from chemclaw.core.config import settings as live

    default = type(live).model_fields["postgres_dsn"].default
    assert "chemclaw:chemclaw" in default  # the collision this guards is real, not hypothetical
    assert _rendered(_record("the chemclaw service started")) == "the chemclaw service started"


def test_a_published_password_stays_published_when_the_dsn_is_repointed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derived half: a *modified* DSN is a secret, but the shipped password inside it is not.

    This is the case a first attempt missed and only CI could see. `tests/conftest.py` repoints
    `postgres_dsn` at an isolated schema, so under CI the DSN is not the shipped default and is
    redacted correctly — while the password inside it is still the literal `chemclaw`, which then
    replaced the product's own name with `***` in unrelated lines. Locally there is no Postgres,
    nothing repoints, and comparing whole values passed.

    So both must hold at once: the repointed DSN is hidden, and the published password is not.
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
    monkeypatch.setattr("chemclaw.core.config.settings.llm_api_key", "")
    monkeypatch.setattr("chemclaw.core.config.settings.note_webhook_secret", "abc")
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
    """A filter may not import on the logging path, and this is why.

    A filter runs at arbitrary moments — including from inside another module's import, and from
    inside Temporal's workflow sandbox, which hooks `__import__` and logs a warning whenever
    sandboxed code touches something restricted. With the import inside `ContextFilter.filter`,
    that warning re-entered the filter, which imported again into a now half-initialised module,
    which tripped another restriction: the workflow worker wedged until the suite's global timeout
    fired. Watching `__import__` for the duration of one `filter` call is the smallest faithful
    reproduction, and it holds for both filters rather than only the one that had the defect.

    The hook *records* rather than raises, and the assertions run after it is uninstalled: raising
    inside `__import__` takes pytest's own reporting machinery down with it, so the failure arrives
    as an INTERNALERROR naming a pytest module instead of naming this test.
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
    """On the *handler*, not a logger — the distinction is load-bearing.

    Every module logs through `getLogger(__name__)`, so almost every record reaches the root handler
    by propagation, and a filter attached to a logger is not consulted for propagated records.
    Installed on the logger, redaction would silently apply to almost nothing.
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
    """The git push credential is in every pod's env (`_helpers.tpl`) but names no `Settings` field.

    `_SECRET_SETTINGS` is read through `getattr(settings, name, ...)`, so nothing there can ever
    see a credential that is not config — this one is consumed only by
    `deploy/knowledge-sync.sh`. A bare `repr(os.environ)` in a traceback would log it in the clear
    (Sec-6) unless the filter reads the environment variable directly, which is what this proves.
    """
    token = "ghp_knowledge-repo-push-credential-0123456789"
    monkeypatch.setenv("CHEMCLAW_KNOWLEDGE_REPO_TOKEN", token)
    assert token not in _rendered(_record("git push failed: %s", token))


def test_a_connector_bearer_token_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-connector bearer token is resolvable but was never enumerated (Sec-6).

    `_EnvBearerAuth` reads `os.environ[token_env]` per request; the variable *name* is
    manifest-declared, so the filter can enumerate every enabled connector's `token_env` and
    redact whatever value sits behind it, the same way it redacts a `Settings`-held secret.
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


def test_every_named_secret_is_a_real_settings_field() -> None:
    """The credential inventory names fields that exist, so a rename cannot silently disarm it.

    `_SECRET_SETTINGS` is read through `getattr(settings, name, "")`, which turns a name that no
    longer resolves into an empty string and then skips it as too short to redact. That is silent
    by construction: the inventory keeps listing the credential, the filter keeps running, and
    nothing redacts anything. `knowledge_repo_token` sat here in exactly that state — it is not a
    `Settings` field and never was one under that name.

    No credential was actually exposed by it, because the entry was dead rather than wrong. The
    hazard is the next rename: three of the listed values (`hpc_api_token`,
    `hpc_artifact_store_token`, `temporal_api_key`) are touched by no other test, so renaming one
    in `config.py` would leave a real secret reaching the log with the suite still green.

    Deliberately one-directional. It does not assert that every secret-looking field is listed,
    because "secret-looking" is exactly the name-pattern heuristic the inventory's own comment
    rejects — `entra_token_endpoint` is a URL, `budget_max_tokens_per_user` is an integer, and
    `temporal_tls_key` is a path to a PEM rather than the key material. Whether a new field holds a
    credential stays a human judgement made in review; whether a listed one still exists does not.
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
    """The finding: the redaction was real and the JSON formatter discarded it.

    `SecretRedactingFilter` renders and scrubs the traceback into `record.exc_text` precisely so a
    formatter cannot switch redaction off — and `JsonFormatter.format` then called
    `formatException(record.exc_info)`, reaching past the scrubbed copy into the original exception
    and emitting the credential in full.

    The leak existed **only in production**: the chart sets `CHEMCLAW_LOG_JSON=true`, while every
    redaction test above ran the plain formatter. That is why this one is parametrized over both
    rather than added as a third JSON case — the assertion set is identical and the formatter is
    the only axis, so a future formatter cannot be introduced with its own private blind spot.
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
    """Defence in depth: a handler someone else configured still must not emit the key.

    The filter is the mechanism and this is the backstop. It cannot see the per-connector bearer
    tokens (only the filter resolves those), so it is not a replacement — but it must never be the
    reason a credential is written.
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
    """The front door's uvicorn loggers were outside the redaction boundary entirely.

    `deploy/entrypoint.sh` starts the API as `exec uvicorn ... --factory` with no `--log-config`, so
    uvicorn installs its own dictConfig first and gives `uvicorn` a handler with `propagate: false`.
    `uvicorn.error` logs every unhandled ASGI exception with `exc_info` — the records most likely to
    carry a DSN or an auth header — and they reached a stream this module had never touched.

    Asserting on the filters rather than on captured output because that is the property that
    generalises: any logger that opts out of propagation must still be swept.
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
    """The value inventory can only cover what this process configured.

    Everything that merely passes through — the caller's own bearer token, a PAT quoted in an
    upstream error — was outside it, and eleven realistic shapes were measured reaching the stream
    verbatim. These rules are anchored on a vendor-assigned prefix and a long opaque tail, so they
    are structural without being a guess.
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
        "Bearer token was rejected by the identity provider",
        "the access_token field was absent from the response body",
        "no api_key configured for this provider",
    ],
)
def test_the_structural_rules_never_touch_ordinary_content(innocent: str) -> None:
    r"""The reason pattern matching was rejected once, held to — and the way it first failed.

    A false positive corrupts a log line, and a rule that ate a SMILES, an InChIKey, a note slug or
    an ADR id would be worse than the leak it closed. The first version of these rules did something
    worse still: an over-broad value class after a key name ate this repository's own source lines,
    which are the one text guaranteed to appear in a traceback. An adversarial review measured 41
    changed lines across the tree, four of them executable source.

    That version passed the earlier form of this test, which carried only the six identifiers above.
    The source lines and the prose are here because their absence is what made it pass.
    """
    assert redact_secrets(innocent) == innocent


def test_the_structural_rules_still_catch_the_real_shapes_after_narrowing() -> None:
    """Narrowing must not have removed the floor it was added for.

    The digit requirement and the opaque character class were added to stop the rules eating source
    lines. This is the other half: the eleven measured pass-through shapes must still be redacted,
    including the two spellings the first version missed entirely (`PGPASSWORD=` and a `repr`'d
    config dict), which the narrowing pass folded in.
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


@pytest.mark.parametrize(
    "unit",
    ["-eyJ", "password=", "PGPASSWORD=", "api_key=", "access_token=", "client_secret=", "Bearer "],
    ids=["jwt", "password", "pgpassword", "api-key", "access-token", "client-secret", "bearer"],
)
def test_redaction_cannot_be_made_quadratic_by_a_log_line(unit: str) -> None:
    r"""Every pattern's cost is linear in the line, because this runs holding the logging lock.

    **Every pattern is passed, because two of them were quadratic while one was not.** Measured,
    `password=` ran at 63.9x and `api_key=` at 56.2x for an 8x input — while `-eyJ`, the pattern
    the first fix targeted, was already linear. The cause was shared:
    `_HAS_DIGIT` was written `_OPAQUE*\d` and reintroduced, in a lookahead, exactly the unbounded
    tail `_NOT_MID_TOKEN` had been added to remove. Measured then: 18 KB -> 0.5 s, 36 KB -> 2.0 s,
    72 KB -> 8.1 s.

    The reach is what makes it serious. `uvicorn.access` is a
    non-propagating logger, so `_handlers_that_reach_an_output_stream` attaches this filter to it —
    and it is the one logger that writes the raw request URL. A 115 KB request line stalled the pod
    for 21 s, unauthenticated, on a 404, before any ASGI middleware ran.

    Parametrized so a new pattern is covered by construction rather than by whoever remembers. The
    bound is generous rather than tight: the claim is that the cost is not quadratic, not that it is
    fast, so this stays honest on a loaded box.
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
    """`exc_info=False` is a bool on the record, and the filter used to subscript it.

    Found by a cross-lane review, and invisible to every lane that produced it. The logging lane
    moved traceback rendering into the filter (so a credential in a traceback is redacted before a
    deployment's own formatter sees it) guarded by `record.exc_info is not None`. The enforcement
    lane then added `degraded(..., exc_info: bool = True)`, which forwards straight to
    `logger.log`. `Logger._log` stores what it is handed, so `exc_info=False` is a *bool* on the
    record: `False is not None` is true, and `formatException(False)` raises
    `TypeError: 'bool' object is not subscriptable`.

    Filters run inside `Handler.handle`, outside logging's own error handling, so it propagated to
    the caller. The one production site passing it is `skill_manifest`, inside the `except` whose
    entire job is to skip a malformed `SKILL.md` and carry on — so one bad manifest made
    `build_agent` raise instead. `logging`'s own `Formatter.format` tests truthiness here; the fix
    is to match it.
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
    """A record the filter cannot process must behave exactly as it does with no filter installed.

    The sibling of `exc_info=False` one test up, and the same mechanism: `Handler.handle` calls
    `self.filter(record)` *outside* the try/except that wraps `emit()`, so anything the filter
    raises lands in whoever called `logger.info(...)`. Without the filter every one of these five
    malformations is logging's own to report — `handleError` writes `--- Logging error ---` to
    stderr and the caller returns normally. With the filter installed each one raised instead.

    That matters most where it is least visible: `metrics_bridge.degraded()` builds
    `"degraded[%s]: " + message` and forwards `*args`, so a mismatched `degraded()` call inside an
    `except` block replaced the degradation being reported with a `TypeError` — precisely what
    `metrics_bridge` exists to prevent.

    Asserts the whole `Handler.handle` path, not `filter()` in isolation, because the defect is
    about *where* the exception surfaces rather than about the filter's return value.
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


def test_the_filter_survives_a_second_test_that_built_the_front_door() -> None:
    """The order dependence that hid the bug above from every per-lane test run.

    `configure_logging()` installs this filter on handlers that outlive the test that built them,
    so `pytest tests/test_degraded.py` alone passed while
    `pytest tests/test_auth.py tests/test_degraded.py` failed two. A defect reachable only in a
    particular order is one that every lane's own green run is structurally unable to see, which is
    why this asserts the composed state rather than the isolated one.
    """
    from chemclaw.core.metrics_bridge import degraded

    configure_logging()
    configure_logging()  # idempotent, and the second call is what a second suite would do
    degraded(logging.getLogger("probe"), "log_redaction", "no traceback here", exc_info=False)


def test_configure_logging_twice_does_not_stack_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """`configure_logging` is documented as safe to call more than once — for every handler.

    `force=True` resets the *root's* handlers, so the root was always fine. A non-propagating
    logger's handlers are not ours to reset, and the sweep added a fresh pair to each on every call:
    measured 2 -> 4 -> 6 filters over three calls. Every record on the front door's hot path would
    then be redacted N times, and `SecretRedactingFilter.__init__` walks the connector registry off
    disk, so the startup-only ERROR and counter fired once per handler per call instead.
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


def test_the_logger_sweep_survives_concurrent_getlogger() -> None:
    """Snapshotting `loggerDict` under the logging lock, not iterating the live view.

    `configure_logging()` runs in the app factory while worker startup, a lazy connector import or
    OTel's first use may be creating loggers on another thread. Iterating the live mapping raised
    `RuntimeError: dictionary changed size during iteration` in 64 of 4000 measured attempts — and
    the raise aborts configuration with filters attached to only some handlers, which is the worst
    of the three outcomes.
    """
    import threading

    stop = threading.Event()
    failures: list[BaseException] = []

    def churn(index: int) -> None:
        counter = 0
        while not stop.is_set():
            logging.getLogger(f"chemclaw.test.churn{index}.mod{counter}")
            counter += 1

    workers = [threading.Thread(target=churn, args=(i,), daemon=True) for i in range(4)]
    for worker in workers:
        worker.start()
    try:
        for _ in range(2_000):
            try:
                _handlers_that_reach_an_output_stream()
            except RuntimeError as exc:  # pragma: no cover - the defect this pins
                failures.append(exc)
                break
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=5)
    assert not failures, f"the sweep raced against getLogger(): {failures[0]}"
