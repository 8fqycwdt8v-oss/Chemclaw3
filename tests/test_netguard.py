"""The in-process egress guard: it blocks a non-allowlisted host and permits the declared ones."""

import ast
import os
import pathlib
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
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

    `_reset_for_tests` writes the module-global `_allowed`; without restoring it, a later test would
    inherit an emptied allowlist and refuse a configured connector host.
    """
    saved = netguard._allowed
    yield
    netguard._reset_for_tests(saved)


def test_guard_is_armed_at_config_import() -> None:
    """Importing config arms the guard, so it is a property of the system, not of a launcher.

    Read off `_armed` directly, as the armed gauge does.
    """
    import chemclaw.core.config  # noqa: F401  (the import is the arming)

    assert netguard._armed


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
# network?), the only question any caller asks, whether the address arrives as a bind, an endpoint
# or a destination. `127.0.0.2`, `0.0.0.0`, `::` and `[::1]` are the rows two separate predicates
# once disagreed on.
_ADDRESSES: list[tuple[str, bool]] = [
    ("127.0.0.1", True),
    ("127.0.0.2", True),  # was: loopback to the guard, network-exposed to the front door
    ("127.255.255.254", True),  # the rest of 127.0.0.0/8, which no literal set can enumerate
    ("localhost", True),
    ("::1", True),
    ("[::1]", True),  # a bracketed literal — the set never stripped them
    ("::1%lo0", True),  # a zone id
    # The unspecified address is not loopback. As a bind it is every interface; as a destination it
    # never leaves the host. The strict answer is shared: a bind is refused and a destination needs
    # an allowlist entry.
    ("0.0.0.0", False),
    ("::", False),
    ("", False),
    # The short, decimal, octal and hexadecimal spellings `inet_aton(3)` accepts and
    # `ipaddress.ip_address` does not; each reaches `127.0.0.1` on a real socket.
    ("127.1", True),
    ("2130706433", True),
    ("0x7f.1", True),
    ("0177.1", True),
    # And the other direction, so the fallback cannot be read as "any number is loopback":
    # `inet_aton` accepts this one too, as 0.0.48.57.
    ("12345", False),
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

    The egress guard asks whether a destination may be dialled without an allowlist entry; the front
    door asks whether a bind is network-exposed. One table makes a divergence a failing test.
    `is_loopback_url` is included because two callers receive the address as a URL.
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

    In that posture `_refuse_unauthenticated_exposure` depends only on the loopback answer.
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
    """No module carries a second literal set of loopback hosts. AST-walked, not grepped.

    A collection literal of loopback addresses is a predicate in disguise and diverges silently.
    `PG_LOOPBACK_HOSTS` is allowed because it is a different predicate (is this connection local,
    for a TLS exemption, with `""` covering a hostless `file://` sink), argued in `core/http.py`'s
    module docstring.
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
        egress_ssh_resolve_timeout_seconds = _SSH_TIMEOUT

    hosts = netguard.derive_allowed(_S())
    assert "llm.internal.example" in hosts
    assert "pg.internal" in hosts
    assert "temporal.internal" in hosts
    assert "calc.internal" in hosts
    assert "rxnlabel.internal" in hosts
    assert "calc-bundle.internal" in hosts
    assert "mirror.internal" in hosts
    # No vendor host is on this list: the guard bounds where a prompt can go, and the fixture
    # declares no `llm_provider`, so a re-added provider branch would grow the list here.
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


# Every destination-shaped `Settings` field this process is not expected to dial, each naming the
# process that does dial it.
_NOT_DIALLED_BY_A_GUARDED_PROCESS = {
    "live_probe_base_url": "the live lane dials the front door from `cli/live_probes.py`",
    "phoenix_base_url": "`cli/phoenix_publish.py` uploads a dataset from an operator's shell",
}

# What a field naming a destination is called. Anchored on the suffix, because the *kind* of
# address varies (a URL, a bare `host:port`, a libpq DSN) and only the suffix is common to all of
# them.
_DESTINATION_FIELD = re.compile(r"_(url|endpoint|address|dsn)$")


def test_every_destination_shaped_setting_is_on_the_allowlist_it_derives() -> None:
    """Every destination-shaped setting is on the allowlist `derive_allowed` produces.

    `derive_allowed` walks named settings by hand, so a missed one is a legitimate destination
    refused with an `OSError` that says nothing about egress. The assertion runs over
    `Settings.model_fields`: every field ending in a destination word gets a sentinel host that must
    come back on the allowlist, or be named above. A new setting is covered the day it is declared.
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
    """A connect to an IP an allowed name resolved to is permitted.

    The allowlist holds hostnames but `connect` receives the resolved IP; `getaddrinfo` records it.
    An IP never resolved from an allowed name stays refused, closing the direct-to-IP bypass.
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


# --- The wiring, not the decision -------------------------------------------------------------
#
# The tests above ask `netguard._check(...)` for a decision. These dial the real `socket` surface
# under the armed guard, one per patched entry point, at an address no allowed name resolved to, so
# removing `_check` from any wrapper fails. `socket.create_connection` calls `getaddrinfo` even for
# an IP literal, so it cannot prove `connect` itself is wired.

# TEST-NET-2 (RFC 5737). Never on the allowlist, never in `_resolved_ips` — so it is the
# direct-to-IP bypass, which is the whole reason `_resolved_ips` exists.
_BLOCKED_IP = "198.51.100.7"
_BLOCKED_NAME = "blocked.example"

_SocketFactory = Callable[[int], socket.socket]


