"""The in-process egress guard: refuse an outbound call to a host not on the derived allowlist.

The invariant this defends is that nothing leaves the estate except LLM traffic through the
configured gateway and the declared infrastructure this system dials — Postgres, Temporal, the MCP
connector endpoints, the identity provider, and whatever a deployment names in `egress_allow`. The
sibling `Chemclaw3-mcp` fleet already carries a *deny-all* runtime guard (`mcp_server_kit.egress`);
this core process legitimately dials several destinations, so this one is an **allowlist** derived
from config at arm time rather than a fixed deny.

**Why derived, not typed.** The allowlist is built from the same settings the process actually uses
to dial (`llm_base_url`, `postgres_dsn`, `temporal_address`, the connector URLs, …), so it cannot
drift from what a legitimate call needs — adding a connector or moving the LLM endpoint updates the
allowlist by the same edit that updates the dial. A host outside it is refused, logged at ERROR with
the host, and counted; the log and the counter are load-bearing rather than decorative, because
`EgressForbidden` subclasses `OSError` — the family libraries silently retry on — so a refusal that
only surfaced as a connection error would be swallowed by the first `except OSError` in the stack
(the LLM failover, `publish/drivers/http`, `connectors/health` all have one).

**What it cannot cover, stated rather than implied.** A patched `socket` in this interpreter says
nothing about a **child process** (the KG PR-gate shells out to `git`), a **`ctypes` call into
libc**, or a syscall from a **compiled extension** (this closure carries `grpcio`, `rdkit`, torch,
`psycopg_binary`). Those are the NetworkPolicy's job — the layer that takes the network away rather
than asking Python nicely — and the chart's `git_remote` / egress rules are where they land. This
guard catches the large class a static import scan cannot: a dependency reaching out at runtime.

**One shape has no such backstop, and it is why `refuse_proxied_egress` exists.** A proxy moves the
destination out of the address, so the allowlist cannot see it; and where the proxy is a sidecar on
loopback — the shipped OpenShift shape — it shares the pod's network namespace, so the NetworkPolicy
cannot see it either. That case is refused at boot rather than at dial, which is also what makes it
reach the child process the paragraph above concedes: `git` inherits the environment, and a
configured proxy is now a thing this process refuses to start beside.

Armed once, at `chemclaw.core.config` import, beside `pin_langsmith_egress`, because that module is
the one import every entrypoint makes (the front door, the CLI, the connector server, the durable
worker). Arming it there makes the guard a property of the system rather than of a launcher — the
failure mode this repository has already recorded twice (the Helm-only LangSmith pin; the Helm-only
LLM provider).
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from chemclaw.core.http import is_loopback_host

logger = logging.getLogger(__name__)

_armed = False
_allowed: frozenset[str] = frozenset()
_refused = 0
# IPs that an allowlisted *hostname* resolved to, recorded by the patched `getaddrinfo`. This is
# what makes an allowlist guard work at the `connect` layer: a legitimate call resolves an allowed
# name (permitted, and the resulting IPs land here) and then connects to one of those IPs (permitted
# because it is here). A `connect` to an IP literal that was never resolved from an allowed name is
# still refused, and a blocked name never reaches `connect` because its `getaddrinfo` was refused
# first. Unbounded growth is a non-issue: the set is the deployment's own small, stable set of
# gateway/infra addresses, and it lives for the life of the process like the allowlist itself.
_resolved_ips: set[str] = set()


class EgressForbidden(OSError):
    """An outbound call to a host outside the allowlist. Subclasses `OSError` deliberately.

    A library that catches `OSError` and retries or degrades will treat a refusal as an unreachable
    host, which is why the ERROR log and the counter — not the exception alone — are what make a
    refusal auditable.
    """


def _host_of(address: Any) -> str | None:
    """The host string from a socket address, or None when there is nothing that leaves the host.

    Returns None for a family the check cannot read (AF_UNIX is a path, not a host) so `_check`
    treats it as "nothing to leave for" rather than refusing local IPC. A `bytes` host is decoded,
    because a `bytes` host in the address tuple walked past a `str`-only check in pure Python.
    """
    host: Any
    if isinstance(address, (tuple, list)) and address:
        host = address[0]
    else:
        host = address
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return None
    return host if isinstance(host, str) else None


def _check(address: Any) -> None:
    """Raise `EgressForbidden` unless `address` is loopback, an allowlisted host, or a resolved IP.

    `connect` almost always receives an *IP*, not a name (the caller resolved it first), so the
    allowlist — which holds hostnames — is consulted together with `_resolved_ips`, the IPs that an
    allowlisted name resolved to through the patched `getaddrinfo`. A name that reaches here
    directly (some clients pass a hostname to `connect`) is checked against the allowlist.

    **"Loopback" is `core.http.is_loopback_host` and nothing else.** This module carried its own
    parsed copy beside the front door's three-string set, and the two disagreed on `127.0.0.2` and
    on `0.0.0.0` — enough that a pod bound non-loopback and pointed at a `127.0.0.2` gateway walked
    past the boot check written to catch it. The local copy additionally exempted the *unspecified*
    address, which the shared one deliberately does not (as a bind it is every interface), so
    `0.0.0.0` and `""` now need an allowlist entry like any other destination. Measured before the
    change: nothing dials them — there is no `0.0.0.0` URL in the tree, and asyncio,
    `socket.create_server`, `http.server` and `socketserver` all bind an unspecified host without
    the resolver seeing it.
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

    Imported lazily and best-effort: the guard arms at config import, before the metrics registry is
    necessarily built, and a refusal must never fail because a counter was not ready. The host is
    *not* a label (it is caller-influenced and unclampable — an unbounded series); the counter is
    bare, exactly as the fleet's `chemclaw_mcp_egress_refused_total` is.
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


