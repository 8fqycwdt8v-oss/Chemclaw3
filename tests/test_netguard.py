"""The in-process egress guard: it blocks a non-allowlisted host and permits the declared ones."""

import ast
import os
import pathlib
import re
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from chemclaw.api import middleware
from chemclaw.core import netguard
from chemclaw.core.config import Settings, settings
from chemclaw.core.http import is_loopback_host, is_loopback_url


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


def _proxy_settings(**overrides: str) -> Settings:
    """A settings object dialling one non-loopback https gateway, plus a database and a broker.

    The two non-HTTP destinations are here on purpose: they are what `derive_allowed` returns and
    what a first version of this check charged to a proxy, so every "not refused" arm below is also
    an assertion that they are *not* counted.
    """
    return Settings(
        llm_base_url="https://gateway.internal/v1",
        # Every HTTP destination on one host and one scheme, so an arm about *which* destinations
        # are counted is not also an arm about the shipped loopback defaults. Leaving these at
        # their defaults made the scheme arm below fail for a reason that had nothing to do with
        # schemes: `calc_server_url` ships as `http://127.0.0.1:…`, so `HTTP_PROXY` alone really
        # does carry it, and the fixture was measuring that instead of the property under test.
        calc_server_url="https://gateway.internal/calc",
        rxnlabel_server_url="https://gateway.internal/rxnlabel",
        postgres_dsn="postgresql://chemclaw@db.internal:5432/chemclaw",
        temporal_address="temporal.internal:7233",
        egress_allow=overrides.pop("egress_allow", "gateway.internal"),
        **overrides,  # type: ignore[arg-type]
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
    deleted — measured: gutting `refuse_proxied_egress` to `lambda settings: None` left four of the
    arms below green. So each of them re-runs with the bypass removed and asserts the refusal
    *does* fire, which is what distinguishes "this configuration is accepted" from "nothing is
    checked".
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
    `netguard._refused` never moved, while the same request through a *named* proxy and the same
    request with no proxy were both refused at `getaddrinfo`. The loopback arm needs no
    allowlisting because `_check` exempts loopback by construction — and it must keep doing so,
    since this process dials Postgres, Temporal and the calc backend there.

    A loopback proxy is not an attacker-only shape: an OpenShift service mesh or egress sidecar is
    one by design, and it is also the reason the module docstring's usual fallback does not apply —
    a sidecar shares the pod's network namespace, so its traffic never crosses a NetworkPolicy
    enforcement point. There is no layer below this one for this shape.
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
    ],
)
def test_every_proxy_spelling_is_read(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    """Every case, not both cases — and the difference was a working bypass of this whole check.

    The first version of this read `name` and `name.upper()` for three names, and its docstring
    argued the case correctly ("a control an operator disables by typing the variable in lower
    case") one step short of the conclusion. `urllib.getproxies_environment` lowercases *every*
    `*_proxy` name, and so do httpx and requests: measured, `Https_Proxy`, `HTTPS_proxy`,
    `https_PROXY`, `Http_Proxy` and `All_Proxy` each gave an httpx client one proxy mount while the
    hand-rolled check saw nothing and the process booted silently.

    So this is parametrised over mixed-case spellings specifically. The lesson is the one the ADR
    already states about `no_proxy` and did not apply to the proxy variables themselves: reading an
    environment convention a second time is writing a second answer to it.
    """
    _proxy_env(monkeypatch, **{variable: "http://127.0.0.1:15001"})
    with pytest.raises(RuntimeError, match="SECURITY: a proxy is configured"):
        netguard.refuse_proxied_egress(_proxy_settings())


def test_a_proxy_named_in_the_allowlist_is_the_operators_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch, and why it is `egress_allow` rather than the loopback exemption.

    A deployment legitimately behind a mesh says so by naming the sidecar, and naming it is exactly
    what distinguishes the operator's intent from an env var somebody else set. Loopback does not
    earn the exemption here for the same reason it is the dangerous case: the address carries no
    signal at all.
    """
    settings_with_proxy = _proxy_settings(egress_allow="gateway.internal,127.0.0.1")
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001")
    assert not _refuses(settings_with_proxy)
    _assert_live(monkeypatch, settings_with_proxy)


def test_no_proxy_covering_every_destination_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The question is not "is a proxy set" but "would it carry anything this process dials".

    Without this, a container that configures a proxy for its own package installs and excludes the
    cluster — which is what this repository's own CI and dev sandboxes do — could not run the
    process at all. The bypass test is the stdlib's own (`urllib.request.proxy_bypass`) rather than
    a second reading of `no_proxy` written here, because a second reading is a second answer.
    """
    settings = _proxy_settings()
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001", NO_PROXY="gateway.internal")
    assert not _refuses(settings)
    _assert_live(monkeypatch, settings)


def test_a_wildcard_no_proxy_bypasses_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """`no_proxy=*` is a real configuration and both httpx and the stdlib honour it.

    Checked because the two have to agree: if this check read `no_proxy` itself it could easily
    treat `*` as a literal hostname and refuse a deployment whose clients proxy nothing at all.
    """
    settings = _proxy_settings()
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001", NO_PROXY="*")
    assert not _refuses(settings)
    _assert_live(monkeypatch, settings)


def test_no_proxy_that_misses_one_destination_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One uncovered destination is enough, which is what makes the bypass check a narrowing.

    The failure to avoid is a `NO_PROXY` that looks thorough and leaves the gateway out — the one
    destination whose traffic is the prompts and the bearer.
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001", NO_PROXY="127.0.0.1,localhost")
    with pytest.raises(RuntimeError, match="gateway.internal"):
        netguard.refuse_proxied_egress(_proxy_settings())


def test_a_proxy_for_another_scheme_carries_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HTTP_PROXY` alone against an `https` destination proxies nothing, and must not refuse.

    Measured on httpx: with only `HTTP_PROXY` set and every destination on `https`, the client
    builds a plain connection pool rather than an `HTTPProxy` transport — so a check that asked
    "is any proxy variable set" reported as carried a configuration in which nothing leaves through
    the proxy. This is the arm that makes the per-scheme lookup load-bearing rather than tidy.
    """
    settings = _proxy_settings()
    _proxy_env(monkeypatch, HTTP_PROXY="http://127.0.0.1:15001")
    assert not _refuses(settings)
    _assert_live(monkeypatch, settings)


def test_a_database_and_a_broker_are_not_charged_to_a_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`derive_allowed` is every host by any protocol; a proxy variable carries only HTTP.

    Measured against live Postgres with all three proxy variables pointed at a dead port: psycopg
    connects and returns a row — libpq does not read them. Charging the DSN host and the Temporal
    frontend to a proxy would refuse a deployment whose `NO_PROXY` covers every HTTP destination
    and simply does not name its database, which is a pod that will not start for a reason that is
    not true. The settings here dial both, so this arm fails the moment the destination set widens
    back to `derive_allowed`.
    """
    settings = _proxy_settings()
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001", NO_PROXY="gateway.internal")
    assert not _refuses(settings), "a non-HTTP destination was charged to a proxy"
    assert netguard.proxied_destinations(settings) == {}
    for host in ("db.internal", "temporal.internal"):
        assert host in netguard.derive_allowed(settings), (
            "the premise: these are on the allowlist, so this arm is about the narrowing rather "
            "than about them being absent"
        )


def test_no_proxy_configured_is_the_silent_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overwhelmingly common configuration must cost nothing and say nothing."""
    settings = _proxy_settings()
    _proxy_env(monkeypatch)
    assert not _refuses(settings)
    _assert_live(monkeypatch, settings)


def test_disabling_the_guard_disables_this_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """`arm_from_settings` returns before the refusal, and that is deliberate.

    A deployment that has opted out of the guard has opted out of this with it — one opt-out, said
    once at WARNING, rather than a second switch nobody knows to look for.
    """
    off = _proxy_settings(egress_guard_enabled=False)  # type: ignore[arg-type]
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001")
    netguard.arm_from_settings(off)
    assert _refuses(_proxy_settings()), (
        "the positive control: the same environment must refuse when the guard is enabled, or "
        "this arm proves nothing about the opt-out"
    )


def test_the_refusal_names_the_proxy_and_what_it_would_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator reading this at boot has to be able to act on it without reading the source.

    Three things have to be in the message: which proxy, which destinations it would carry, and the
    one edit that says "this is intended" — *in the form that works*. Measured, only the bare host
    is accepted: `proxy.corp:3128` and `http://proxy.corp:3128` both still refuse, and both land in
    `derive_allowed` verbatim where `_check` can never match them. So the message says so.
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://sidecar.internal:15001")
    with pytest.raises(RuntimeError) as raised:
        netguard.refuse_proxied_egress(_proxy_settings())
    message = str(raised.value)
    assert "sidecar.internal" in message
    assert "gateway.internal" in message
    assert "CHEMCLAW_EGRESS_ALLOW" in message
    assert "bare host" in message, "the form that actually works has to be in the message"


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
    non-loopback *bind* and a worker does not bind. Without this arm, deleting one line from
    `arm_from_settings` would leave every test above green and every process unguarded.

    It must also refuse *before* arming, because a proxied call is a legitimate-looking dial to an
    allowlisted address: arming first would mean starting a process whose guard is structurally
    blind to where its prompts go.
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

    from chemclaw.core.http import gateway_client_kwargs

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