@pytest.fixture
def open_socket() -> Iterator[_SocketFactory]:
    """A short-timeout socket factory whose sockets are closed when the test ends.

    The timeout matters only if the guard is broken and the real syscall runs; under the armed guard
    nothing is dialled.
    """
    opened: list[socket.socket] = []

    def factory(kind: int = socket.SOCK_STREAM) -> socket.socket:
        sock = socket.socket(socket.AF_INET, kind)
        sock.settimeout(0.25)
        opened.append(sock)
        return sock

    yield factory
    for sock in opened:
        sock.close()


def _dial_connect(open_socket: _SocketFactory) -> None:
    open_socket(socket.SOCK_STREAM).connect((_BLOCKED_IP, 443))


def _dial_connect_ex(open_socket: _SocketFactory) -> None:
    open_socket(socket.SOCK_STREAM).connect_ex((_BLOCKED_IP, 443))


def _dial_sendto(open_socket: _SocketFactory) -> None:
    open_socket(socket.SOCK_DGRAM).sendto(b"leak", (_BLOCKED_IP, 443))


def _dial_sendto_with_flags(open_socket: _SocketFactory) -> None:
    # The three-argument spelling, so the address is read off the *end* of the arguments rather
    # than off a fixed position.
    open_socket(socket.SOCK_DGRAM).sendto(b"leak", 0, (_BLOCKED_IP, 443))


def _dial_sendmsg(open_socket: _SocketFactory) -> None:
    # Positional, so the guard's `len(args) >= 4` branch is the one charged — a datagram socket
    # never calls `connect`, which is why this entry point is patched at all.
    open_socket(socket.SOCK_DGRAM).sendmsg([b"leak"], [], 0, (_BLOCKED_IP, 443))


def _dial_getaddrinfo(open_socket: _SocketFactory) -> None:
    socket.getaddrinfo(_BLOCKED_NAME, 443)


def _dial_gethostbyname(open_socket: _SocketFactory) -> None:
    socket.gethostbyname(_BLOCKED_NAME)


def _dial_gethostbyname_ex(open_socket: _SocketFactory) -> None:
    socket.gethostbyname_ex(_BLOCKED_NAME)


def _dial_getnameinfo(open_socket: _SocketFactory) -> None:
    # Numeric flags, so that a *broken* guard returns immediately instead of doing a reverse
    # lookup: the address still left this process for the resolver to see, which is the point.
    socket.getnameinfo((_BLOCKED_IP, 443), socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)


def _dial_gethostbyaddr(open_socket: _SocketFactory) -> None:
    socket.gethostbyaddr(_BLOCKED_IP)


# One row per entry point `arm()` patches. `bind`/`listen`/`accept` are deliberately not patched
# (the front door and the worker still serve), so they are not here.
_PATCHED_ENTRY_POINTS: list[tuple[str, Callable[[_SocketFactory], None]]] = [
    ("socket.connect", _dial_connect),
    ("socket.connect_ex", _dial_connect_ex),
    ("socket.sendto", _dial_sendto),
    ("socket.sendto/flags", _dial_sendto_with_flags),
    ("socket.sendmsg", _dial_sendmsg),
    ("getaddrinfo", _dial_getaddrinfo),
    ("gethostbyname", _dial_gethostbyname),
    ("gethostbyname_ex", _dial_gethostbyname_ex),
    ("getnameinfo", _dial_getnameinfo),
    ("gethostbyaddr", _dial_gethostbyaddr),
]


@contextmanager
def _nothing_allowed() -> Iterator[None]:
    """An empty allowlist and no recorded resolutions, both restored afterwards.

    `_resolved_ips` is process-wide, so an earlier resolution of the blocked address would otherwise
    permit it here.
    """
    resolved = set(netguard._resolved_ips)
    netguard._reset_for_tests([])
    netguard._resolved_ips.clear()
    try:
        yield
    finally:
        netguard._resolved_ips.clear()
        netguard._resolved_ips.update(resolved)


@pytest.mark.parametrize(
    ("dial",),
    [(dial,) for _, dial in _PATCHED_ENTRY_POINTS],
    ids=[name for name, _ in _PATCHED_ENTRY_POINTS],
)
def test_every_patched_entry_point_refuses_through_the_socket_module(
    dial: Callable[[_SocketFactory], None], open_socket: _SocketFactory
) -> None:
    """Each entry point `arm()` patches refuses a non-allowlisted destination, driven for real.

    Nothing here calls `_check`. Delete the `_check(...)` line from any one of the nine wrappers
    and exactly one of these rows goes red, naming it — which is the property the corpus lacked.
    """
    before = netguard._refused
    with _nothing_allowed(), pytest.raises(netguard.EgressForbidden):
        dial(open_socket)
    assert netguard._refused == before + 1, "the refusal was not counted"


