"""The in-process egress guard: it blocks a non-allowlisted host and permits the declared ones."""

import re
import socket
from collections.abc import Iterator

import pytest

from chemclaw.core import netguard
from chemclaw.core.config import Settings


@pytest.fixture(autouse=True)
def _restore_allowlist() -> Iterator[None]:
    """Snapshot and restore the config-derived allowlist around a test that mutates it.

    `_reset_for_tests` writes the module-global `_allowed`; without this, a later test in a full run
    would inherit an emptied allowlist and refuse a legitimately-configured connector host.
    """
    saved = netguard.allowed_hosts()
    yield
    netguard._reset_for_tests(saved)


def test_guard_is_armed_at_config_import() -> None:
    """Importing config arms the guard, so it is a property of the system, not of a launcher."""
    import chemclaw.core.config  # noqa: F401  (the import is the arming)

    assert netguard.armed()


def test_a_non_allowlisted_host_is_refused() -> None:
    """An external host that is not the gateway or declared infra is refused, not dialled."""
    with pytest.raises(netguard.EgressForbidden):
        socket.getaddrinfo("pypi.org", 443)
    with pytest.raises(netguard.EgressForbidden):
        socket.create_connection(("140.82.112.3", 443), timeout=1)


def test_loopback_and_allowlisted_hosts_pass() -> None:
    """Loopback (Postgres/Temporal/calc dev defaults) and an allowlisted host are not refused."""
    netguard._reset_for_tests(["llm.internal.example"])
    # loopback is decided by address, never refused
    netguard._check(("127.0.0.1", 5432))
    netguard._check(("localhost", 7233))
    netguard._check(("::1", 8860))
    # an allowlisted host passes
    netguard._check(("llm.internal.example", 443))
    # a non-allowlisted one does not
    with pytest.raises(netguard.EgressForbidden):
        netguard._check(("evil.example", 443))


def test_localhost_suffix_is_not_trusted() -> None:
    """A `.localhost` suffix is NOT loopback (the sibling guard's bug): only exact `localhost` is.

    An /etc/hosts line or a wildcard zone would otherwise turn the suffix into "any destination".
    """
    assert netguard._is_loopback("localhost")
    assert netguard._is_loopback("127.0.0.1")
    assert netguard._is_loopback("::1")
    assert not netguard._is_loopback("exfil.localhost")
    assert not netguard._is_loopback("evil.example")


def test_a_bytes_host_does_not_walk_past_the_check() -> None:
    """A bytes host in the address tuple is decoded and checked, not treated as unreadable."""
    netguard._reset_for_tests([])
    with pytest.raises(netguard.EgressForbidden):
        netguard._check((b"evil.example", 443))


def test_the_allowlist_is_derived_from_the_dialled_destinations() -> None:
    """The allowlist comes from the settings the process actually dials, not a static list."""

    class _S:
        llm_base_url = "https://llm.internal.example:8000/v1"
        llm_fallback_base_url = ""
        postgres_dsn = "postgresql://u:p@pg.internal:5432/db"
        postgres_migration_dsn = ""
        session_store_dsn = ""
        temporal_address = "temporal.internal:7233"
        calc_server_url = "http://calc.internal:8860/mcp"
        rxnlabel_server_url = "http://rxnlabel.internal:8865/mcp"
        connector_urls = {"calc": "http://calc-bundle.internal:8815/mcp"}
        entra_required = False
        entra_jwks_endpoint = ""
        entra_jwks_url = ""
        otel_enabled = False
        otel_endpoint = ""
        vector_store_provider = "pgvector"
        vector_store_url = ""
        egress_allow = "mirror.internal"

    hosts = netguard.derive_allowed(_S())
    assert "llm.internal.example" in hosts
    assert "pg.internal" in hosts
    assert "temporal.internal" in hosts
    assert "calc.internal" in hosts
    assert "rxnlabel.internal" in hosts
    assert "calc-bundle.internal" in hosts
    assert "mirror.internal" in hosts
    # **No vendor host is ever on this list, and it used to be** — `derive_allowed` added
    # `api.anthropic.com` whenever `llm_provider == "anthropic"`, which was the shipped default. So
    # the guard whose job is to bound where a prompt can go was opening exactly the destination the
    # exfiltration path used, on the configuration a fresh checkout gets
    # (`D-2026-09-04-a-gateway-is-the-only-provider`). There is no branch left, and this fixture
    # declares no `llm_provider` — which is itself the assertion: a re-added one would be a
    # `getattr` default and this list would grow again.
    assert "api.anthropic.com" not in hosts
    assert "api.openai.com" not in hosts
    assert hosts == {
        "llm.internal.example",
        "pg.internal",
        "temporal.internal",
        "calc.internal",
        "rxnlabel.internal",
        "calc-bundle.internal",
        "mirror.internal",
    }


# Every destination-shaped `Settings` field this process is *not* expected to dial, with the
# reason. A row here is a claim about which process holds the destination, so each names one — and
# an empty exception list would be a stronger claim than this system can make, because the two
# below are genuinely somebody else's socket.
_NOT_DIALLED_BY_A_GUARDED_PROCESS = {
    "live_probe_base_url": "the live lane dials the front door from `cli/live_probes.py`",
    "phoenix_base_url": "`cli/phoenix_publish.py` uploads a dataset from an operator's shell",
}

# What a field naming a destination is called. Anchored on the suffix, because the *kind* of
# address varies (a URL, a bare `host:port`, a libpq DSN) and only the suffix is common to all of
# them.
_DESTINATION_FIELD = re.compile(r"_(url|endpoint|address|dsn)$")


