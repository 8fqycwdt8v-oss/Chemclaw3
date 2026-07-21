"""One place to open a Temporal client, configured consistently.

Both the worker (`workers/`) and the agent's job tools (`agents/`, Phase 1.5+) need a client that
points at the configured address/namespace and uses the pydantic data converter so our models
serialize losslessly. Extracted here so that wiring is written once, not copied per caller (DRY).

Securing the transport (plan F4-T6, §7.2) is one of the two non-Entra bridges: identity rides
*inside* the workflow payload (`requested_by`, F4-T3), never the transport, so here we only
authenticate the connection — mTLS (client cert/key + server-root CA) or a Temporal Cloud API key.
The connect options are built by a pure helper so they can be asserted in tests without a broker.
"""

from pathlib import Path
from typing import Any

from temporalio.client import Client, TLSConfig
from temporalio.contrib.pydantic import pydantic_data_converter

from chemclaw.config import settings


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
    """Connect to Temporal with the configured address, namespace, converter, and transport auth."""
    return await Client.connect(settings.temporal_address, **connect_options())
