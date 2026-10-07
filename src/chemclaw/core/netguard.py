"""The in-process egress guard: refuse an outbound call to a host not on the derived allowlist.

Nothing may leave the estate except traffic to the configured LLM gateway and the declared
infrastructure (Postgres, Temporal, MCP connector endpoints, the identity provider) plus whatever a
deployment names in `egress_allow`. The allowlist is derived from the same settings the process
dials with, so moving an endpoint updates both at once. A refusal is logged at ERROR and counted,
because `EgressForbidden` subclasses `OSError` and libraries routinely swallow those.

Layers:
- This module patches Python's `socket` entry points. It cannot see child processes, `ctypes` calls,
  compiled extensions (gRPC's C-core, Temporal's Rust core) or `_socket.socket`.
- `core/netguard_preload.c`, an `LD_PRELOAD` interposer on libc's network calls (that file is the
  declaration of which ones), armed by `deploy/entrypoint.sh` from the allowlist this module
  derives, covers compiled code and inherited child processes (so a remote git host must be
  allowlisted). It reports `chemclaw_egress_preload_armed` separately.
- Statically linked binaries and raw syscalls are left to the NetworkPolicy.

A proxy moves the destination out of the address, and a loopback sidecar proxy is invisible to the
allowlist, the interposer (loopback is exempt) and the NetworkPolicy. `refuse_proxied_egress`
handles that at boot with two arms: a proxy variable that would carry a named destination dialled by
an environment-reading client (`_env_reading_destinations`), and, under `entra_required` (the
enforced posture), any undeclared ambient proxy, since carriers such as the `git` child name no
derivable destination. A developer checkout behind a corporate proxy must still import, hence the
gate. Every first-party `httpx` client passes `trust_env=False`;
`tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy` enforces it.

Armed once at `chemclaw.core.config` import, the one import every entrypoint makes, so the guard is
a property of the system rather than of a launcher.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from chemclaw.core.checkout import is_the_processes_own_checkout
from chemclaw.core.http import is_loopback_host

logger = logging.getLogger(__name__)

_armed = False
_allowed: frozenset[str] = frozenset()
_refused = 0
# IPs that an allowlisted hostname resolved to, recorded by the patched `getaddrinfo`, so the
# following `connect` to one of them is permitted. An IP literal never resolved from an allowed name
# is refused. Small and stable: the deployment's own infrastructure addresses.
_resolved_ips: set[str] = set()


class EgressForbidden(OSError):
    """An outbound call to a host outside the allowlist. Subclasses `OSError` deliberately.

    Libraries that catch `OSError` will treat a refusal as an unreachable host, so the ERROR log and
    the counter are what make a refusal auditable.
    """


def _host_of(address: Any) -> str | None:
    """The host string from a socket address, or None when there is nothing that leaves the host.

    An internet address is a tuple; anything else (an `AF_UNIX` path as `str` or `bytes`) is local
    IPC and returns None, matching `netguard_preload.c`, which checks only `AF_INET`/`AF_INET6`.
    Refusing it would break local IPC such as `multiprocessing`'s forkserver. A `bytes` host inside
    a tuple is decoded so it cannot bypass a `str`-only check.

    Args:
        address: Whatever the caller handed `connect`/`sendto`.

    Returns:
        The hostname or IP an internet address names, or None when the address names nothing
        outside this host.
    """
    if not isinstance(address, (tuple, list)) or not address:
        return None
    host: Any = address[0]
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return None
    return host if isinstance(host, str) else None


def _check(address: Any) -> None:
    """Raise `EgressForbidden` unless `address` is loopback, an allowlisted host, or a resolved IP.

    `connect` usually receives an IP, so the allowlist is consulted together with `_resolved_ips`; a
    hostname passed directly is checked against the allowlist. "Loopback" is
    `core.http.is_loopback_host` and nothing else, so the unspecified address and `""` need an
    allowlist entry like any other destination.
    """
    host = _host_of(address)
    if (
        host is None
        or is_loopback_host(host)
        or host.strip("[]").lower() in _allowed
        or host.strip("[]") in _resolved_ips
    ):
        return
    global _refused
    _refused += 1
    logger.error("egress refused: outbound connection to %r is not on the allowlist", host)
    _record_refusal(host)
    raise EgressForbidden(
        f"outbound connection to {host!r} refused: it is not the LLM gateway, declared "
        "infrastructure, or a host named in CHEMCLAW_EGRESS_ALLOW"
    )


def _record_refusal(host: str) -> None:
    """Count the refusal on `chemclaw_egress_refused_total` if metrics are wired.

    Lazy and best-effort: the guard arms at config import, possibly before the registry exists, and
    a refusal must never fail on a counter. The host is not a label (unbounded); the counter is
    bare.
    """
    try:
        from chemclaw.core.metrics_bridge import record_metric

        record_metric(lambda metrics: metrics.increment("chemclaw_egress_refused_total"))
    except Exception:
        pass


def _host_from_url(value: str) -> str | None:
    """The hostname from a URL or a bare `host:port`, lowercased, or None if there is none."""
    value = value.strip()
    if not value:
        return None
    parsed = urlsplit(value if "://" in value else f"//{value}")
    return parsed.hostname.lower() if parsed.hostname else None


def _host_from_dsn(dsn: str) -> str | None:
    """The host of a libpq/SQLAlchemy-style DSN, or None (an empty or `host=`-keyword DSN)."""
    host = _host_from_url(dsn)
    if host:
        return host
    for part in dsn.split():
        if part.startswith("host="):
            return part[5:].strip().lower() or None
    return None


#: Bound on `git remote get-url`, which reads `.git/config` and opens no socket: this guards against
#: a wedged filesystem, not the network.
_GIT_REMOTE_TIMEOUT_SECONDS = 5.0

#: What a hostname may contain: letters, digits, dots, hyphens, underscores and the colons of an
#: unbracketed IPv6 literal. Anything else is junk from a malformed remote URL, a comma above all:
#: `netguard_preload.c::parse_allowlist` splits on commas, so one entry would become two hosts on
#: the compiled layer and diverge from this one.
_A_PLAUSIBLE_HOST = re.compile(r"\A[A-Za-z0-9._:-]+\Z")


def _push_hosts_for(repo_dir: str, remote: str, ssh_timeout_seconds: float) -> frozenset[str]:
    """Resolve the checkout, then ask `_push_hosts` — which caches on what it is given.

    The cache key must be unambiguous, and a relative `repo_dir` names different directories under
    different working directories. `ssh_timeout_seconds` is passed in so this module never reads the
    config that is still importing it.
    """
    if not repo_dir or not remote or is_the_processes_own_checkout(repo_dir):
        return frozenset()
    try:
        resolved = str(Path(repo_dir).resolve())
    except OSError:
        return frozenset()
    return _push_hosts(resolved, remote, ssh_timeout_seconds)


#: The URL schemes git hands to ssh. The scp-like `user@host:path` has no scheme and is ssh too.
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


def _ssh_hostname(alias: str, timeout_seconds: float) -> str:
    """The host ssh would dial for `alias`, per `ssh -G`, or `alias` itself if ssh cannot say.

    An ssh remote's URL host may be an alias (`Host`/`HostName` in ssh config), so ssh itself is
    asked which host it would dial, without connecting. Never raises: any failure falls back to the
    alias, which a deployment can still override in `egress_allow`. `--` stops a host being read as
    an option, and stdin is closed so nothing waits on a prompt.
    """
    try:
        found = subprocess.run(
            ["ssh", "-G", "--", alias],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return alias
    if found.returncode != 0:
        return alias
    for line in found.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        if key.lower() == "hostname":
            host = value.strip().lower()
            return host if _A_PLAUSIBLE_HOST.match(host) else alias
    return alias


@lru_cache(maxsize=8)
def _push_hosts(repo_dir: str, remote: str, ssh_timeout_seconds: float) -> frozenset[str]:
    """The hosts `kg/git_writer.py` would push notes to, or empty if it would push nowhere.

    `repo_dir` is already resolved (see `_push_hosts_for`); cached because arming happens once per
    process. The git remote is a name (`"origin"`), not a settings field, so it is resolved here
    from the checkout. Both guard layers arm from this one derivation, so a push host cannot be
    permitted by one layer and refused by the other.

    Uses `get-url --push --all`, because `git push` honours `pushurl`, `pushInsteadOf` and multiple
    push URLs. Nothing is derived when the checkout is this process's own (`core/checkout.py`; the
    writer refuses to push from it). Local-path remotes and every failure contribute nothing, since
    an absent entry only refuses a push a deployment can still allowlist, while a wrong one opens a
    host. Nothing here may raise: this runs at config import in every process. An ssh remote is
    resolved once more through `_ssh_hostname`.
    """
    try:
        # A fixed argv, no shell, and `--` before the remote name so a remote called `-x` is a
        # remote and not a flag.
        found = subprocess.run(
            ["git", "-C", repo_dir, "remote", "get-url", "--push", "--all", "--", remote],
            capture_output=True,
            text=True,
            timeout=_GIT_REMOTE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if found.returncode != 0:
        return frozenset()
    hosts: set[str] = set()
    for line in found.stdout.splitlines():
        url = line.strip()
        # A path is not a destination. `_host_from_url` reads `../notes` as the host `..`, and
        # `file://` and an absolute path have no host at all.
        if not url or url.startswith(("file://", "/", ".", "~")):
            continue
        if "://" not in url and ":" not in url:
            continue  # a bare relative path, e.g. `notes`
        try:
            host = _host_from_url(url)
        except ValueError:
            continue  # git accepts `https://[oops/path`; `urlsplit` does not
        if not host or not _A_PLAUSIBLE_HOST.match(host):
            continue
        scheme = url.split("://", 1)[0].lower() if "://" in url else "ssh"
        if scheme in _SSH_SCHEMES:
            host = _ssh_hostname(host, ssh_timeout_seconds)
        hosts.add(host)
    return frozenset(hosts)


def derive_allowed(settings: Any) -> frozenset[str]:
    """Build the allowlist from the destinations this deployment actually dials.

    Read off the settings object so the allowlist moves with the dial. Loopback needs no entry for
    the guard, but dev defaults still appear in the result (bare `Settings()` yields `{'127.0.0.1',
    'localhost'}`). `tests/test_netguard.py` gives every destination-named `Settings` field a
    sentinel and asserts it arrives here or is declared someone else's socket.

    The result is also the compiled layer's allowlist. `temporal_address` is added unconditionally
    (every component dials it); `otel_endpoint` only under `otel_enabled`, together with the
    standard `OTEL_EXPORTER_OTLP_*` variables the exporter would actually use. Hosts supplied only
    by manifests (warehouse connections, sinks, delivery channels, external vector stores) are not
    on this object and must be named in `egress_allow`.
    """
    hosts: set[str] = set()

    def add(value: str | None, *, dsn: bool = False) -> None:
        host = _host_from_dsn(value or "") if dsn else _host_from_url(value or "")
        if host:
            hosts.add(host)

    # The two model destinations, and no third: no vendor host is ever added, so prompts can only go
    # to the configured gateway.
    add(settings.llm_base_url)
    add(settings.llm_fallback_base_url)
    add(settings.postgres_dsn, dsn=True)
    if getattr(settings, "postgres_migration_dsn", ""):
        add(settings.postgres_migration_dsn, dsn=True)
    # The split session database, empty when it is the same server as `postgres_dsn`. A missing
    # entry here is an outage, reported by psycopg as a connection failure with nothing naming
    # egress.
    if getattr(settings, "session_store_dsn", ""):
        add(settings.session_store_dsn, dsn=True)
    add(settings.temporal_address)
    add(settings.calc_server_url)
    add(settings.rxnlabel_server_url)
    for url in getattr(settings, "connector_urls", {}).values():
        add(url)
    if getattr(settings, "entra_required", False):
        add(getattr(settings, "entra_jwks_endpoint", "") or settings.entra_jwks_url)
    if getattr(settings, "otel_enabled", False):
        # Every spelling the exporter would resolve, most specific first, since `core/logging.py`
        # only `setdefault`s the bridge and OTel prefers the per-signal variable.
        add(settings.otel_endpoint)
        add(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
        add(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if getattr(settings, "vector_store_provider", "pgvector") != "pgvector":
        add(settings.vector_store_url)
    # The git note remote, a name rather than a destination; see `_push_hosts`.
    hosts |= _push_hosts_for(
        str(getattr(settings, "note_repo_dir", "") or ""),
        str(getattr(settings, "git_remote", "") or ""),
        settings.egress_ssh_resolve_timeout_seconds,
    )
    for extra in (settings.egress_allow or "").split(","):
        host = extra.strip().lower()
        if host:
            hosts.add(host)
    return frozenset(hosts)


def arm(allowed: Iterable[str] = ()) -> None:
    """Patch the socket entry points so a call to a non-allowlisted host raises `EgressForbidden`.

    Idempotent, so a re-import cannot double-wrap. Covers `connect`, the forward resolvers
    (`getaddrinfo`/`gethostbyname[_ex]`, since DNS is a round trip of its own), datagram sends
    (`sendto`/`sendmsg`) and the reverse resolvers (`getnameinfo`/`gethostbyaddr`). `bind`/`listen`/
    `accept` are left alone so servers still serve.
    """
    global _armed, _allowed
    _allowed = frozenset(allowed)
    if _armed:
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_sendmsg = socket.socket.sendmsg
    original_getaddrinfo = socket.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex
    original_getnameinfo = socket.getnameinfo
    original_gethostbyaddr = socket.gethostbyaddr

    def connect(self: socket.socket, address: Any) -> None:
        _check(address)
        return original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return original_connect_ex(self, address)

    def sendto(self: socket.socket, *args: Any) -> int:
        _check(args[-1] if args else None)
        return int(original_sendto(self, *args))

    def sendmsg(self: socket.socket, *args: Any, **kwargs: Any) -> int:
        address = args[-1] if len(args) >= 4 else kwargs.get("address")
        _check(address)
        return int(original_sendmsg(self, *args, **kwargs))

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        _check((host, port))
        results = original_getaddrinfo(host, port, *args, **kwargs)
        # Record resolved IPs only for an allowlisted name. A loopback name passes `_check` by name
        # without resolution, and recording its addresses would make any extra record on that name a
        # standing, port-independent allowlist entry.
        name = _host_of((host, port))
        if name is not None and name.strip("[]").lower() in _allowed:
            for entry in results:
                sockaddr = entry[4] if len(entry) > 4 else None
                if isinstance(sockaddr, tuple) and sockaddr and isinstance(sockaddr[0], str):
                    _resolved_ips.add(sockaddr[0])
        return results

    def gethostbyname(hostname: Any) -> Any:
        _check((hostname, 0))
        return original_gethostbyname(hostname)

    def gethostbyname_ex(hostname: Any) -> Any:
        _check((hostname, 0))
        return original_gethostbyname_ex(hostname)

    def getnameinfo(sockaddr: Any, flags: Any) -> Any:
        _check(sockaddr)
        return original_getnameinfo(sockaddr, flags)

    def gethostbyaddr(ip_address: Any) -> Any:
        _check((ip_address, 0))
        return original_gethostbyaddr(ip_address)

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    socket.socket.sendto = sendto  # type: ignore[method-assign,assignment]
    socket.socket.sendmsg = sendmsg  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.getnameinfo = getnameinfo
    socket.gethostbyaddr = gethostbyaddr
    _armed = True


# The proxy variables consumers read, lower case winning. `grpc_proxy` is read first by grpc, which
# then falls back to the others.
_PROXY_VARIABLES = ("all_proxy", "grpc_proxy", "http_proxy", "https_proxy")


def _proxy_value(name: str) -> str:
    """One proxy variable's value, lower case winning — as httpx, requests, git and grpc read it.

    Not `urllib.request.getproxies_environment`: it drops `http` when `REQUEST_METHOD` is set (a CGI
    defence), which git and grpc do not do, so the refusal would go silent while a child process is
    still proxied. Its case handling is reproduced: any spelling matches, exact lower case
    preferred.
    """
    if os.environ.get(name, "").strip():
        return os.environ[name].strip()
    # Every spelling, not just `.upper()`: a mixed-case `Grpc_Proxy` still configures grpc.
    for key, value in os.environ.items():
        if key.lower() == name and value.strip():
            return value.strip()
    return ""


def _env_reading_destinations(settings: Any) -> list[tuple[str, str, tuple[str, ...]]]:
    """The destinations whose clients actually read a proxy variable, as (url, reader, variables).

    Not `derive_allowed`: every first-party client passes `trust_env=False`, so charging their
    destinations would refuse pods for an untrue reason (and break a developer checkout behind a
    corporate proxy). Today one destination qualifies: the OTLP gRPC span exporter, since grpc reads
    `grpc_proxy`, `https_proxy` and `http_proxy` regardless of target scheme, and with sensitive
    data on that traffic carries prompts and completions. A destination that becomes immune must
    leave this list, because it feeds a refusal. The `git` push host has no setting to key a row on;
    it is covered by `ambient_proxies` under the enforced posture.
    """
    destinations: list[tuple[str, str, tuple[str, ...]]] = []
    if getattr(settings, "otel_enabled", False) and settings.otel_endpoint:
        # Every variable, and not by scheme: grpc's resolution ignores the target's.
        destinations.append(
            (
                settings.otel_endpoint,
                "the OTLP span exporter",
                ("grpc_proxy", "https_proxy", "http_proxy", "all_proxy"),
            )
        )
    return destinations


def ambient_proxies() -> dict[str, str]:
    """Every proxy variable set in this environment, as variable name -> proxy host.

    For carriers with no derivable destination (the `git` child keeps every proxy variable, and
    dependencies may build their own clients), so it takes no settings. `no_proxy` is honoured only
    as `*`: there is no host to ask a per-host bypass about, and `*` is what CPython and git
    short-circuit on. Read through `_proxy_value` for one set of case rules.
    """
    if _proxy_value("no_proxy") == "*":
        return {}
    found: dict[str, str] = {}
    for variable in _PROXY_VARIABLES:
        host = _host_from_url(_proxy_value(variable))
        if host:
            found[variable.upper()] = host
    return found


def proxied_destinations(settings: Any) -> dict[str, tuple[str, str]]:
    """Destination host -> (proxy host, what reads the environment for it).

    Keyed by destination and holding both facts because the message names all three, and so two
    variables naming different proxies for one host cannot overwrite each other into a false pass.
    """
    from urllib.request import proxy_bypass

    carried: dict[str, tuple[str, str]] = {}
    for url, reader, variables in _env_reading_destinations(settings):
        # A bare `host:port` is a real OTLP endpoint spelling and `urlsplit` reads its host as the
        # *scheme*, so the netloc is taken the way `_host_from_url` already takes it.
        host = _host_from_url(url)
        if not host or proxy_bypass(host):
            continue
        for variable in variables:
            proxy_host = _host_from_url(_proxy_value(variable))
            if proxy_host:
                carried[f"{host} ({reader}, via {variable.upper()})"] = (proxy_host, reader)
                break
    return carried


def refuse_proxied_egress(settings: Any) -> None:
    """Refuse to start when a proxy variable would carry this process's traffic off-address.

    A proxied client dials the proxy and names the real destination in the request line, so the
    allowlist cannot see it; a loopback sidecar proxy also escapes the interposer and NetworkPolicy.

    The first arm charges destinations dialled by environment-reading clients
    (`_env_reading_destinations`) whose proxy is not bypassed by `NO_PROXY` or named in
    `egress_allow`. The second arm, under `entra_required` only, refuses any undeclared ambient
    proxy (`ambient_proxies`), since the `git` child and dependency clients name no derivable
    destination. The arms raise separately because only the first can offer `NO_PROXY` as a remedy;
    both messages open with the same string.

    Raises:
        RuntimeError: naming the proxy, what would carry it, and the edit that proceeds — plus
            the destination and the reader where there is one. Raised at boot, from the
            `chemclaw.core.config` import every process makes.
    """
    declared = {
        entry.strip().lower() for entry in (settings.egress_allow or "").split(",") if entry.strip()
    }
    undeclared = {
        destination: proxy
        for destination, (proxy, _) in proxied_destinations(settings).items()
        if proxy not in declared
    }
    if undeclared:
        proxies = ", ".join(sorted(set(undeclared.values())))
        destinations = "; ".join(sorted(undeclared))
        raise RuntimeError(
            f"SECURITY: a proxy is configured in this process's environment ({proxies}) and would "
            f"carry traffic to {destinations} — that traffic would reach a host this deployment "
            "has not declared, and the egress guard cannot see it because it sees only the dial to "
            "the proxy. To proceed, add the proxy to CHEMCLAW_EGRESS_ALLOW as a bare host (no "
            "scheme, no port) to say this is intended, add these destinations to NO_PROXY, or "
            "unset the variable."
        )
    if not getattr(settings, "entra_required", False):
        return
    ambient = {
        variable: proxy for variable, proxy in ambient_proxies().items() if proxy not in declared
    }
    if not ambient:
        return
    proxies = ", ".join(sorted(set(ambient.values())))
    variables = ", ".join(sorted(ambient))
    raise RuntimeError(
        f"SECURITY: a proxy is configured in this process's environment ({proxies}, via "
        f"{variables}) and entra_required=true — the deployment that believes it is in the "
        "enforced posture. This process has carriers that read the environment and reach hosts no "
        "setting names: the `git push` kg/git_writer.py shells out to, whose child environment "
        "deliberately keeps every proxy variable, and any dependency that builds its own HTTP "
        "client. That traffic would leave with no layer able to observe it — the egress guard sees "
        "only the dial to the proxy, a loopback sidecar shares the pod's network namespace so the "
        "NetworkPolicy never sees it, and the LD_PRELOAD interposer exempts loopback by "
        "construction. To proceed, add the proxy to CHEMCLAW_EGRESS_ALLOW as a bare host (no "
        "scheme, no port) to say this is intended, set NO_PROXY=* to take every carrier off it, or "
        "unset the variable."
    )


def arm_from_settings(settings: Any) -> None:
    """Derive the allowlist from `settings` and arm, unless the guard is disabled.

    The one call `chemclaw.core.config` makes. `egress_guard_enabled=False` leaves the process
    unguarded (relying on the NetworkPolicy) and skips the proxy refusal too. The proxy refusal runs
    before `arm`, because a proxied call looks like a legitimate dial the running guard cannot
    fault.
    """
    if not settings.egress_guard_enabled:
        logger.warning(
            "egress guard disabled (CHEMCLAW_EGRESS_GUARD_ENABLED=false) — outbound calls are "
            "bounded only by the NetworkPolicy, not by this process"
        )
        return
    refuse_proxied_egress(settings)
    arm(derive_allowed(settings))
    _publish_armed()


def _publish_armed() -> None:
    """Bind the `chemclaw_egress_guard_armed` gauge to the live armed state, best-effort.

    Bound to a source so a scrape reflects the real state; best-effort because arming may precede
    the registry.
    """
    try:
        from chemclaw.core.metrics import METRICS

        METRICS.bind_gauge("chemclaw_egress_guard_armed", lambda: 1.0 if _armed else 0.0)
    except Exception:
        pass


def _reset_for_tests(allowed: Iterable[str] = ()) -> None:
    """Re-derive the allowlist without re-patching. Tests only — the patch itself is idempotent.

    `_check` is a connect-time hook, so a pooled connection opened while a host was allowed survives
    its removal. Unreachable in production, where the allowlist is fixed at import; a config-reload
    feature would have to close pools too.
    """
    global _allowed
    _allowed = frozenset(allowed)
    _push_hosts.cache_clear()