def test_the_patched_connect_still_dials_a_permitted_destination(
    open_socket: _SocketFactory,
) -> None:
    """The wrapper delegates: a permitted `connect` reaches the real one and completes.

    Without this, the row above is satisfied by a `connect` that refuses *everything* — and this
    process dials Postgres, Temporal and the calc backend on loopback every run.
    """
    server = open_socket(socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with _nothing_allowed():
        open_socket(socket.SOCK_STREAM).connect(server.getsockname())


def test_the_guard_patched_every_entry_point_it_says_it_patched() -> None:
    """`arm()` installed a wrapper over each name it patches, so a removed patch fails too.

    The rows above catch a hollowed-out wrapper; this catches one that is no longer installed,
    observed on the live `socket` module.
    """
    import chemclaw.core.config  # noqa: F401  (the import is the arming)

    installed = {
        "socket.connect": socket.socket.connect,
        "socket.connect_ex": socket.socket.connect_ex,
        "socket.sendto": socket.socket.sendto,
        "socket.sendmsg": socket.socket.sendmsg,
        "getaddrinfo": socket.getaddrinfo,
        "gethostbyname": socket.gethostbyname,
        "gethostbyname_ex": socket.gethostbyname_ex,
        "getnameinfo": socket.getnameinfo,
        "gethostbyaddr": socket.gethostbyaddr,
    }
    unpatched = sorted(
        name
        for name, entry in installed.items()
        if getattr(entry, "__module__", None) != netguard.__name__
    )
    assert unpatched == [], f"{unpatched} are the stdlib's own, so the guard never sees the call"


def _proxy_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Clear every proxy variable this environment carries, then set `values`.

    CI and the dev sandbox run behind their own proxies. Cleared by suffix, case-insensitively,
    because `urllib` treats any `*_proxy` variable in any case as a proxy variable.
    """
    for name in list(os.environ):
        lowered = name.lower()
        if lowered.endswith("_proxy") or lowered == "no_proxy":
            monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _proxy_settings(**overrides: object) -> Settings:
    """A deployment dialling a gRPC OTLP collector, plus destinations no proxy variable can carry.

    The gateway, calc backend, database and broker are present so every "not refused" arm also
    asserts they are not charged: their clients pass `trust_env=False`.
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
    """A deployment in the enforced identity posture: `entra_required`, tenant, loopback infra.

    `entra_required` can be popped like the other defaults because it is the gate the ambient arm of
    `refuse_proxied_egress` turns on.
    """
    return _proxy_settings(
        entra_required=bool(overrides.pop("entra_required", True)),
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
    """The positive control every "not refused" arm needs.

    "No exception" also passes against a gutted function, so each arm re-runs with the bypass
    removed and asserts the refusal does fire.
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://control.invalid:3128")
    assert _refuses(settings), (
        "the positive control did not fire, so the negative arm above proves nothing about "
        "whether this check does anything at all"
    )


def test_an_undeclared_proxy_refuses_the_process_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """An undeclared proxy refuses the process at boot.

    A proxy moves the destination out of the address the guard checks, so a request through a local
    proxy would succeed with the allowlist empty. A service mesh or egress sidecar is a loopback
    proxy sharing the pod's network namespace, so no lower layer sees its traffic.
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
    """Every case spelling of every proxy variable is read.

    httpx, requests, git and grpc all lower-case the name, so checking only `name` and
    `name.upper()` misses mixed case. Variables are read directly because
    `urllib.request.getproxies_environment` drops `http` when `REQUEST_METHOD` is set, which git and
    grpc do not. `grpc_proxy` is read only by this deployment's exporter.
    """
    _proxy_env(monkeypatch, **{variable: "http://127.0.0.1:15001"})
    with pytest.raises(RuntimeError, match="SECURITY: a proxy is configured"):
        netguard.refuse_proxied_egress(_proxy_settings())


def test_only_the_destinations_whose_clients_read_the_environment_are_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the destinations whose clients read the environment are charged.

    Every first-party HTTP client passes `trust_env=False`, so a proxy cannot carry them, and
    charging them would refuse deployments where nothing is proxied. This arm dials all four and
    expects silence.
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
    """The shipped defaults import behind a corporate proxy.

    Every shipped destination is loopback; the refusal is about what a proxy can carry, not whether
    one is configured.
    """
    _proxy_env(monkeypatch, HTTP_PROXY="http://proxy.corp:3128", ALL_PROXY="http://proxy.corp:3128")
    assert not _refuses(Settings())


def test_the_grpc_exporter_is_charged_whatever_the_targets_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gRPC exporter is charged whatever the target's scheme.

    grpc reads `grpc_proxy`, then `https_proxy`, then `http_proxy` regardless of scheme, and with
    `otel_include_sensitive_data` that traffic carries prompts and completions.
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


def test_the_jwks_fetch_is_no_longer_a_charged_destination() -> None:
    """The JWKS fetch is not a charged destination.

    `_HttpxJwkClient` fetches with `trust_env=False`, so a row for it would refuse a pod over a
    hazard that does not exist. The positive control is the second half: with `otel_enabled` on, the
    OTLP row must come back, so an emptied `_env_reading_destinations` fails rather than agreeing.
    The enforced posture behind a proxy is covered by the next test.
    """
    charged = [reason for _, reason, _ in netguard._env_reading_destinations(_entra_settings())]
    assert charged == [], f"the enforced posture charges a destination nothing proxies: {charged}"
    control = [
        reason
        for _, reason, _ in netguard._env_reading_destinations(_entra_settings(otel_enabled=True))
    ]
    assert control == ["the OTLP span exporter"], (
        f"the positive control charged {control}, so the empty result above is evidence about "
        "this function having a body, not about the JWKS row being absent from it"
    )


def test_the_enforced_posture_is_refused_behind_an_undeclared_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enforced posture is refused behind an undeclared proxy.

    Not because of the JWKS fetch, but because of carriers with no derivable destination:
    `kg/git_writer._git_child_env` deliberately keeps proxy variables in the `git` child's
    environment, so a push and its credential go through the proxy. The git destination is not
    derived for the allowlist, and the compiled interposer exempts loopback, so nothing below this
    sees a loopback sidecar.
    """
    settings = _entra_settings()
    for proxy in ("http://sidecar.internal:15001", "http://127.0.0.1:15001"):
        _proxy_env(monkeypatch, HTTPS_PROXY=proxy)
        assert netguard.proxied_destinations(settings) == {}, (
            "the premise: nothing is charged here, so this refusal is the ambient arm rather than "
            "the JWKS row having come back"
        )
        assert _refuses(settings), f"the enforced posture booted with {proxy} carrying its git push"


def test_the_ambient_arm_is_the_enforced_posture_and_not_a_proxy_ban(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ambient arm is the enforced posture, not a proxy ban: every way out works.

    `egress_allow` naming the proxy declares the mesh intended; `NO_PROXY=*` makes `git` dial
    directly; and with identity off (dev checkout, `make chat`, CI) an ambient corporate proxy is
    expected and nothing is refused, re-run here on the enforced fixture so the gate itself is
    measured.
    """
    proxy = "http://sidecar.internal:15001"
    _proxy_env(monkeypatch, HTTPS_PROXY=proxy)
    assert not _refuses(_entra_settings(egress_allow="sidecar.internal"))
    assert not _refuses(_entra_settings(entra_required=False))
    _proxy_env(monkeypatch, HTTPS_PROXY=proxy, NO_PROXY="*")
    assert not _refuses(_entra_settings())
    _proxy_env(monkeypatch)
    assert not _refuses(_entra_settings()), "no proxy at all must stay the silent case"
    _proxy_env(monkeypatch, HTTPS_PROXY=proxy)
    assert _refuses(_entra_settings()), (
        "the positive control did not fire, so the four negative arms above prove nothing about "
        "whether the enforced posture is checked at all"
    )


def test_a_proxy_named_in_the_allowlist_is_the_operators_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy named in `egress_allow` is the operator's decision and proceeds.

    Naming the sidecar is what distinguishes intent from an env var someone else set; loopback
    carries no such signal, so it earns no exemption.
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
    """A declared proxy does not hide an undeclared one reaching the same host by another reader.

    `proxied_destinations` is keyed per destination, reader and variable. A collision needs two
    readers, and this deployment ships one, so the pair is injected and the real function driven.
    """
    monkeypatch.setattr(
        netguard,
        "_env_reading_destinations",
        lambda _settings: [
            ("https://shared.internal:4317", "the OTLP span exporter", ("grpc_proxy",)),
            ("https://shared.internal/tenant/keys", "a second reader", ("https_proxy",)),
        ],
    )
    _proxy_env(
        monkeypatch,
        GRPC_PROXY="http://undeclared.corp:3128",
        HTTPS_PROXY="http://declared.corp:3128",
    )
    settings = _proxy_settings(egress_allow="gateway.internal,declared.corp")
    carried = netguard.proxied_destinations(settings)
    assert len(carried) == 2, f"one reader's entry was overwritten by the other's: {carried}"
    proxies = {proxy for proxy, _ in carried.values()}
    assert proxies == {"undeclared.corp", "declared.corp"}, (
        f"both readers' proxies must survive into the comparison, got {proxies}"
    )

    # The invariant is about the refusal, not the dict: `refuse_proxied_egress` filters to
    # undeclared proxies, so with one declared and one not, the refusal must fire and name the
    # undeclared one.
    with pytest.raises(RuntimeError, match="SECURITY: a proxy is configured") as refusal:
        netguard.refuse_proxied_egress(settings)
    assert "undeclared.corp" in str(refusal.value)
    assert "declared.corp" not in str(refusal.value).replace("undeclared.corp", ""), (
        "the declared proxy is the operator's decision and must not be reported as the offender"
    )

    # The control that makes the raise above mean something: declare both and it goes silent, so
    # what fired was the `undeclared` filter rather than "two entries exist".
    both = _proxy_settings(egress_allow="gateway.internal,declared.corp,undeclared.corp")
    netguard.refuse_proxied_egress(both)


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
    """The refusal names the proxy, the destination, what reads it, and the edit that proceeds.

    Only the bare host is accepted in `egress_allow`; `proxy.corp:3128` and `http://proxy.corp:3128`
    still refuse.
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
    """`arm_from_settings` is where the refusal is wired.

    It is the single call `chemclaw.core.config` makes, which puts the refusal in front of every
    process, not only the front door.
    """
    _proxy_env(monkeypatch, HTTPS_PROXY="http://127.0.0.1:15001")
    with pytest.raises(RuntimeError, match="SECURITY: a proxy is configured"):
        netguard.arm_from_settings(_proxy_settings())


def test_refusing_the_proxy_does_not_surrender_the_environment_trust_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refusing the proxy does not surrender the environment trust store.

    `trust_env=False` also stops httpx reading `SSL_CERT_FILE`/`SSL_CERT_DIR`, which would silently
    swap a site's trust store for `certifi`. `create_default_context(cafile=None)` falls through to
    OpenSSL's default paths, which honour both variables.
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

    Issued for `127.0.0.1`, because the guard refuses `getaddrinfo` for non-allowlisted names; the
    certificates differ only in issuer, which is what the handshake decides on.
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

    A handshake rather than `SSLContext.get_ca_certs()`, which does not report a `capath` at all.
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
    """An ambient `SSL_CERT_DIR` cannot widen a configured CA pin.

    httpx's precedence is exclusive (`SSL_CERT_FILE`, else `SSL_CERT_DIR`, else certifi); making
    `cafile` conditional but `capath` unconditional would add ambient roots to a pinned client.
    Driven through a real handshake, since `get_ca_certs()` is blind to `capath`.
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
    """A configured bundle is the whole store; certifi is not added to it.

    A pin that also trusts certifi is not a pin. The bundle is not certifi itself, so ignoring
    `ca_bundle` fails here.
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
    """`SSL_CERT_FILE` wins over `SSL_CERT_DIR` exclusively, as httpx reads them.

    Asserted against `httpx.Client(trust_env=True)` on the same environment, through a handshake.
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


#: The module-level request verbs. Each builds a throwaway `Client` internally and takes the same
#: `trust_env`, defaulting to True — so `httpx.get(url)` is a client construction wearing a
#: different name, and a scan that matched only the class names could not see one.
_HTTPX_VERBS = (
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "request",
    "stream",
)


def _httpx_module_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """This module's aliases for `httpx`, and its names imported from `httpx`.

    `Client` is matched by bare name anywhere; verbs like `get` are matched only as
    `<httpx alias>.<verb>` or as a name imported from `httpx`.
    """
    aliases: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "httpx"}
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            imported |= {a.asname or a.name for a in node.names if a.name in _HTTPX_VERBS}
    return aliases, imported


def _httpx_client_constructions() -> list[tuple[str, int, bool]]:
    """Every httpx client construction in `src/`, and whether it refuses the environment.

    Verbs (`httpx.get(...)`) build a `Client` per call with `trust_env` defaulting to True, so they
    are scanned like the classes. Compliant forms: `trust_env=False`; `**gateway_client_kwargs(...)`
    inline or via a local bound to it (that mapping sets `trust_env=False` unconditionally); and an
    `http_client=` delegate, which is itself checked by this function. Bare `**kwargs` does not
    count.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    found: list[tuple[str, int, bool]] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        aliases, imported = _httpx_module_names(tree)
        bound = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "gateway_client_kwargs"
            for target in node.targets
            if isinstance(target, ast.Name)
        }

        def refuses_the_environment(call: ast.Call, bound: set[str] = bound) -> bool:
            """Whether this client construction cannot read a proxy variable."""
            for keyword in call.keywords:
                if (
                    keyword.arg == "trust_env"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                ):
                    return True
                if keyword.arg == "http_client" and isinstance(keyword.value, ast.Call):
                    return refuses_the_environment(keyword.value)
                if keyword.arg is None and (
                    (
                        isinstance(keyword.value, ast.Call)
                        and getattr(keyword.value.func, "id", "") == "gateway_client_kwargs"
                    )
                    or (isinstance(keyword.value, ast.Name) and keyword.value.id in bound)
                ):
                    return True
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            qualified = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in aliases
            ) or (isinstance(func, ast.Name) and func.id in imported)
            if name not in ("Client", "AsyncClient") and not (name in _HTTPX_VERBS and qualified):
                continue
            found.append(
                (
                    path.relative_to(src.parents[1]).as_posix(),
                    node.lineno,
                    refuses_the_environment(node),
                )
            )
    return found


def test_every_served_http_client_refuses_the_ambient_proxy() -> None:
    """Every HTTP client constructed in `src/` refuses the ambient proxy.

    `_env_reading_destinations` charges only destinations whose clients read the environment, which
    is correct only while every first-party client passes `trust_env=False`; httpx defaults it to
    True, so the property decays by omission. There is no exemption list; despite its name the scan
    covers all of `src/`. `gateway_client_kwargs` is asserted against the live function before the
    scan accepts unpackings of it.
    """
    assert gateway_client_kwargs("").get("trust_env") is False, (
        "`gateway_client_kwargs` is the exemption the scan below grants by name; if it stops "
        "refusing the environment, every client built from it reads a proxy variable again."
    )
    offenders = sorted(
        f"{module}:{line}" for module, line, refuses in _httpx_client_constructions() if not refuses
    )
    assert not offenders, (
        f"{offenders} build an httpx client without `trust_env=False`. A proxy variable on the pod "
        "would carry that traffic to a host of the setter's choosing — past the egress guard, "
        "which sees only the dial to the proxy, and past a NetworkPolicy when the proxy is a "
        "loopback sidecar. Pass `trust_env=False`, or `**gateway_client_kwargs()`."
    )


def test_a_loopback_name_does_not_seed_the_resolved_ip_allowlist() -> None:
    """A loopback name does not seed the resolved-IP allowlist.

    `localhost` passes `_check` by name, so recording its resolved addresses would let a second A
    record turn into a permanent, port-independent allowlist entry. Loopback IPs are already exempt,
    so `_resolved_ips` needs only addresses an allowlisted name resolved to.
    """
    saved = set(netguard._resolved_ips)
    try:
        netguard._reset_for_tests(["allowed.example"])
        netguard._resolved_ips.clear()
        socket.getaddrinfo("localhost", 0)
        assert netguard._resolved_ips == set(), (
            "resolving a host that is trusted by name, not by allowlist, seeded `_resolved_ips`"
        )
        # The allowlist branch still records, because that is what makes the guard work at all:
        # a legitimate call resolves an allowed name and then connects to one of the IPs it got.
        netguard._reset_for_tests(["localhost"])
        socket.getaddrinfo("localhost", 0)
        assert netguard._resolved_ips, "an allowlisted name must still record what it resolved to"
    finally:
        netguard._resolved_ips.clear()
        netguard._resolved_ips.update(saved)
        netguard._reset_for_tests(netguard.derive_allowed(settings))


# --- the git note remote, the one destination that is a *name* rather than a field --------------

#: The shipped bound on `ssh -G`, read off `Settings` so these tests exercise the default a
#: deployment runs with rather than a second copy of it.
_SSH_TIMEOUT: float = Settings.model_fields["egress_ssh_resolve_timeout_seconds"].default


def _clone_with_remote(tmp_path: Path, url: str, *, name: str = "origin", push: str = "") -> str:
    """A real checkout with a real remote, so the resolution is driven rather than mocked."""
    repo = tmp_path / f"notes-{abs(hash((url, name, push))) % 10**8}"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", name, url], check=True, capture_output=True
    )
    if push:
        subprocess.run(
            ["git", "-C", str(repo), "config", "--add", f"remote.{name}.pushurl", push],
            check=True,
            capture_output=True,
        )
    return str(repo)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://git.example.com/org/notes.git", {"git.example.com"}),
        ("git@git.example.com:org/notes.git", {"git.example.com"}),
        ("ssh://git@git.example.com:2222/org/notes.git", {"git.example.com"}),
        ("https://GIT.EXAMPLE.COM/org/notes.git", {"git.example.com"}),
        ("ssh://git@[2001:db8::1]:2222/org/notes.git", {"2001:db8::1"}),
        # No credential reaches the allowlist, which is a host list and not a connection string.
        ("https://user:pw@creds.example.com/org/notes.git", {"creds.example.com"}),
        # Every spelling of "nowhere off this box". `_host_from_url` reads `../notes` as the host
        # `..`, which is why the path forms are refused before it rather than after.
        ("/srv/notes.git", set()),
        ("file:///srv/notes.git", set()),
        ("../notes", set()),
        ("~/notes", set()),
        ("notes", set()),
    ],
)
def test_the_git_note_remote_resolves_to_the_hosts_it_would_push_to(
    tmp_path: Path, url: str, expected: set[str]
) -> None:
    """Driven against real `git remote add`, because the scp-like form is the one that surprises.

    `git@host:path` has no scheme, so anything reading it as a URL sees no host unless it is asked
    the right way; `/srv/notes.git` and `../notes` have no host at all and must not contribute one.
    """
    netguard._push_hosts.cache_clear()
    assert (
        netguard._push_hosts_for(_clone_with_remote(tmp_path, url), "origin", _SSH_TIMEOUT)
        == expected
    )


def test_the_push_url_is_what_is_derived_when_it_differs_from_the_fetch_url(
    tmp_path: Path,
) -> None:
    """The push URL is derived when it differs from the fetch URL.

    `git push` uses `remote.<name>.pushurl`, and plain `get-url` returns the fetch URL; `--push
    --all` derives every push URL.
    """
    netguard._push_hosts.cache_clear()
    one = _clone_with_remote(
        tmp_path, "https://fetch.example.com/o/n.git", push="https://push.example.com/o/n.git"
    )
    assert netguard._push_hosts_for(one, "origin", _SSH_TIMEOUT) == {"push.example.com"}, (
        "the fetch host was derived, so the guard would refuse the push it is meant to permit"
    )
    netguard._push_hosts.cache_clear()
    several = _clone_with_remote(
        tmp_path, "https://fetch.example.com/o/n.git", push="https://p1.example.com/o/n.git"
    )
    subprocess.run(
        [
            "git",
            "-C",
            several,
            "config",
            "--add",
            "remote.origin.pushurl",
            "https://p2.example.com/o/n.git",
        ],
        check=True,
        capture_output=True,
    )
    assert netguard._push_hosts_for(several, "origin", _SSH_TIMEOUT) == {
        "p1.example.com",
        "p2.example.com",
    }


def test_a_remote_url_git_accepts_and_urlsplit_refuses_does_not_crash_the_process(
    tmp_path: Path,
) -> None:
    """A remote URL git accepts and `urlsplit` refuses does not crash the process.

    `derive_allowed` runs at config import, so a `ValueError` from an unbalanced `[` would be a
    crashloop.
    """
    netguard._push_hosts.cache_clear()
    for url in ("https://[oops/path", "ssh://[2001:db8::1/x"):
        repo = _clone_with_remote(tmp_path, url)
        netguard._push_hosts.cache_clear()
        assert netguard._push_hosts_for(repo, "origin", _SSH_TIMEOUT) == set()


def test_a_derived_host_can_never_carry_the_compiled_layers_separator(tmp_path: Path) -> None:
    """A derived host can never contain a comma, the compiled layer's separator.

    `core/netguard_preload.c::parse_allowlist` splits on commas while `_check` compares whole
    strings, so such an entry would be allowed by one layer and refused by the other.
    """
    netguard._push_hosts.cache_clear()
    repo = _clone_with_remote(tmp_path, "https://harmless,target.example.com/n.git")
    assert netguard._push_hosts_for(repo, "origin", _SSH_TIMEOUT) == set()
    netguard._push_hosts.cache_clear()
    multiline = _clone_with_remote(tmp_path, "https://evil.example.com\nhttps://good.example.com/x")
    assert not any(
        "," in host for host in netguard._push_hosts_for(multiline, "origin", _SSH_TIMEOUT)
    )


@pytest.mark.parametrize("spelling", [".", "./", "././", "src/..", "{cwd}", "{cwd}/"])
def test_no_spelling_of_this_processes_own_checkout_derives_a_git_host(spelling: str) -> None:
    """No spelling of this process's own checkout derives a git host.

    The writer refuses to commit into the running application's own tree, so there is no destination
    to allow. `core/checkout.py` is the one predicate both sides ask.
    """
    netguard._push_hosts.cache_clear()
    assert (
        netguard._push_hosts_for(spelling.format(cwd=os.getcwd()), "origin", _SSH_TIMEOUT) == set()
    )


def test_the_derivation_and_the_writers_refusal_ask_the_same_question(tmp_path: Path) -> None:
    """The derivation and the writer's refusal ask the same question.

    `git_writer._require_dedicated_checkout` raises for exactly the directories
    `is_the_processes_own_checkout` reports.
    """
    from chemclaw.core.checkout import is_the_processes_own_checkout
    from chemclaw.kg.git_writer import GitWriteError, _require_dedicated_checkout

    for spelling in (".", "./", "src/..", os.getcwd(), str(tmp_path)):
        refused = False
        try:
            _require_dedicated_checkout(spelling)
        except GitWriteError:
            refused = True
        assert refused == is_the_processes_own_checkout(spelling), spelling


def test_an_unresolvable_remote_contributes_nothing_rather_than_failing(tmp_path: Path) -> None:
    """An unresolvable remote contributes nothing rather than failing.

    A missing directory, a non-checkout, or a missing remote must not raise at import; a deployment
    can still name the host in `egress_allow`.
    """
    netguard._push_hosts.cache_clear()
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    assert netguard._push_hosts_for(str(plain), "origin", _SSH_TIMEOUT) == set()
    assert (
        netguard._push_hosts_for(str(tmp_path / "does-not-exist"), "origin", _SSH_TIMEOUT) == set()
    )
    repo = _clone_with_remote(tmp_path, "https://git.example.com/org/notes.git", name="upstream")
    assert netguard._push_hosts_for(repo, "origin", _SSH_TIMEOUT) == set()
    assert netguard._push_hosts_for(repo, "upstream", _SSH_TIMEOUT) == {"git.example.com"}


def test_a_remote_named_like_a_flag_is_a_remote(tmp_path: Path) -> None:
    """A remote named like a flag is a remote: `--` precedes the name.

    Without the separator, `get-url --upload-pack=...` is parsed as an option, and a future git
    accepting it would execute a string from a config file.
    """
    netguard._push_hosts.cache_clear()
    repo = _clone_with_remote(tmp_path, "https://git.example.com/o/n.git")
    subprocess.run(
        ["git", "-C", repo, "config", "--add", "remote.--upload-pack=id.url", "https://x.test/n"],
        check=True,
        capture_output=True,
    )
    without = subprocess.run(
        ["git", "-C", repo, "remote", "get-url", "--push", "--all", "--upload-pack=id"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "unknown option" in (without.stderr + without.stdout).lower(), (
        "git no longer reads this as an option without the separator, so what `--` protects has "
        "changed; re-read it rather than deleting it"
    )
    # With it, the same string is a remote *name* and resolves to that remote's URL — which is the
    # point: a `.git/config` decides where a push goes, never what git is asked to run.
    assert netguard._push_hosts_for(repo, "--upload-pack=id", _SSH_TIMEOUT) == {"x.test"}


def test_the_cache_key_is_the_resolved_directory_and_not_the_string(tmp_path: Path) -> None:
    """The cache key is the resolved directory, not the string.

    A relative path under two working directories names two clones, so resolution happens before the
    cache.
    """
    netguard._push_hosts.cache_clear()
    first, second = tmp_path / "a", tmp_path / "b"
    for where, host in ((first, "alpha.example.com"), (second, "beta.example.com")):
        where.mkdir()
        subprocess.run(
            ["git", "-C", str(where), "init", "-q", "notes"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(where / "notes"), "remote", "add", "origin", f"https://{host}/n.git"],
            check=True,
            capture_output=True,
        )
    was = os.getcwd()
    try:
        os.chdir(first)
        assert netguard._push_hosts_for("notes", "origin", _SSH_TIMEOUT) == {"alpha.example.com"}
        os.chdir(second)
        assert netguard._push_hosts_for("notes", "origin", _SSH_TIMEOUT) == {"beta.example.com"}
    finally:
        os.chdir(was)


def test_the_derived_allowlist_carries_the_git_note_remote(tmp_path: Path) -> None:
    """End to end: the host reaches `derive_allowed`, which is what both guard layers arm from.

    The compiled `LD_PRELOAD` layer reads this same set through `cli/egress_preload.py`, so a host
    added anywhere else would be permitted by one layer and refused by the other.
    """
    netguard._push_hosts.cache_clear()

    class _S:
        llm_base_url = "https://llm.internal.example:8000/v1"
        llm_fallback_base_url = ""
        postgres_dsn = "postgresql://u:p@pg.internal:5432/db"
        temporal_address = "temporal.internal:7233"
        calc_server_url = ""
        rxnlabel_server_url = ""
        connector_urls: dict[str, str] = {}
        egress_allow = ""
        egress_ssh_resolve_timeout_seconds = _SSH_TIMEOUT
        note_repo_dir = _clone_with_remote(tmp_path, "git@notes.example.com:org/knowledge.git")
        git_remote = "origin"

    assert "notes.example.com" in netguard.derive_allowed(_S())


def _fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Put an `ssh` first on `PATH` that logs its argv to the returned file, then runs `body`.

    The real client reads the user's configuration from their home directory, which a test cannot
    redirect. The stand-in reproduces the `hostname <host>` line `_ssh_hostname` reads, as a real
    `ssh -G` prints it.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "ssh-calls.log"
    script = bin_dir / "ssh"
    script.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n{body}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return calls


_ALIAS_CONFIG = """case "$3" in
  notes-alias) echo "user git"; echo "hostname real-git.internal.example"; echo "port 22" ;;
  *) echo "hostname $3" ;;
