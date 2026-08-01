"""The logging switch applies the configured level (admin-troubleshooting, P0).

Proves `configure_logging` is genuinely config-driven — an admin raising `CHEMCLAW_LOG_LEVEL`
changes the root logger's threshold — and is case-insensitive, without asserting on any
specific handler wiring (which `logging.basicConfig` owns).
"""

import logging
import os
import subprocess
import sys

import pytest

from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging, configure_telemetry


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


def test_configure_telemetry_is_a_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With OTel off (the default), telemetry setup does nothing and never raises."""
    monkeypatch.setattr(settings, "otel_enabled", False)
    configure_telemetry()  # must return cleanly without importing/wiring any exporter


def test_configure_telemetry_works_with_the_shipped_helm_value() -> None:
    """OTel must actually start under the value the chart ships, not merely validate.

    `deploy/helm/chemclaw/values.yaml` sets `CHEMCLAW_OTEL_ENABLED: "true"`, and
    `configure_telemetry` is called unconditionally at process start by the front door
    (`service/app.py::_lifespan`), the background worker and every connector worker. The OTel
    SDK and OTLP exporter were not declared dependencies, so that call raised and *every* Python
    component CrashLoopBackOff'd on first deploy.

    The existing chart test only constructed `Settings(**helm_values)` — which succeeds, because
    the value is a perfectly valid bool. That is the gap this closes: a production value has to be
    *executed*, not type-checked. Any regression that drops the SDK from the dependency closure
    fails here instead of in the cluster.

    **In a subprocess, deliberately.** `configure_otel_providers` installs *global* tracer and
    meter providers and starts a background export loop. Run in-process, this test would leave
    every later test in the session exporting spans to a collector that is not there — which it
    did, filling the run with `Failed to export traces` errors. The thing under test is a process
    startup path, so a process is the honest place to test it.
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
# **intentional**: GxP requires an attributable "who did what to which inputs" record. Redacting it
# would break the requirement the trail exists to meet.

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
    from chemclaw.core.logging import SecretRedactingFilter

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
    from chemclaw.core.logging import SecretRedactingFilter

    record = _record("connecting: %s", _DSN)
    SecretRedactingFilter().filter(record)
    assert record.args is None
    assert _DSN not in logging.Formatter("%(message)s").format(record)


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
    from chemclaw.agent.identity_context import (
        reset_current_correlation_id,
        set_current_correlation_id,
    )
    from chemclaw.core.logging import ContextFilter

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
    from chemclaw.core.logging import ContextFilter

    record = _record("worker starting")
    assert ContextFilter().filter(record) is True
    assert record.correlation_id == "-"  # type: ignore[attr-defined]


def test_json_output_is_one_parseable_object_per_line() -> None:
    """A log stack should parse, not regex a `%`-format string."""
    import json

    from chemclaw.core.logging import ContextFilter, JsonFormatter

    record = _record("ELN sync found %d entries", 3)
    ContextFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "ELN sync found 3 entries"
    assert payload["level"] == "INFO"
    assert set(payload) >= {"time", "logger", "correlation_id", "actor", "session_id"}


def test_a_traceback_stays_inside_the_json_object() -> None:
    """A multi-line traceback trailing a line-delimited record is forty broken entries."""
    import json

    from chemclaw.core.logging import JsonFormatter

    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "chemclaw.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    line = JsonFormatter().format(record)
    assert "ValueError: boom" in json.loads(line)["exception"]
    assert "\n" not in line.strip()


def test_configure_logging_installs_both_filters_on_the_handler() -> None:
    """On the *handler*, not a logger — the distinction is load-bearing.

    Every module logs through `getLogger(__name__)`, so almost every record reaches the root handler
    by propagation, and a filter attached to a logger is not consulted for propagated records.
    Installed on the logger, redaction would silently apply to almost nothing.
    """
    from chemclaw.core.logging import ContextFilter, SecretRedactingFilter

    configure_logging()
    handlers = logging.getLogger().handlers
    assert handlers, "configure_logging left the root logger with no handler"
    for handler in handlers:
        kinds = {type(f) for f in handler.filters}
        assert ContextFilter in kinds and SecretRedactingFilter in kinds