def test_every_destination_shaped_setting_is_on_the_allowlist_it_derives() -> None:
    """The derivation checks itself, rather than being kept in step by whoever reads it.

    `derive_allowed` is a hand-maintained walk over named settings, and its module docstring claims
    the allowlist "cannot drift from what a legitimate call needs" because it is read off the same
    settings the process dials with. That is a property of the *list*, not of the mechanism, and it
    had drifted twice by the time anyone measured it: `session_store_dsn` — the split session
    database the chart provisions a secret key for — and `rxnlabel_server_url`, the sibling of the
    `calc_server_url` line directly above it. Both failures are a control refusing a configured,
    legitimate destination, and both surface as an `OSError` that says nothing about egress: the
    session store as an outage, the labelling server as "the labelling server is not answering"
    with a drain that retries forever.

    So the assertion is over `Settings.model_fields` rather than over a list written here: every
    field whose name ends in a destination word is given a distinct sentinel host, and each must
    come back on the allowlist or be named above with the process that dials it instead. A setting
    added next year lands in this test the day it is declared.
    """
    hosts = {name: f"{name.replace('_', '-')}.sentinel.example" for name in Settings.model_fields}
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        # The enforced posture, because three of the destinations below are only dialled in it —
        # and it brings its own guards, which is why the DSNs state a verified sslmode and the
        # broker names a CA.
        entra_required=True,
        entra_tenant_id="t",
        entra_audience="api://x",
        harness_enabled=True,
        temporal_tls_ca="/ca.pem",
        llm_model="m",
        llm_base_url=f"https://{hosts['llm_base_url']}/v1",
        llm_fallback_base_url=f"https://{hosts['llm_fallback_base_url']}/v1",
        postgres_dsn=f"postgresql://u:p@{hosts['postgres_dsn']}/db?sslmode=verify-full",
        postgres_migration_dsn=(
            f"postgresql://u:p@{hosts['postgres_migration_dsn']}/db?sslmode=verify-full"
        ),
        session_store_dsn=f"postgresql://u:p@{hosts['session_store_dsn']}/db?sslmode=verify-full",
        temporal_address=f"{hosts['temporal_address']}:7233",
        calc_server_url=f"https://{hosts['calc_server_url']}/mcp",
        rxnlabel_server_url=f"https://{hosts['rxnlabel_server_url']}/mcp",
        entra_jwks_url=f"https://{hosts['entra_jwks_url']}/keys",
        otel_enabled=True,
        otel_endpoint=f"https://{hosts['otel_endpoint']}:4317",
        vector_store_provider="qdrant",
        vector_store_url=f"https://{hosts['vector_store_url']}:6333",
    )

    allowed = netguard.derive_allowed(settings)
    missing = sorted(
        name
        for name in Settings.model_fields
        if _DESTINATION_FIELD.search(name)
        and name not in _NOT_DIALLED_BY_A_GUARDED_PROCESS
        and hosts[name] not in allowed
    )
    assert missing == [], (
        f"{missing} name a destination this process dials and the egress guard would refuse it. "
        "Add it to `derive_allowed`, or to `_NOT_DIALLED_BY_A_GUARDED_PROCESS` with the process "
        "that holds the socket."
    )
    # The other direction: a row whose field stopped being a destination, or started being dialled
    # here after all, re-blesses an omission for the next reader.
    stale = sorted(
        name
        for name, host in hosts.items()
        if name in _NOT_DIALLED_BY_A_GUARDED_PROCESS
        and (not _DESTINATION_FIELD.search(name) or host in allowed)
    )
    assert stale == [], f"{stale} no longer need an exception row"


def test_a_connect_to_a_resolved_ip_is_permitted() -> None:
    """The getaddrinfo→connect flow: a connect to an IP an allowed name resolved to is permitted.

    This is the case a naive allowlist guard breaks — the allowlist holds a *hostname* but `connect`
    receives the *IP* the name resolved to. `getaddrinfo` records the resolved IPs (see
    `test_getaddrinfo_records_the_resolved_ip`); here we assert the consequence: a recorded IP is
    allowed at `connect`, while an IP never resolved from an allowed name is refused (the
    direct-to-IP bypass stays closed).
    """
    netguard._reset_for_tests(["llm.internal.example"])
    netguard._resolved_ips.clear()
    netguard._resolved_ips.add("203.0.113.5")  # as the patched getaddrinfo would have recorded it
    netguard._check(("203.0.113.5", 443))  # permitted — it is a resolved IP
    with pytest.raises(netguard.EgressForbidden):
        netguard._check(("198.51.100.7", 443))  # refused — never resolved from an allowed name
    netguard._resolved_ips.clear()


def test_getaddrinfo_records_the_resolved_ip() -> None:
    """The patched getaddrinfo records the IPs a resolution returned, for the connect check.

    `localhost` resolves (unlike a synthetic name in this sandbox) and is loopback, so it exercises
    the recording path end-to-end through the real armed guard.
    """
    netguard._resolved_ips.clear()
    infos = socket.getaddrinfo("localhost", 80)
    resolved = {entry[4][0] for entry in infos}
    assert resolved <= netguard._resolved_ips, "getaddrinfo did not record the resolved IPs"
    netguard._resolved_ips.clear()


def test_a_blocked_name_never_reaches_connect() -> None:
    """A non-allowlisted name is refused at getaddrinfo, so its IP is never recorded."""
    netguard._reset_for_tests([])
    before = set(netguard._resolved_ips)
    with pytest.raises(netguard.EgressForbidden):
        socket.getaddrinfo("blocked.example", 443)
    assert netguard._resolved_ips == before, "a refused resolution still recorded an IP"