esac"""


@pytest.mark.parametrize(
    "url",
    [
        "git@notes-alias:org/notes.git",
        "ssh://git@notes-alias:2222/org/notes.git",
        "git+ssh://git@notes-alias/org/notes.git",
    ],
)
def test_an_ssh_alias_derives_the_host_ssh_dials_not_the_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """An ssh alias derives the host ssh dials, not the alias.

    Otherwise both guard layers refuse the deployment's own push. The argv is asserted too: `--`
    keeps a host spelled like an option from becoming one.
    """
    calls = _fake_ssh(tmp_path, monkeypatch, _ALIAS_CONFIG)
    netguard._push_hosts.cache_clear()
    repo = _clone_with_remote(tmp_path, url)
    assert netguard._push_hosts_for(repo, "origin", _SSH_TIMEOUT) == {"real-git.internal.example"}
    assert calls.read_text().split() == ["-G", "--", "notes-alias"]


def test_an_https_remote_never_asks_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The host of an `https://` URL is the host dialled, so no second subprocess is spent on it.

    This runs at config import in every process; an https deployment pays for the `git` call and
    nothing more.
    """
    calls = _fake_ssh(tmp_path, monkeypatch, 'echo "hostname elsewhere.example"')
    netguard._push_hosts.cache_clear()
    repo = _clone_with_remote(tmp_path, "https://git.example.com/org/notes.git")
    assert netguard._push_hosts_for(repo, "origin", _SSH_TIMEOUT) == {"git.example.com"}
    assert not calls.exists(), "ssh was asked about an https remote"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("exit 255", "ssh refused its configuration"),
        (
            'echo "hostname harmless,target.example"',
            "a host carrying the compiled layer's separator",
        ),
        ('echo "port 22"', "no hostname line at all"),
    ],
)
def test_an_ssh_that_cannot_answer_leaves_the_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, why: str
) -> None:
    """Every ssh failure falls back to the alias, never to nothing.

    The alias is a correctable wrong entry; an exception is a crashloop at config import, and an
    empty set would drop a right entry when the host is not an alias.
    """
    calls = _fake_ssh(tmp_path, monkeypatch, body)
    netguard._push_hosts.cache_clear()
    repo = _clone_with_remote(tmp_path, "git@notes-alias:org/notes.git")
    assert netguard._push_hosts_for(repo, "origin", _SSH_TIMEOUT) == {"notes-alias"}, why
    assert calls.exists(), "the fallback was reached without ssh ever being asked"


