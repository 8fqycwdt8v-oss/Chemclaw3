"""The logging switch applies the configured level (admin-troubleshooting, P0).

Proves `configure_logging` is genuinely config-driven — an admin raising `CHEMCLAW_LOG_LEVEL`
changes the root logger's threshold — and is case-insensitive, without asserting on any
specific handler wiring (which `logging.basicConfig` owns).
"""

import logging

import pytest

from chemclaw.config import settings
from chemclaw.logging import configure_logging, configure_telemetry


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


def test_configure_telemetry_works_with_the_shipped_helm_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    """
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_endpoint", "http://otel-collector.observability.svc:4317")
    configure_telemetry()  # constructing the exporter dials nothing; a missing SDK raises here
