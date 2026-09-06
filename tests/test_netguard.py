"""The in-process egress guard: it blocks a non-allowlisted host and permits the declared ones."""

import ast
import os
import pathlib
import re
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from chemclaw.api import middleware
from chemclaw.core import netguard
from chemclaw.core.config import Settings, settings
from chemclaw.core.http import gateway_client_kwargs, is_loopback_host, is_loopback_url


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
    assert is_loopback_host("localhost")
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert not is_loopback_host("exfil.localhost")
    assert not is_loopback_host("evil.example")


# One table, driven through every caller below. Each row is (host, is it unreachable from the
# network?) — the *only* question any of them asks, whether the address arrives as a bind, an
# endpoint or a destination.
#
# The four marked rows are where the two answers used to differ, measured on 2026-09-05 against
# `aed402c`: `core.http.LOOPBACK_HOSTS` was three literal strings and `netguard._is_loopback`
# parsed, so `127.0.0.2`, `0.0.0.0`, `::` and a bracketed `[::1]` were loopback to one and not to
# the other. The consequence was not cosmetic — a pod with the shipped `service_host="0.0.0.0"`
# bind and `CHEMCLAW_LLM_BASE_URL=http://127.0.0.2:8820/v1` passed
# `api.middleware._refuse_unconfigured_llm_gateway`, the guard added to catch exactly that, and
# then failed every turn on a refused connection.
_ADDRESSES: list[tuple[str, bool]] = [
    ("127.0.0.1", True),
    ("127.0.0.2", True),  # was: loopback to the guard, network-exposed to the front door
    ("127.255.255.254", True),  # the rest of 127.0.0.0/8, which no literal set can enumerate
    ("localhost", True),
    ("::1", True),
    ("[::1]", True),  # a bracketed literal — the set never stripped them
    ("::1%lo0", True),  # a zone id
    # The unspecified address is NOT loopback, and this is the one row the two roles disagree on
    # for a real reason: as a *bind* it is every interface (the whole subject of SEC-2), as a
    # *destination* it never leaves the host. The shared answer is the strict one, so a bind is
    # refused and a destination needs an allowlist entry. Nothing dials it — there is no
    # `0.0.0.0` URL in the tree, and no local-server bind idiom reaches the guard's resolver.
    ("0.0.0.0", False),
    ("::", False),
    ("", False),
    ("exfil.localhost", False),  # a suffix is never resolved, never trusted
    ("127.0.0.1.nip.io", False),
    # An IPv4-mapped literal follows its mapped address, both ways. This row was written the
    # other way round from the CPython docs and the parametrized run corrected it, which is the
    # argument for driving a table rather than asserting the cases somebody thought of.
    ("::ffff:127.0.0.1", True),
    ("::ffff:8.8.8.8", False),
    ("llm.internal.example", False),
    ("not an address", False),
]


@pytest.mark.parametrize(("host", "loopback"), _ADDRESSES)
def test_every_caller_gets_one_answer_about_one_address(host: str, loopback: bool) -> None:
    """The shared predicate and both of its callers agree, row by row, over one table.

    The two callers ask different questions of the same fact — the egress guard asks whether a
    *destination* may be dialled without an allowlist entry, the front door asks whether a *bind*
    is network-exposed — and before this test they got their answers from two different pieces of
    code. Pinning them against one table is what makes a third caller's divergence a failing test
    rather than a measurement somebody has to think to take.

    `is_loopback_url` is included because a URL is how two of the four callers actually receive the
    address (`llm_base_url`, a connector's `endpoint.url`), so a bracket- or parse-handling
    difference between the bare host and the URL form would be a divergence of the same kind.
    """
    assert is_loopback_host(host) is loopback

    # The egress guard: loopback needs no allowlist entry, everything else is refused.
    netguard._reset_for_tests([])
    netguard._resolved_ips.clear()
    if loopback:
        netguard._check((host, 443))
    else:
        with pytest.raises(netguard.EgressForbidden):
            netguard._check((host, 443))

    # The front door's bind rule: a loopback bind is dev and boots, anything else is refused.
    with _service_host(host):
        if loopback:
            middleware._refuse_unauthenticated_exposure()
        else:
            with pytest.raises(RuntimeError, match="non-loopback interface"):
                middleware._refuse_unauthenticated_exposure()

    # And the URL form, for the callers that receive one.
    if host and "not an address" not in host:
        bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
        assert is_loopback_url(f"http://{bracketed}:8820/v1") is loopback


