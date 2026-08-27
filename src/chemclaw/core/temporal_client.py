"""One place to open a Temporal client, configured consistently.

Both the workers (`durable/`, `connectors/*/worker.py`) and the agent's job tools (`agent/`) need
a client that
points at the configured address/namespace and uses the pydantic data converter so our models
serialize losslessly. Extracted here so that wiring is written once, not copied per caller (DRY).

Securing the transport (plan F4-T6, §7.2) is one of the two non-Entra bridges: identity rides
*inside* the workflow payload (`requested_by`, F4-T3), never the transport, so here we only
authenticate the connection — mTLS (client cert/key + server-root CA) or a Temporal Cloud API key.
The connect options are built by a pure helper so they can be asserted in tests without a broker.

**One client per process.** `connect()` used to open a new gRPC channel per call — every
connector-job launch, every status poll, every approval route, every schedule description. In
production that transport is mTLS, so each was a full TLS handshake plus three blocking
`Path.read_bytes()` for the PEMs, all on the event loop serving the chat surface. A Temporal
`Client` is designed to be long-lived and multiplexes concurrent calls over its channel, so the
correct number to hold is one; it is cached here rather than at each of the six call sites (which
patch this symbol in tests and would each have needed their own cache).
"""

import asyncio
from pathlib import Path
from typing import Any

from temporalio.client import Client, TLSConfig
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError

# The process's client, built on first use. A module singleton for the same reason the metrics
# registry and the logging configuration are: the thing being shared is a process-wide resource,
# and threading it through six unrelated call sites would be plumbing with no decision in it.
_CLIENT: Client | None = None
# Serialises the first connect so a burst of concurrent tool calls opens one channel, not N. One
# lock per process is correct because one event loop per process is the deployment shape.
_CONNECT_LOCK = asyncio.Lock()
# The SDK's own telemetry runtime, built at most once per process (below). A `Runtime` owns a Rust
# core and a bound socket, so a second one is either a bind failure or a second exposition nobody
# scrapes — which is why this is a module singleton beside the client rather than a per-connect
# object.
_RUNTIME: Runtime | None = None


def telemetry_runtime() -> Runtime | None:
    """The Temporal SDK runtime exporting its own metrics, or `None` when that is switched off.

    **What this closes.** `Client.connect` takes a `runtime=` and nothing in `src/` ever passed
    one, so the SDK's entire metric surface was absent: no poller count, no worker slot
    saturation, no sticky-cache size or miss rate, no `activity_schedule_to_start_latency`, no
    `activity_execution_failed`, no `workflow_task_execution_failed`. None of those is derivable
    from anything this repository counts — they are facts about the *worker*, and the first two
    are the only reading of whether `worker_max_concurrent_activities` is the bottleneck. A CREST
    search holds one of eight slots for hours, so a saturated `connector-calc` worker and an idle
    one produced identical dashboards.

    Exposed on its own port rather than folded into `chemclaw_*`: the names, labels and
    cardinality are the SDK's, and `core/metrics.py` is a registry that refuses an undeclared
    label set by design. Two ports, one scrape config each.

    Returns `None` when `temporal_metrics_port` is 0, which is the default — a process that binds
    a port nobody asked for is a surprise outside a cluster, and two workers on one developer
    machine cannot both bind.
    """
    global _RUNTIME
    if not settings.temporal_metrics_port:
        return None
    if _RUNTIME is None:
        _RUNTIME = Runtime(
            telemetry=TelemetryConfig(
                metrics=PrometheusConfig(
                    bind_address=(
                        f"{settings.temporal_metrics_host}:{settings.temporal_metrics_port}"
                    )
                )
            )
        )
    return _RUNTIME


def _tls_config() -> TLSConfig | None:
    """Build an mTLS config from the configured PEM paths, or `None` when none are set.

    A client cert+key authenticates this component to the Temporal frontend; the server-root CA
    pins the frontend. Any subset may be set (e.g. only a CA for server-auth), so each path is
    read independently and absent ones stay `None`.
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

    Returns the namespace + pydantic converter always, plus `tls` when mTLS is configured,
    `api_key` when a Temporal Cloud key is configured, `runtime` when the SDK's own metrics port
    is set, and the OpenTelemetry interceptor when span export is on. In local dev (none set) the
    client connects plaintext, exactly as before F4-T6.
    """
    options: dict[str, Any] = {
        "namespace": settings.temporal_namespace,
        "data_converter": pydantic_data_converter,
    }
    # The SDK's own metrics, when a deployment asked for them. `None` is not passed through as
    # `runtime=None` — that is the SDK's "use the lazy default runtime" value and means the same
    # thing, but leaving the key out keeps `connect_options()` a description of what was
    # *configured*, which is what the tests read it as.
    runtime = telemetry_runtime()
    if runtime is not None:
        options["runtime"] = runtime
    # W3C trace context across the durable boundary. Without it a trace stopped dead at every
    # durable job: the correlation id already rode in the payload (`ConnectorJobInput`) and in the
    # workflow memo, so log lines could be joined by grep, and no span could — a six-hour job was
    # an orphan root with no link to the turn that asked for it. The client half propagates the
    # context on `start_workflow`; `Worker` carries the matching half (`durable/serve.py`'s two
    # callers), and both are needed because the context has to be written on one side and read on
    # the other.
    #
    # Behind `otel_enabled` for the same reason the tracer provider is: with tracing off the
    # global provider is a no-op, so this would attach an empty header to every workflow start to
    # no purpose.
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

    Cached because a `Client` is a long-lived multiplexed channel, not a per-call object: opening
    one per call meant an mTLS handshake per connector-job launch and per status poll. The
    double-check around the lock keeps the warm path lock-free, which is the path every tool call
    takes.

    `connect_options` is built in a worker thread because it reads the mTLS PEMs from disk — three
    blocking reads that would otherwise land on the event loop serving the chat surface. It runs
    once per process, but "once" on a slow mount is still a stall nobody can attribute.

    **A failure is translated here, once, for all of `connect()`'s callers.** temporalio reports an
    unreachable broker as `RuntimeError('Failed client connect: Server connection error:
    tonic::transport::Error(Transport, ConnectError(...))')`, which reaches the model as "Error:
    Function failed." for the model — and in the 2026-08-03 live run the model met that in
    `request_development_report` and wrote the whole report by hand instead. There is one client, so
    there is one message: framing it per call site would be the same sentence maintained N times,
    and only one of the callers ever grew it — the connector-job launcher, i.e. the one path someone
    had already sat and debugged. Every other durable tool was still leaking the raw error.

    Nothing is cached on the failure path — `_CLIENT` is assigned only from a successful
    `Client.connect`, and the `async with` releases the lock however the body leaves — so an outage
    does not poison the singleton and the next call retries for real.

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
                # Every fault reachable here means the same thing to a caller: the transport is
                # down, an unreadable PEM included — one is fixed by an operator and the other by
                # waiting, and neither is something the chemist or the model can act on differently.
                raise SubsystemUnavailableError(
                    "the durable execution backend (Temporal) is unreachable, so durable jobs "
                    "cannot be started or inspected right now — nothing was queued by this call. "
                    "This is an infrastructure outage, not a problem with the request; the same "
                    "call will work once the backend is back."
                ) from exc
    return _CLIENT
