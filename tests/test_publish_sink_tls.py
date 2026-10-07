"""The result sinks refuse a non-loopback cleartext transport under the enforced posture.

Published records are confidential and an HTTP sink sends a bearer credential, so sinks follow the
same `entra_required` rule as the database and Temporal. Pinned both ways: the refusal fires where
it should, loopback and `https://`/`sslmode` configurations pass, and the guard is inert outside
the enforced posture.
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