@contextmanager
def _service_host(host: str) -> Iterator[None]:
    """Bind `settings.service_host` to `host` in the unauthenticated, no-opt-out posture.

    That posture is what makes `_refuse_unauthenticated_exposure` a pure function of the loopback
    answer: with `entra_required` or `service_allow_insecure` on it returns for another reason and
    would assert nothing about this table.
    """
    saved = (settings.service_host, settings.entra_required, settings.service_allow_insecure)
    settings.service_host = host
    settings.entra_required = False
    settings.service_allow_insecure = False
    try:
        yield
    finally:
        (settings.service_host, settings.entra_required, settings.service_allow_insecure) = saved


def test_the_loopback_answer_has_exactly_one_definition_in_src() -> None:
    """No module may carry a second literal set of loopback hosts. AST-walked, not grepped.

    The disagreement this file's table pins was not a bug inside either predicate — both were
    right about what their own author meant — it was that there were *two*, and the second was a
    three-element set that could not express `127.0.0.0/8`. So the durable guard is against the
    shape: a collection literal naming loopback addresses is a predicate in disguise, and the next
    one would diverge the same way, silently, on the same addresses.

    `PG_LOOPBACK_HOSTS` is the one that remains and it is allowed here rather than folded in,
    because measurement says it is a different predicate rather than a stale copy: it asks *is this
    connection local* for a TLS/plaintext exemption, and its extra `""` member makes a hostless
    sink URL (a `file://` outbox) local. Substituting `is_loopback_host` for it was measured across
    its three readers and moves behaviour both ways — widening the exemption for `127.0.0.2`, the
    rest of `127.0.0.0/8`, `[::1]` and a zone id, and narrowing it for the empty host. The argument
    lives in `core/http.py`'s module docstring; this row is what keeps a *fourth* out.
    """
    known = {"src/chemclaw/core/config/__init__.py": "PG_LOOPBACK_HOSTS"}
    src = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    found: dict[str, list[int]] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
                continue
            literals = {e.value for e in node.elts if isinstance(e, ast.Constant)}
            if "127.0.0.1" in literals and literals & {"localhost", "::1"}:
                rel = path.relative_to(src.parents[1]).as_posix()
                found.setdefault(rel, []).append(node.lineno)

    assert set(found) <= set(known), (
        f"a second literal set of loopback hosts appeared in {sorted(set(found) - set(known))}. "
        "Call `chemclaw.core.http.is_loopback_host` instead — a set of literals cannot express "
        "`127.0.0.0/8`, and the last two definitions disagreed on `127.0.0.2` and `0.0.0.0`."
    )


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


