"""The two non-Entra transport bridges carry identity as a claim (plan F4-T6), offline.

Temporal: `connect_options` builds mTLS/api-key transport auth from config — asserted on the
constructed args, no live broker. HPC: `map_to_hpc_identity` maps a requesting Entra oid to the
shared HPC service identity and logs every mapping (the sole audit link back to the real user).
"""

import logging

import pytest

from agents.identity.hpc_bridge import map_to_hpc_identity
from chemclaw.config import settings
from chemclaw.temporal_client import connect_options


def test_hpc_bridge_maps_and_logs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Entra oid maps to the configured HPC identity and the mapping is logged."""
    monkeypatch.setattr(settings, "hpc_bridge_identity", "hpc-svc")
    with caplog.at_level(logging.INFO, logger="agents.identity.hpc_bridge"):
        identity = map_to_hpc_identity("entra-oid-1")
    assert identity == "hpc-svc"
    assert "entra-oid-1" in caplog.text  # the real user is in the audit line
    assert "hpc-svc" in caplog.text  # alongside the identity the cluster actually saw


def test_temporal_plaintext_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no TLS/API-key config the client connects plaintext (dev), as before F4-T6."""
    for field in ("temporal_tls_cert", "temporal_tls_key", "temporal_tls_ca", "temporal_api_key"):
        monkeypatch.setattr(settings, field, "")
    options = connect_options()
    assert "tls" not in options and "api_key" not in options
    assert options["namespace"] == settings.temporal_namespace


def test_temporal_api_key_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured Temporal Cloud API key rides in the connect options."""
    monkeypatch.setattr(settings, "temporal_api_key", "cloud-key")
    assert connect_options()["api_key"] == "cloud-key"


def test_temporal_mtls_is_wired(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Configured mTLS PEM paths are read into a TLSConfig on the connect options."""
    cert = tmp_path / "client.crt"  # type: ignore[operator]
    key = tmp_path / "client.key"  # type: ignore[operator]
    ca = tmp_path / "ca.crt"  # type: ignore[operator]
    cert.write_bytes(b"CERT")
    key.write_bytes(b"KEY")
    ca.write_bytes(b"CA")
    monkeypatch.setattr(settings, "temporal_tls_cert", str(cert))
    monkeypatch.setattr(settings, "temporal_tls_key", str(key))
    monkeypatch.setattr(settings, "temporal_tls_ca", str(ca))

    tls = connect_options()["tls"]
    assert tls.client_cert == b"CERT"
    assert tls.client_private_key == b"KEY"
    assert tls.server_root_ca_cert == b"CA"