def derive_allowed(settings: Any) -> frozenset[str]:
    """Build the allowlist from the destinations this deployment actually dials.

    Every entry is a host this process has a configured, legitimate reason to reach. Reading them
    off the settings object rather than a static list is what keeps the allowlist in step with the
    dial: a moved LLM endpoint or a new connector updates both at once. Loopback needs no entry
    (`core.http.is_loopback_host` covers it), so the dev defaults (Postgres/Temporal/calc on
    localhost) add nothing here.

    **The walk below is hand-written and the coverage is not.** "It cannot drift" was a claim about
    this list, and two settings had already drifted out of it; `tests/test_netguard.py` now gives
    every `Settings` field whose name ends in a destination word a sentinel host and asserts each
    one arrives here or is named there as somebody else's socket, so the next such field fails on
    the day it is declared rather than in a deployment that split its session store.

    A *manifest*-supplied host — a warehouse ELN's `connection:`, a result sink's, a delivery
    channel's, an external vector store reached through `module:callable` — is still not derived
    from anything, because it is not on this object at all: those blocks are the deployment's own
    file. Such a destination has to be named in `egress_allow`, and that is a real limit rather
    than an oversight, written down here because the docstring above used to read as though nothing
    needed naming.
    """
    hosts: set[str] = set()

    def add(value: str | None, *, dsn: bool = False) -> None:
        host = _host_from_dsn(value or "") if dsn else _host_from_url(value or "")
        if host:
            hosts.add(host)

    # The two model destinations, and there is no third: with the provider concept gone
    # (`D-2026-09-04-a-gateway-is-the-only-provider`) no vendor host is ever added here. A branch
    # used to put `api.anthropic.com` on the allowlist whenever `llm_provider == "anthropic"` —
    # which was the shipped default — so the guard that exists to bound where prompts can go was
    # opening the exact destination the exfiltration path used.
    add(settings.llm_base_url)
    add(settings.llm_fallback_base_url)
    add(settings.postgres_dsn, dsn=True)
    if getattr(settings, "postgres_migration_dsn", ""):
        add(settings.postgres_migration_dsn, dsn=True)
    # The split session database, empty when it is the same server as `postgres_dsn`. Missing here
    # until 2026-09-04, and the shape of that omission is worth keeping in mind for the next
    # destination: an allowlist gap is not a hole, it is an *outage* — a deployment that follows the
    # chart's own `sessionStoreDsn` secret had every durable-session write refused by its own
    # process, as an `OSError` psycopg reports as a connection failure with nothing naming egress.
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
        add(settings.otel_endpoint)
    if getattr(settings, "vector_store_provider", "pgvector") != "pgvector":
        add(settings.vector_store_url)
    for extra in (settings.egress_allow or "").split(","):
        host = extra.strip().lower()
        if host:
            hosts.add(host)
    return frozenset(hosts)