def _proxy_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Clear every proxy variable this environment happens to carry, then set `values`.

    The CI container and the dev sandbox both run behind a filtering proxy of their own, so a test
    that only *sets* a variable is measuring the ambient environment as much as its own arm. The
    clear is by suffix rather than by a list of names, because the names are the thing under test:
    `urllib` treats any `*_proxy` variable in any case as a proxy variable, and a helper that swept
    only the three canonical spellings would leave `Https_Proxy` standing in the arm written to
    prove `Https_Proxy` is read.
    """
    for name in list(os.environ):
        lowered = name.lower()
        if lowered.endswith("_proxy") or lowered == "no_proxy":
            monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _proxy_settings(**overrides: object) -> Settings:
    """A deployment dialling a gRPC OTLP collector, plus destinations no proxy variable can carry.

    The gateway, the calc backend, the database and the broker are here on purpose: they are what
    a first version of this check charged, and every "not refused" arm below is therefore also an
    assertion that they are *not* counted. Charging them refused deployments for a reason that was
    not true — measured, every first-party client for them passes `trust_env=False`, zero proxy
    mounts against two on a default `httpx.Client` — and on the shipped loopback defaults it
    stopped a developer behind a corporate proxy from importing the config at all.
    """
    return Settings(
        llm_base_url="https://gateway.internal/v1",
        calc_server_url="https://calc.internal/mcp",
        postgres_dsn=str(
            overrides.pop("postgres_dsn", "postgresql://chemclaw@db.internal:5432/chemclaw")
        ),
        temporal_address=str(overrides.pop("temporal_address", "temporal.internal:7233")),
        otel_enabled=bool(overrides.pop("otel_enabled", True)),
        otel_endpoint=str(overrides.pop("otel_endpoint", "https://collector.obs:4317")),
        egress_allow=str(overrides.pop("egress_allow", "gateway.internal")),
        **overrides,  # type: ignore[arg-type]
    )


def _entra_settings(**overrides: object) -> Settings:
    """A deployment in the enforced identity posture, whose JWKS fetch goes out through urllib."""
    return _proxy_settings(
        entra_required=True,
        # Loopback, because `entra_required` refuses a plaintext broker channel and this fixture is
        # about the JWKS fetch rather than about Temporal's transport.
        temporal_address="127.0.0.1:7233",
        # Loopback too, for the same reason: `entra_required` refuses a plaintext DSN, and this
        # fixture's subject is the JWKS fetch rather than Postgres' transport.
        postgres_dsn="postgresql://chemclaw@127.0.0.1:5432/chemclaw",
        entra_audience="api://chemclaw",
        entra_tenant_id="tenant",
        harness_enabled=True,
        entra_jwks_url=str(
            overrides.pop(
                "entra_jwks_url", "https://login.microsoftonline.com/tenant/discovery/v2.0/keys"
            )
        ),
        otel_enabled=overrides.pop("otel_enabled", False),
        **overrides,
    )


def _refuses(settings: Settings) -> bool:
    """Whether the boot refusal fires, as a bool — so an arm can assert either direction."""
    try:
        netguard.refuse_proxied_egress(settings)
    except RuntimeError:
        return True
    return False


def _assert_live(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """The positive control every "not refused" arm needs to mean anything.

    A test that asserts "no exception" passes just as well against a function whose body has been
    deleted — measured on an earlier version: gutting `refuse_proxied_egress` left several arms
    green. So each of them re-runs with the bypass removed and asserts the refusal *does* fire,
    which is what distinguishes "this configuration is accepted" from "nothing is checked".
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://control.invalid:3128")
    assert _refuses(settings), (
        "the positive control did not fire, so the negative arm above proves nothing about "
        "whether this check does anything at all"
    )


def test_an_undeclared_proxy_refuses_the_process_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hole this closes: a proxy moves the destination out of the address the guard checks.

    Measured before the fix, with the allowlist empty and one local HTTP proxy: a request to an
    external host through `proxy=http://127.0.0.1:<port>` returned **HTTP 200 with the body** and
    `netguard._refused` never moved. A loopback proxy is not an attacker-only shape — an OpenShift
    service mesh or egress sidecar is one by design, and a sidecar shares the pod's network
    namespace, so its traffic never crosses a NetworkPolicy enforcement point either. There is no
    layer below this one for this shape.
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001")
    with pytest.raises(RuntimeError, match="SECURITY: a proxy is configured"):
        netguard.refuse_proxied_egress(_proxy_settings())


