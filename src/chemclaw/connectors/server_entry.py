"""Run one bundle's MCP server as a process, with the process setup every other role gets.

Calls `configure_logging()` and `configure_telemetry()` before serving, which gives connector
servers secret redaction (they hold bearer tokens), correlation id and actor on every log line,
and the no-op meter provider that prevents the OpenTelemetry proxy leak.

The setup is here, not in `connector_app`, because `configure_logging()` removes every root
handler and `connector_app` runs at import time in modules tests and the dev composite import
freely. A process-wide side effect belongs at the process boundary.
"""

import logging

import uvicorn

from chemclaw.core.asgi import transport_bounds
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging, configure_telemetry

logger = logging.getLogger(__name__)


def main(connector: str) -> None:
    """Configure this process, then serve `connector`'s app.

    The app is passed to uvicorn as an import string so it is built after logging is configured;
    otherwise import-time log lines would go to an unredacted root logger.
    """
    configure_logging()
    configure_telemetry()
    logger.info("connector server starting: %s", connector)
    uvicorn.run(
        f"chemclaw.connectors.{connector}.server.app:app",
        host=settings.service_host,
        port=settings.service_port,
        # Ours is already applied above; letting uvicorn install its own would replace it — the
        # same reason `core/worker_http.py` passes `log_config=None`.
        log_config=None,
        # This is every `connector-*` pod, and it ran unbounded. `BodySizeLimit` already covers the
        # body half here over the connector's own smaller ceiling; these are the transport half.
        **transport_bounds(),
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m chemclaw.connectors.server_entry <connector-name>")
    main(sys.argv[1])