def arm(allowed: Iterable[str] = ()) -> None:
    """Patch the socket entry points so a call to a non-allowlisted host raises `EgressForbidden`.

    Idempotent — arming twice is a no-op, so a re-import cannot double-wrap. The same seven-plus-two
    entry points the sibling guard covers, for the same measured reasons: DNS is a round trip in its
    own right (`getaddrinfo`/`gethostbyname[_ex]`), a datagram socket never calls `connect`
    (`sendto`/`sendmsg`), and the reverse-lookup family (`getnameinfo`/`gethostbyaddr`) is the same
    resolver round trip with the address as the covert channel. `bind`/`listen`/`accept` are left
    alone so the front door and the worker HTTP surface still serve.
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
        # Record the IPs an allowed name resolved to, so the subsequent `connect` to one of them is
        # permitted. Only when the *name* was not itself an IP literal (an IP literal resolving to
        # itself is already covered by the allowlist / loopback check).
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


def armed() -> bool:
    """Whether the guard is installed. For the readiness gauge and tests."""
    return _armed


def allowed_hosts() -> frozenset[str]:
    """The current allowlist. For tests and diagnostics."""
    return _allowed


_PROXY_ENV_VARS = ("all_proxy", "http_proxy", "https_proxy")


def refuse_proxied_egress(settings: Any) -> None:
    """Refuse to start when a proxy variable would carry this process's traffic off-address.

    **A proxy moves the destination out of the address, which is the one thing an allowlist guard
    cannot see.** Everything below `arm()` asks "which *host* may this process dial"; a client
    configured with a proxy dials the *proxy* and names the real destination in the request line.
    Measured with the allowlist empty and a local recorder standing in for a sidecar: a request to
    an external host through `proxy=http://127.0.0.1:<port>` returned HTTP 200 with the body, and
    `_refused` never moved — while the same request with a *named* proxy, and the same request with
    no proxy at all, were both refused at `getaddrinfo`. The loopback arm needs no allowlisting,
    because `_check` exempts loopback by construction and must keep exempting it: this process
    dials Postgres, Temporal and the calc backend there.

    That is not an attacker-only shape. An OpenShift service mesh or egress sidecar *is* a loopback
    proxy by design, and it is the layer that would otherwise be the backstop: a sidecar shares the
    pod's network namespace, so traffic to it never crosses a NetworkPolicy enforcement point. For
    this one shape the module docstring's "that is the NetworkPolicy's job" does not hold, and
    nothing below the guard catches it.

    So a proxy is treated as a **destination**: if one is configured and its host is not named in
    `CHEMCLAW_EGRESS_ALLOW`, this process does not start. Named explicitly rather than accepted for
    being loopback, because loopback is exactly the case that needs the operator's signature.

    **`NO_PROXY` is honoured, and that is what keeps this from being a nuisance.** The question is
    not "is a proxy set" but "would a proxy carry traffic to somewhere this deployment actually
    dials", so the destinations are `derive_allowed(settings)` and the bypass test is the stdlib's
    own (`urllib.request.proxy_bypass`) rather than a second reading of `no_proxy`
    written here. A sandbox whose `no_proxy` covers the loopback addresses the dev defaults use —
    this repository's CI containers, among others — configures a proxy and is not refused, because
    no call this process makes would go through it.

    Silent when the guard is disabled, because `arm_from_settings` returns before reaching this: a
    deployment that has opted out of the guard has opted out of this too, and says so at WARNING.

    Raises:
        RuntimeError: naming the proxy, the destinations it would carry, and the one way to
            proceed. Loud at boot rather than loud on the first turn, and unlike
            `api/middleware._refuse_unconfigured_llm_gateway` it reaches the durable worker too,
            because it hangs off the `chemclaw.core.config` import every entrypoint makes.
    """
    from urllib.request import proxy_bypass

    configured = {
        value.strip()
        for name in _PROXY_ENV_VARS
        for value in (os.environ.get(name, ""), os.environ.get(name.upper(), ""))
        if value.strip()
    }
    if not configured:
        return
    proxy_hosts = {host for host in (_host_from_url(url) for url in configured) if host}
    declared = {
        entry.strip().lower() for entry in (settings.egress_allow or "").split(",") if entry.strip()
    }
    undeclared = sorted(proxy_hosts - declared)
    if not undeclared:
        return
    carried = sorted(host for host in derive_allowed(settings) if not proxy_bypass(host))
    if not carried:
        return
    raise RuntimeError(
        "SECURITY: a proxy is configured in this process's environment "
        f"({', '.join(undeclared)}) and would carry traffic to {', '.join(carried)} — every "
        "prompt, completion and Authorization bearer would be re-terminated at a host this "
        "deployment has not declared, past the CA pinning and invisibly to the egress guard, "
        "which sees only the dial to the proxy. Name the proxy in CHEMCLAW_EGRESS_ALLOW to say "
        "this is intended, add these destinations to NO_PROXY, or unset the proxy variable."
    )


def arm_from_settings(settings: Any) -> None:
    """Derive the allowlist from `settings` and arm, unless the guard is disabled.

    The one call `chemclaw.core.config` makes. When `egress_guard_enabled` is False the guard is not
    installed and the process runs unguarded — the stated opt-out for a deployment relying on the
    NetworkPolicy alone, and `refuse_proxied_egress` is skipped with it.

    The proxy refusal runs **before** `arm`, because it is the one failure a running guard cannot
    report: a proxied call is a legitimate-looking dial to an allowlisted or loopback address, so
    arming first would mean starting a process whose guard is structurally blind to where its
    prompts go.
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

    Bound to a source rather than set to a value so a scrape always reflects the real state (the
    registry's gauges are live sources, `metrics.bind_gauge`). Best-effort for the same reason as
    `_record_refusal`: arming happens at config import, possibly before the registry is built.
    """
    try:
        from chemclaw.core.metrics import METRICS

        METRICS.bind_gauge("chemclaw_egress_guard_armed", lambda: 1.0 if _armed else 0.0)
    except Exception:
        pass


def _reset_for_tests(allowed: Iterable[str] = ()) -> None:
    """Re-derive the allowlist without re-patching. Tests only — the patch itself is idempotent."""
    global _allowed
    _allowed = frozenset(allowed)
