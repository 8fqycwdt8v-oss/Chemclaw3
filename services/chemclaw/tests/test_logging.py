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