@pytest.mark.parametrize(
    "variable",
    [
        "https_proxy",
        "HTTPS_PROXY",
        "Https_Proxy",
        "HTTPS_proxy",
        "https_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "All_Proxy",
        "grpc_proxy",
        "GRPC_PROXY",
        "Grpc_Proxy",
    ],
)
def test_every_proxy_spelling_is_read(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    """Every case, not both cases — and the difference was a working bypass, twice.

    A first version read `name` and `name.upper()`, which misses every mixed-case spelling while
    httpx, requests, git and grpc all lower-case the name. That was found by review and fixed by
    going through `urllib.request.getproxies_environment`. Re-targeting the check at grpc and
    urllib then required reading the variables directly again — CPython drops `http` from that
    mapping whenever `REQUEST_METHOD` is set, and neither git nor grpc implements the carve-out —
    and the *same* two-spelling shortcut came back with it. Measured: `Grpc_Proxy` gave grpc a live
    proxy and the process booted silently. `grpc_proxy` is parametrised here because it is the one
    variable only this deployment's exporter reads.
    """
    _proxy_env(monkeypatch, **{variable: "http://127.0.0.1:15001"})
    with pytest.raises(RuntimeError, match="SECURITY: a proxy is configured"):
        netguard.refuse_proxied_egress(_proxy_settings())


def test_only_the_destinations_whose_clients_read_the_environment_are_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowing this check turns on, asserted as a property rather than described.

    Every first-party HTTP client — the gateway, the embedding seam, the calc backend, every
    connector endpoint — passes `trust_env=False`, so a proxy variable cannot carry any of them.
    Charging them made the refusal fire on deployments where measurably nothing would be proxied,
    and on the shipped loopback defaults it stopped a developer behind a corporate proxy from
    importing the config at all. This arm dials all four and expects silence.
    """
    settings = Settings(
        llm_base_url="https://gateway.internal/v1",
        calc_server_url="https://calc.internal/mcp",
        rxnlabel_server_url="https://rxnlabel.internal/mcp",
        postgres_dsn="postgresql://chemclaw@db.internal:5432/chemclaw",
        temporal_address="temporal.internal:7233",
        egress_allow="gateway.internal",
    )
    _proxy_env(monkeypatch, HTTPS_PROXY="http://sidecar.internal:15001", HTTP_PROXY="http://s:1")
    assert netguard.proxied_destinations(settings) == {}
    assert not _refuses(settings)
    for host in ("gateway.internal", "calc.internal", "db.internal", "temporal.internal"):
        assert host in netguard.derive_allowed(settings), (
            "the premise: these are on the allowlist, so this arm is about the narrowing rather "
            "than about them being absent"
        )


def test_the_shipped_defaults_start_behind_a_corporate_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stock checkout on a developer's machine must import, and once it did not.

    Measured on the version this replaces: `HTTP_PROXY=http://proxy.corp:3128 python -c "import
    chemclaw.core.config"` raised, and `pytest` collection died with it — because every shipped
    destination is loopback and loopback destinations were charged. Anyone behind a corporate proxy
    could not run this repository. The refusal must be about what a proxy can carry, not about
    whether one is configured.
    """
    _proxy_env(monkeypatch, HTTP_PROXY="http://proxy.corp:3128", ALL_PROXY="http://proxy.corp:3128")
    assert not _refuses(Settings())


def test_the_grpc_exporter_is_charged_whatever_the_targets_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grpc resolves its proxy without consulting the target, which no per-scheme rule would catch.

    `core/logging.py` uses the **gRPC** OTLP exporter, and grpc reads `grpc_proxy`, then
    `https_proxy`, then `http_proxy`, regardless of whether the endpoint is `https`. Measured with
    only `http_proxy` set against a TLS target: three `CONNECT` frames reached the recorder. With
    `otel_include_sensitive_data` that traffic is prompts and completions, so this is the live hole
    the re-targeting exists to close.
    """
    settings = _proxy_settings()
    for variable in ("grpc_proxy", "https_proxy", "http_proxy"):
        _proxy_env(monkeypatch, **{variable: "http://sidecar.internal:15001"})
        assert _refuses(settings), f"{variable} must charge the gRPC exporter"


def test_a_bare_host_port_otlp_endpoint_is_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """`collector.obs:4317` is a real OTLP spelling and `urlsplit` reads its host as the *scheme*.

    A scheme-filtered version of this check dropped that form entirely — the destination with the
    most sensitive traffic on it, silently uncharged, on the spelling the OTLP documentation uses.
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://sidecar.internal:15001")
    settings = _proxy_settings(otel_endpoint="collector.obs:4317")
    assert "collector.obs" in " ".join(netguard.proxied_destinations(settings))
    assert _refuses(settings)


def test_the_jwks_fetch_is_charged_by_its_own_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PyJWKClient` goes through `urllib`, which *does* resolve per scheme — unlike grpc.

    The two readers in this check disagree about the environment and both are reproduced rather
    than averaged: an `https` JWKS endpoint is carried by `https_proxy` or `all_proxy` and not by
    `http_proxy`, while the exporter beside it is carried by any of them.
    """
    settings = _entra_settings()
    for variable in ("https_proxy", "all_proxy"):
        _proxy_env(monkeypatch, **{variable: "http://sidecar.internal:15001"})
        assert _refuses(settings), f"{variable} must charge the JWKS fetch"
    _proxy_env(monkeypatch, HTTP_PROXY="http://sidecar.internal:15001")
    assert not _refuses(settings), "an https JWKS endpoint is not carried by HTTP_PROXY"
    _assert_live(monkeypatch, settings)


def test_an_unenforced_identity_posture_has_no_jwks_fetch_to_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`entra_required=False` means nothing fetches the key set, so nothing is proxied."""
    settings = _proxy_settings(
        otel_enabled=False, entra_jwks_url="https://login.microsoftonline.com/t/keys"
    )
    _proxy_env(monkeypatch, HTTPS_PROXY="http://sidecar.internal:15001")
    assert not _refuses(settings)
    _assert_live(monkeypatch, _entra_settings())


def test_a_proxy_named_in_the_allowlist_is_the_operators_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch, and why it is `egress_allow` rather than the loopback exemption.

    A deployment legitimately behind a mesh says so by naming the sidecar, and naming it is exactly
    what distinguishes the operator's intent from an env var somebody else set. Loopback does not
    earn the exemption here for the same reason it is the dangerous case: the address carries no
    signal at all.
    """
    declared = _proxy_settings(egress_allow="gateway.internal,127.0.0.1")
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001")
    assert not _refuses(declared)
    _assert_live(monkeypatch, declared)


def test_no_proxy_covering_every_charged_destination_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The question is not "is a proxy set" but "would it carry anything this process dials".

    The bypass test is the stdlib's own (`urllib.request.proxy_bypass`) rather than a second
    reading of `no_proxy` written here, because a second reading is a second answer.
    """
    settings = _proxy_settings()
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001", NO_PROXY="collector.obs")
    assert not _refuses(settings)
    _assert_live(monkeypatch, settings)


def test_a_wildcard_no_proxy_bypasses_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """`no_proxy=*` is a real configuration and both httpx and the stdlib honour it."""
    settings = _proxy_settings()
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001", NO_PROXY="*")
    assert not _refuses(settings)
    _assert_live(monkeypatch, settings)


def test_two_readers_on_one_host_do_not_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared proxy must not hide an undeclared one reaching the same host by another reader.

    An earlier version keyed `carried` by destination host alone, so two entries for one host
    overwrote each other and only the survivor was compared against the allowlist. **The first
    test written for this could not fail**, because it varied two variables on *one* reader — and a
    reader takes the first variable that hits and stops, so it can only ever record one proxy. The
    collision needs two readers, which is what this deployment has: the gRPC exporter and the
    `urllib` JWKS fetch, resolving different variables, both able to name the same host.

    Here the exporter is carried by an **undeclared** proxy and the JWKS fetch by a declared one,
    on one host. Keyed by host, whichever landed second wins the comparison and the process starts
    with the exporter's spans — prompts, under `otel_include_sensitive_data` — going to a host
    nobody declared.
    """
    shared = _entra_settings(
        otel_enabled=True,
        otel_endpoint="https://shared.internal:4317",
        entra_jwks_url="https://shared.internal/tenant/keys",
        egress_allow="gateway.internal,declared.corp",
    )
    _proxy_env(
        monkeypatch,
        GRPC_PROXY="http://undeclared.corp:3128",
        HTTPS_PROXY="http://declared.corp:3128",
    )
    carried = netguard.proxied_destinations(shared)
    assert len(carried) == 2, f"one reader's entry was overwritten by the other's: {carried}"
    assert _refuses(shared), "the undeclared proxy on the exporter was hidden by the declared one"


def test_no_proxy_configured_is_the_silent_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overwhelmingly common configuration must cost nothing and say nothing."""
    settings = _proxy_settings()
    _proxy_env(monkeypatch)
    assert not _refuses(settings)
    _assert_live(monkeypatch, settings)


def test_disabling_the_guard_disables_this_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """`arm_from_settings` returns before the refusal, and that is deliberate."""
    off = _proxy_settings(egress_guard_enabled=False)
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001")
    netguard.arm_from_settings(off)
    assert _refuses(_proxy_settings()), (
        "the positive control: the same environment must refuse when the guard is enabled, or "
        "this arm proves nothing about the opt-out"
    )


def test_the_refusal_names_the_proxy_the_destination_and_what_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator has to be able to act on this at boot without reading the source.

    Four things: which proxy, which destination, **what reads the environment for it** — because
    "the OTLP exporter" is what makes the refusal checkable rather than mysterious — and the one
    edit that proceeds, in the form that works. Measured, only the bare host is accepted:
    `proxy.corp:3128` and `http://proxy.corp:3128` both still refuse.
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://sidecar.internal:15001")
    with pytest.raises(RuntimeError) as raised:
        netguard.refuse_proxied_egress(_proxy_settings())
    message = str(raised.value)
    assert "sidecar.internal" in message
    assert "collector.obs" in message
    assert "OTLP span exporter" in message
    assert "bare host" in message


def test_only_the_bare_host_form_of_the_escape_hatch_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the message promises, asserted — so the promise cannot drift from the behaviour."""
    _proxy_env(monkeypatch, HTTPS_PROXY="http://sidecar.internal:15001")
    assert _refuses(_proxy_settings(egress_allow="gateway.internal,sidecar.internal:15001"))
    assert _refuses(_proxy_settings(egress_allow="gateway.internal,http://sidecar.internal:15001"))
    assert not _refuses(_proxy_settings(egress_allow="gateway.internal,sidecar.internal"))


def test_arm_from_settings_is_where_the_refusal_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests above call the function; this one pins that anything *calls the function*.

    `arm_from_settings` is the single call `chemclaw.core.config` makes, which is what puts this
    refusal in front of the durable worker as well as the front door — the gap
    `api/middleware._refuse_unconfigured_llm_gateway` has by construction, since its signal is a
    non-loopback *bind* and a worker does not bind.
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001")
    with pytest.raises(RuntimeError, match="SECURITY: a proxy is configured"):
        netguard.arm_from_settings(_proxy_settings())


def test_refusing_the_proxy_does_not_surrender_the_environment_trust_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`trust_env` conflates proxy discovery with the trust store; only the first is objected to.

    The backlog row that opened this named the cost and was right: `trust_env=False` also stops
    httpx reading `SSL_CERT_FILE`/`SSL_CERT_DIR`, so a client that merely dropped `trust_env` would
    swap a deployment's env-supplied trust store for `certifi` — silently, and visibly only as a
    TLS failure against the site's own privately-signed gateway. Measured with a probe bundle
    holding one certificate against `certifi`'s 118: `trust_env=False` with no explicit `verify`
    read **118**, and with the context this builds it reads **1**.

    `create_default_context(cafile=None)` is what makes that a one-liner rather than a second
    setting — it falls through to OpenSSL's own default paths, which honour both variables.
    """
    import ssl

    import certifi
    import httpx

    first = pathlib.Path(certifi.where()).read_text(encoding="utf-8")
    one_cert = first.split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n"
    bundle = tmp_path / "one-ca.pem"
    bundle.write_text(one_cert, encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))

    with httpx.Client(**gateway_client_kwargs("")) as client:
        # Read off the pool the client actually dials with, not off the kwargs — the kwargs are
        # what this test would be asserting against itself.
        context = client._transport._pool._ssl_context  # type: ignore[attr-defined]
        assert isinstance(context, ssl.SSLContext)
        assert len(context.get_ca_certs()) == 1, "the environment-supplied trust store was replaced"
        assert client.trust_env is False
    assert len(ssl.create_default_context().get_ca_certs()) == 1, (
        "the premise: OpenSSL's default paths honour SSL_CERT_FILE, which is what makes "
        "`cafile=None` preserve the deployment's store rather than fall back to certifi"
    )


