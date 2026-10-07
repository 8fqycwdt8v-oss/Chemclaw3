"""One place to open a Temporal client, configured consistently.

Workers and the agent's job tools share one client with the configured address and namespace and the
pydantic data converter. Identity rides inside workflow payloads (`requested_by`), never the
transport, so the connection is authenticated only by mTLS or a Temporal Cloud API key. Connect
options come from a pure helper so they can be tested without a broker.

One client per process: a `Client` is long-lived and multiplexes calls, while a client per call
would mean an mTLS handshake and blocking PEM reads on the event loop each time.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from temporalio.client import Client, TLSConfig
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

from chemclaw.core.aio import LoopLocalLock
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

# The process's client, built on first use; a module singleton like the metrics registry.
_CLIENT: Client | None = None
# Serialises the first connect so a burst of concurrent calls opens one channel, not N. One lock per
# event loop (`core/aio.py`): an `asyncio.Lock` shared across loops hangs a second loop's waiter.
# Two loops opening two channels is a cost, not a fault, so no `threading.Lock` is needed;
# `_RUNTIME` below is reached from a thread and tolerates a second build.
_CONNECT_LOCK = LoopLocalLock("core.temporal_client's connect lock")
# The SDK's own telemetry runtime, built at most once per process: a `Runtime` owns a Rust core and
# a bound socket.
_RUNTIME: Runtime | None = None


def telemetry_runtime() -> Runtime | None:
    """The Temporal SDK runtime exporting its own metrics, or `None` when that is switched off.

    Worker facts no first-party counter can derive (poller count, slot saturation, sticky cache,
    schedule-to-start latency, task failures) come only from the SDK. Exposed on its own port, since
    its names and labels are the SDK's and `core/metrics.py` refuses undeclared labels.

    `None` when `temporal_metrics_port` is 0 (the default), and `None` when the exporter cannot
    start (for example the port is taken): SDK metrics are optional and the worker is not, and the
    failure would otherwise be reported as a Temporal outage. It is counted as
    `chemclaw_degraded_total{subsystem="temporal_sdk_metrics"}`.
    """
    global _RUNTIME
    if not settings.temporal_metrics_port:
        return None
    if _RUNTIME is None:
        bind_address = f"{settings.temporal_metrics_host}:{settings.temporal_metrics_port}"
        try:
            _RUNTIME = Runtime(
                telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address=bind_address))
            )
        except Exception:
            degraded(
                logger,
                "temporal_sdk_metrics",
                "could not export Temporal SDK metrics on %s; the client runs without them",
                bind_address,
            )
            return None
    return _RUNTIME


def _tls_config() -> TLSConfig | None:
    """Build an mTLS config from the configured PEM paths, or `None` when none are set.

    The client cert and key authenticate this component; the server-root CA pins the frontend. Any
    subset may be set, so each path is read independently.
    """
    cert = settings.temporal_tls_cert
    key = settings.temporal_tls_key
    ca = settings.temporal_tls_ca
    if not (cert or key or ca):
        return None
    return TLSConfig(
        client_cert=Path(cert).read_bytes() if cert else None,
        client_private_key=Path(key).read_bytes() if key else None,
        server_root_ca_cert=Path(ca).read_bytes() if ca else None,
    )


def connect_options() -> dict[str, Any]:
    """The keyword args for `Client.connect`, so transport security is testable without a broker.

    Always the namespace and pydantic converter; plus `tls` when mTLS is configured, `api_key` for a
    Temporal Cloud key, `runtime` when the SDK metrics port is set, and the OpenTelemetry
    interceptor when span export is on. With none set the client connects plaintext (local dev).
    """
    options: dict[str, Any] = {
        "namespace": settings.temporal_namespace,
        "data_converter": pydantic_data_converter,
    }
    # Omitted rather than passed as `runtime=None` (same SDK behaviour), so the options describe
    # only what was configured.
    runtime = telemetry_runtime()
    if runtime is not None:
        options["runtime"] = runtime
    # W3C trace context across the durable boundary, so a job's spans join the turn that started it.
    # The client half propagates on `start_workflow`; `Worker` carries the reading half
    # (`durable/serve.py`). Only under `otel_enabled`, since otherwise the provider is a no-op.
    if settings.otel_enabled:
        options["interceptors"] = [TracingInterceptor()]
    tls = _tls_config()
    if tls is not None:
        options["tls"] = tls
    if api_key := settings.temporal_api_key.get_secret_value():
        options["api_key"] = api_key
    return options


async def connect() -> Client:
    """Return this process's Temporal client, connecting on first use.

    Double-checked around the lock so the warm path is lock-free. `connect_options` runs in a worker
    thread because it reads PEM files. Connection failures are translated here, once, into a
    chemist-readable `SubsystemUnavailableError`; a raw transport error invites the model to
    fabricate the job's result. Failures cache nothing, so the next call retries.

    Raises:
        SubsystemUnavailableError: When the broker cannot be reached, or the configured mTLS
            material cannot be read. The underlying exception is attached as `__cause__`.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    async with _CONNECT_LOCK:
        if _CLIENT is None:
            try:
                options = await asyncio.to_thread(connect_options)
                _CLIENT = await Client.connect(settings.temporal_address, **options)
            except Exception as exc:
                # Every fault here, an unreadable PEM included, means the transport is down; neither
                # the chemist nor the model can act on the difference.
                raise SubsystemUnavailableError(
                    "the durable execution backend (Temporal) is unreachable, so durable jobs "
                    "cannot be started or inspected right now — nothing was queued by this call. "
                    "This is an infrastructure outage, not a problem with the request; the same "
                    "call will work once the backend is back."
                ) from exc
    return _CLIENT
