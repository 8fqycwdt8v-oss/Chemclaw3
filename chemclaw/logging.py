"""Application-wide logging setup — one config-driven switch.

Why this exists: before this, the app emitted essentially no logs, so troubleshooting a
stuck worker or a silent ELN sync meant reading raw tracebacks with no context. This gives a
single idempotent `configure_logging()` — called at each worker's entrypoint — that wires the
stdlib root logger to the configured level and format (`CHEMCLAW_LOG_LEVEL`,
`CHEMCLAW_LOG_FORMAT`), so verbosity is an ENV switch, not a code change. Application modules
just do `logging.getLogger(__name__)` and log; they never configure logging themselves.
"""

import logging

from chemclaw.config import settings


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
    from agent_framework.observability import configure_otel_providers

    try:
        configure_otel_providers(enable_sensitive_data=settings.otel_include_sensitive_data)
    except ImportError as exc:  # SDK/exporter extras not installed
        raise RuntimeError(
            "CHEMCLAW_OTEL_ENABLED=true but the OpenTelemetry SDK/OTLP exporter is not installed"
        ) from exc