def _self_signed(label: str, directory: Path) -> tuple[Path, Path]:
    """A throwaway self-signed CA-and-server certificate, as (cert, key).

    **Issued for `127.0.0.1`, not for a name.** The distinguishing fact under test is which issuer
    a client trusts, and a certificate for a made-up hostname cannot express it here: the guard
    this file is about refuses the `getaddrinfo` for any non-allowlisted name, so every arm would
    return "not trusted" for a reason that has nothing to do with the trust store. Every
    certificate is therefore valid for the loopback address and differs only in who signed it —
    `label` names the file, and the handshake decides on the issuer alone.
    """
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / f"{label}.pem"
    key_path = directory / f"{label}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@contextmanager
def _tls_server(cert: Path, key: Path) -> Iterator[int]:
    """Serve one HTTPS endpoint on loopback with `cert`, yielding its port."""
    import http.server
    import ssl as ssl_module
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            return

    context = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()


def _trusts(kwargs: dict[str, Any], port: int) -> bool:
    """Whether a client built from `kwargs` completes a TLS handshake with the server on `port`.

    A handshake rather than `SSLContext.get_ca_certs()`, because that call **does not report a
    `capath` at all** — it is blind to exactly the half of the trust store this file's findings
    turned on, and a table built from it agreed with itself while the stores diverged.
    """
    import httpx

    with httpx.Client(**kwargs) as client:
        try:
            client.get(f"https://127.0.0.1:{port}/")
        except httpx.ConnectError:
            return False
        return True


