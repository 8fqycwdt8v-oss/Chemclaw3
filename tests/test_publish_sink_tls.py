"""The result sinks refuse a non-loopback cleartext transport under the enforced posture.

A published record is confidential chemistry, and an HTTP sink additionally carries a bearer
credential in every request. The system's own database and the Temporal broker already refuse
plaintext-or-unverified transport under `entra_required` (`require_pg_tls`, the Temporal-mTLS
guard); a sink that writes the same-sensitivity data to *another* store is the same exposure.

**It is refused on stricter terms than those, and that asymmetry is the subject of half this
file.** Those guards govern this deployment's own database and broker, inside the cluster the
posture describes. A sink is somebody else's store — the one place computed chemistry and a
credential leave this deployment — and `entra_required` is off by default with no shipped
configuration turning it on, so gating on it put the control in exactly the deployments that had
already opted into caring. These tests pin both directions: the refusal fires whatever the posture,
and loopback dev and `https://`/`sslmode`-bearing configurations are left alone.
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


def test_http_sink_refuses_plaintext_even_when_the_posture_is_not_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that used to read the other way, and this is the finding.

    With `entra_required` off — the default, and every shipped configuration — a non-loopback
    `http://` sink constructed and delivered: confidential chemistry plus a bearer credential in
    cleartext, in the deployments least likely to have thought about it. Loopback dev is what the
    exemption is for, and it still works (the test above).
    """
    monkeypatch.setattr(settings, "entra_required", False)
    with pytest.raises(ValueError, match="cleartext"):
        HttpResultSink(name="lims", tenant_id="site", url="http://results.internal:8080/publish")


def test_http_sink_refuses_verify_tls_false_from_a_manifest() -> None:
    """`verify_tls` was a plain keyword reachable from `config:`, and nothing checked its value.

    Its own docstring said "never set this false"; `sink-validate` binds the driver's *signature*,
    not its values, so `verify_tls: false` in a `sink.yaml` was accepted and delivered without a
    word. A path to a CA bundle is the case the docstring claimed to serve and is now what it
    actually accepts.
    """
    with pytest.raises(ValueError, match="unauthenticated"):
        HttpResultSink(
            name="lims", tenant_id="site", url="https://results.internal/publish", verify_tls=False
        )
    trusted = HttpResultSink(
        name="lims",
        tenant_id="site",
        url="https://results.internal/publish",
        verify_tls="/etc/pki/internal-ca.pem",
    )
    assert trusted is not None


def test_a_posted_batch_carries_an_idempotency_key_over_its_own_content() -> None:
    """The outbox is at-least-once, so a receiver needs something to deduplicate a redelivery on.

    The envelope carried `{tenant_id, writer_version, contract_version, records}` and no batch id,
    and the headers carried only `content-type` plus the bearer — so `deliver`'s own docstring
    conceded that a receiver appending rather than upserting "will accumulate duplicates on any
    transient failure". `calc_ref` alone cannot serve: one calculation is legitimately delivered
    again when a second chemist's publication is merged into its outbox row.
    """
    from tests.test_publish_outbox import _record

    sink = HttpResultSink(name="lims", tenant_id="site", url="https://results.internal/publish")
    first = sink._document([_record("a"), _record("b")])
    assert first["batch_id"] == sink._document([_record("a"), _record("b")])["batch_id"], (
        "a redelivery of the same batch must present the same key"
    )
    assert first["batch_id"] != sink._document([_record("a")])["batch_id"]
    assert sink._headers(str(first["batch_id"]))["idempotency-key"] == first["batch_id"]


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
