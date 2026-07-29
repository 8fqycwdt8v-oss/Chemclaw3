"""One place to open a Temporal client, configured consistently.

Both the worker (`workers/`) and the agent's job tools (`agents/`, Phase 1.5+) need a client that
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
from temporalio.contrib.pydantic import pydantic_data_converter

from chemclaw.core.config import settings

# The process's client, built on first use. A module singleton for the same reason the metrics
# registry and the logging configuration are: the thing being shared is a process-wide resource,
# and threading it through six unrelated call sites would be plumbing with no decision in it.
_CLIENT: Client | None = None
# Serialises the first connect so a burst of concurrent tool calls opens one channel, not N. One
# lock per process is correct because one event loop per process is the deployment shape.
_CONNECT_LOCK = asyncio.Lock()


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

    Returns the namespace + pydantic converter always, plus `tls` when mTLS is configured and
    `api_key` when a Temporal Cloud key is configured. In local dev (none set) the client connects
    plaintext, exactly as before F4-T6.
    """
    options: dict[str, Any] = {
        "namespace": settings.temporal_namespace,
        "data_converter": pydantic_data_converter,
    }
    tls = _tls_config()
    if tls is not None:
        options["tls"] = tls
    if settings.temporal_api_key:
        options["api_key"] = settings.temporal_api_key
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
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    async with _CONNECT_LOCK:
        if _CLIENT is None:
            options = await asyncio.to_thread(connect_options)
            _CLIENT = await Client.connect(settings.temporal_address, **options)
    return _CLIENT