def _hashed_capath(cert: Path, directory: Path) -> Path:
    """`cert` installed into a fresh OpenSSL hash directory, which is what `capath` requires."""
    import subprocess

    capath = directory / "capath"
    capath.mkdir()
    (capath / cert.name).write_bytes(cert.read_bytes())
    subprocess.run(["openssl", "rehash", str(capath)], check=True, capture_output=True)
    return capath


def test_an_ambient_cert_dir_cannot_widen_a_configured_ca_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one path `llm_tls_ca_bundle` exists for, and a first version of this widened it.

    **`SSLContext.get_ca_certs()` cannot see a `capath` at all**, so the four-row table that
    "verified" the trust store agreed with itself while the stores diverged — `SSL_CERT_DIR` read
    0 on both sides because the instrument was blind, not because the answer matched. This drives a
    real handshake instead, which is the only thing that can tell the difference.

    The defect: httpx's own precedence is *exclusive* (`httpx._config.create_ssl_context` is
    `if SSL_CERT_FILE: … elif SSL_CERT_DIR: … else certifi`), and the first version made `cafile`
    conditional while leaving `capath` unconditional — so an ambient `SSL_CERT_DIR` added roots to
    a client whose configured pin is the whole reason it exists. Measured then: the code this
    replaced refused the rogue CA, httpx with the same pin refused it, and the merged version
    returned 200.
    """
    pinned_cert, _ = _self_signed("pinned.invalid", tmp_path)
    rogue_cert, rogue_key = _self_signed("rogue.invalid", tmp_path)
    monkeypatch.setenv("SSL_CERT_DIR", str(_hashed_capath(rogue_cert, tmp_path)))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    with _tls_server(rogue_cert, rogue_key) as port:
        pinned = gateway_client_kwargs(str(pinned_cert))
        assert not _trusts(pinned, port), (
            "an ambient SSL_CERT_DIR widened a configured CA pin — the environment redirected the "
            "one client whose configuration exists to stop it"
        )
        # The control: with no pin configured, the environment's store *is* the answer, and the
        # same rogue CA is trusted. Without this the assertion above passes on a client that
        # trusts nothing at all.
        assert _trusts(gateway_client_kwargs(""), port), (
            "with no bundle configured the environment's store must still be honoured"
        )


def test_a_configured_bundle_is_the_store_and_certifi_is_not_added_to_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pin that also trusts certifi is not a pin, and `get_ca_certs()` counting is not the check.

    This is the mutation the count-based assertion could not catch: making `cafile` ignore
    `ca_bundle` left the old test green, because it configured the bundle *as* `certifi.where()`
    and then asserted only that the store was non-empty.
    """
    pinned_cert, pinned_key = _self_signed("pinned.invalid", tmp_path)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    with _tls_server(pinned_cert, pinned_key) as port:
        assert _trusts(gateway_client_kwargs(str(pinned_cert)), port), (
            "the configured bundle is not reaching the context"
        )
        assert not _trusts(gateway_client_kwargs(""), port), (
            "with no bundle the default store must not already trust this throwaway CA"
        )


