"""Run one bundle's MCP server as a process — the entrypoint connector servers did not have.

Every other process role has a module that owns its startup: the front door has `api/app.py`'s
`create_app`, each Temporal worker has `connectors/worker.py` or `durable/background_worker.py`,
and every CLI has its own `main`. Each of them calls `configure_logging()` and
`configure_telemetry()` there, because those are *process* setup and belong at a process boundary.

A connector server had no such module. `deploy/entrypoint.sh` execed `uvicorn
chemclaw.connectors.<name>.server.app:app` straight at the app object, so the bundle's `app.py`
was the entrypoint by accident — and nothing in it did the setup. The consequences were real and
one-sided:

- **No secret redaction.** This is the one process family that holds per-connector bearer tokens,
  and the one whose whole job is talking to things over HTTP with a credential
  (`D-2026-08-06-a-redactor-that-only-reads-the-message`).
- **No correlation id or actor** on any line, so a connector's logs could not be joined to the
  turn that caused them — the thing `ContextFilter` exists to guarantee.
- **No no-op meter provider.** "Telemetry off" is not the same as no provider: with none set, the
  OpenTelemetry API proxies every instrument call and retains the proxy forever. That is the front
  door's measured memory leak (`_install_noop_meter_provider`), and connector servers were running
  in exactly the configuration that leaks.

The setup cannot go in `connector_app` instead, and the reason is worth recording because it was
tried first: `configure_logging()` is `logging.basicConfig(force=True)`, which *removes every
existing root handler*. `connector_app` is called at import time by seven bundle modules that
tests, the dev composite and anything else import freely — so putting it there tore out pytest's
capture handler and broke two audit-trail tests that had nothing to do with logging. A
process-wide side effect belongs at the process boundary, not in a composition helper.
"""

import logging

import uvicorn

from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging, configure_telemetry

logger = logging.getLogger(__name__)


def main(connector: str) -> None:
    """Configure this process, then serve `connector`'s app.

    The import target is passed to uvicorn as a string rather than imported here, so the app is
    built *after* logging is configured — otherwise a bundle's import-time logging would go to an
    unconfigured, unredacted root logger, which is most of what this module exists to prevent.
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
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m chemclaw.connectors.server_entry <connector-name>")
    main(sys.argv[1])
