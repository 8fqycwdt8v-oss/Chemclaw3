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