def test_the_environment_store_is_read_the_way_httpx_reads_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`SSL_CERT_FILE` wins over `SSL_CERT_DIR`, exclusively — not merged with it.

    Pinned against upstream rather than against a belief: the assertion is that this function and
    `httpx.Client(trust_env=True)` reach the *same* answer on the same environment, driven through
    a handshake because the two disagree only where the counting instrument is blind.
    """
    file_cert, file_key = _self_signed("fromfile.invalid", tmp_path)
    dir_cert, dir_key = _self_signed("fromdir.invalid", tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(file_cert))
    monkeypatch.setenv("SSL_CERT_DIR", str(_hashed_capath(dir_cert, tmp_path)))

    with _tls_server(dir_cert, dir_key) as port:
        ours = _trusts(gateway_client_kwargs(""), port)
        theirs = _trusts({"trust_env": True, "verify": True}, port)
        assert ours is theirs, (
            f"this function trusts the SSL_CERT_DIR issuer ({ours}) where httpx does not "
            f"({theirs}) — the precedence is a union rather than upstream's elif"
        )
        assert ours is False, (
            "the premise: SSL_CERT_FILE is set, so SSL_CERT_DIR must not be consulted at all"
        )
    with _tls_server(file_cert, file_key) as port:
        assert _trusts(gateway_client_kwargs(""), port), (
            "SSL_CERT_FILE is set and its issuer must be the one that is trusted"
        )