def test_an_ssh_that_hangs_is_bounded_by_the_configured_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hanging `ssh -G` is bounded by the configured timeout.

    The stand-in `exec`s into a long sleep, so `subprocess.run`'s kill reaches it. Returning well
    under the sleep proves the bound fired; the call log proves ssh was reached.
    """
    calls = _fake_ssh(tmp_path, monkeypatch, "exec sleep 30")
    netguard._push_hosts.cache_clear()
    repo = _clone_with_remote(tmp_path, "git@notes-alias:org/notes.git")
    started = time.monotonic()
    assert netguard._push_hosts_for(repo, "origin", 0.5) == {"notes-alias"}
    assert time.monotonic() - started < 10, "the timeout did not bound the ssh child"
    assert calls.exists()


def test_no_ssh_on_the_path_leaves_the_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ssh on `PATH` leaves the alias.

    `PATH` holds only a `git` symlink, so `subprocess` raises `OSError`, which must not escape. No
    `shutil.which` probe, since `tests/test_deploy_chart.py` reads those as skip guards.
    """
    git = shutil.which("git")
    assert git is not None
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(git)
    netguard._push_hosts.cache_clear()
    repo = _clone_with_remote(tmp_path, "git@notes-alias:org/notes.git")
    monkeypatch.setenv("PATH", str(only_git))
    assert netguard._push_hosts_for(repo, "origin", _SSH_TIMEOUT) == {"notes-alias"}


