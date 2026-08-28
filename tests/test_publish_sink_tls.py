"""The result sinks refuse a non-loopback cleartext transport under the enforced posture.

A published record is confidential chemistry, and an HTTP sink additionally carries a bearer
credential in every request. The system's own database and the Temporal broker already refuse
plaintext-or-unverified transport under `entra_required` (`require_pg_tls`, the Temporal-mTLS
guard); a sink that writes the same-sensitivity data to *another* store is the same exposure and is
refused on the same terms. These tests pin both directions — the refusal fires where it should, and
loopback dev and `https://`/`sslmode`-bearing configurations are left alone — and that the guard is
inert when the deployment does not claim the enforced posture, so a dev sink is never blocked.
"""

import pytest

from chemclaw.core.config import settings
from chemclaw.publish.connect import SinkConnectionError
from chemclaw.publish.drivers.http import HttpResultSink
from chemclaw.publish.drivers.postgres import PostgresWarehouse


def test_http_sink_refuses_non_loopback_plaintext_under_entra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-loopback `http://` sink leaks the records and its bearer in cleartext — refused."""
    monkeypatch.setattr(settings, "entra_required", True)
    with pytest.raises(ValueError, match="cleartext"):
        HttpResultSink(
            name="lims",
            tenant_id="site",
            url="http://results.internal:8080/publish",
            token_env="CHEMCLAW_LIMS_TOKEN",
        )


def test_http_sink_allows_https_under_entra(monkeypatch: pytest.MonkeyPatch) -> None:
    """`https://` is the fix the refusal names — it constructs."""
    monkeypatch.setattr(settings, "entra_required", True)
    sink = HttpResultSink(
        name="lims",
        tenant_id="site",
        url="https://results.internal/publish",
        token_env="CHEMCLAW_LIMS_TOKEN",
    )
    assert sink is not None


def test_http_sink_allows_loopback_plaintext_under_entra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback dev is exempt: `http://127.0.0.1` never leaves the pod."""
    monkeypatch.setattr(settings, "entra_required", True)
    sink = HttpResultSink(name="dev", tenant_id="site", url="http://127.0.0.1:8080/publish")
    assert sink is not None


def test_http_sink_plaintext_allowed_when_posture_not_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `entra_required` off (dev), the guard is inert and never blocks a sink."""
    monkeypatch.setattr(settings, "entra_required", False)
    sink = HttpResultSink(name="lims", tenant_id="site", url="http://results.internal:8080/publish")
    assert sink is not None


def test_postgres_sink_refuses_non_loopback_prefer_dsn_under_entra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSN whose sslmode leaves libpq's silent-plaintext default is refused off loopback."""
    monkeypatch.setattr(settings, "entra_required", True)
    with pytest.raises(ValueError, match="sslmode"):
        PostgresWarehouse(dsn="postgresql://user:pw@warehouse.internal:5432/results")


def test_postgres_sink_allows_verify_full_dsn_under_entra(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sslmode=verify-full` is the remedy the refusal names — it constructs."""
    monkeypatch.setattr(settings, "entra_required", True)
    driver = PostgresWarehouse(
        dsn="postgresql://user:pw@warehouse.internal:5432/results?sslmode=verify-full"
    )
    assert driver is not None


def test_postgres_sink_refuses_non_loopback_discrete_form_under_entra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discrete host/password params have no sslmode keyword, so a non-loopback host is refused."""
    monkeypatch.setattr(settings, "entra_required", True)
    with pytest.raises(SinkConnectionError, match="sslmode"):
        PostgresWarehouse(host="warehouse.internal", user="u", password="pw", database="results")


def test_postgres_sink_allows_loopback_discrete_form_under_entra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback dev is exempt for the discrete form too."""
    monkeypatch.setattr(settings, "entra_required", True)
    driver = PostgresWarehouse(host="localhost", user="u", password="pw", database="results")
    assert driver is not None