def test_the_derived_allowlist_carries_the_host_an_ssh_alias_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through `derive_allowed`, with the timeout read off the settings object."""
    _fake_ssh(tmp_path, monkeypatch, _ALIAS_CONFIG)
    netguard._push_hosts.cache_clear()
    live = Settings(
        _env_file=None,  # type: ignore[call-arg]
        note_repo_dir=_clone_with_remote(tmp_path, "git@notes-alias:org/notes.git"),
    )
    allowed = netguard.derive_allowed(live)
    assert "real-git.internal.example" in allowed
    assert "notes-alias" not in allowed


def test_the_git_remote_is_a_destination_no_field_suffix_would_have_found() -> None:
    """The git remote is a destination no `Settings` suffix would find, hence its own derivation.

    Asserted against the live setting name, not a string literal, so a change to `Settings` can
    falsify it.
    """
    from chemclaw.core.config import settings as live

    name = next(n for n in type(live).model_fields if n == "git_remote")
    assert not _DESTINATION_FIELD.search(name), (
        "git_remote now has a destination-shaped name, so the derived field guard reaches it and "
        "this special case can go"
    )
    assert "://" not in live.git_remote and ":" not in live.git_remote, (
        f"git_remote now holds {live.git_remote!r}, which names a host; derive it like the others"
    )
