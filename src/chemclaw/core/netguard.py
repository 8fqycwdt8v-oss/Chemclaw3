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

**What *this* layer cannot cover, stated rather than implied.** A patched `socket` in this
interpreter says nothing about a **child process** (`kg/git_writer.py` shells out to `git`), a
**`ctypes` call into libc**, a syscall from a **compiled extension** (this closure carries `grpcio`,
`rdkit`, torch, `psycopg_binary`), or **`_socket.socket`** — the C base class `socket.socket`
subclasses, whose `connect` is not assignable and is two lines of ordinary Python away. This guard
catches the large class a static import scan cannot: a dependency reaching out at runtime.

**Two of this deployment's own destinations were in the compiled-extension class, and they are why
there is a second layer.** gRPC's C-core and Temporal's Rust sdk-core open sockets without touching
`socket.socket` or the module resolvers, so `otel_endpoint` and `temporal_address` — both of which
`derive_allowed` adds — used to be allowlist entries rather than enforcement. Measured with the
allowlist deliberately empty and no proxy set: a `grpc.insecure_channel`, the OTLP gRPC span
exporter and `temporalio.Client.connect` all reached an external listener with `_refused` at 0,
seven connections against one refusal for the pure-Python control. With
`otel_include_sensitive_data` on, that exporter carries prompts and completions, so the blind path
was also the highest-value one.

`core/netguard_preload.c` closes it
(`D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py`): an `LD_PRELOAD` interposition on
libc's `connect`, `getaddrinfo`, `sendto` and `sendmsg`, armed by `deploy/entrypoint.sh` from the
allowlist **this** module derives, so there is one derivation and two enforcement points. Driven
against a real gRPC server over a non-loopback route, all three of the clients above are refused —
grpc reporting `connect failed: ... error: Operation not permitted` from its own C-core — while the
same dials succeed with no interposer and succeed on loopback with it. It reaches the child process
and the `ctypes` call in the paragraph above as well, because a child inherits `LD_PRELOAD`: a
deployment that pushes notes to a **remote** git host must now name that host in `egress_allow`,
which is the first time that destination has been bounded at all. `chemclaw_egress_preload_armed`
is its own series and deliberately not this layer's gauge — `chemclaw_egress_guard_armed` reporting
1 over an open compiled path is what made the finding serious. What neither layer covers is a
**statically linked** binary or one issuing the syscall directly; that is the NetworkPolicy's job,
the layer that takes the network away rather than asking libc nicely.

**One shape has no such backstop, and it is why `refuse_proxied_egress` exists.** A proxy moves the
destination out of the address, so the allowlist cannot see it; and where the proxy is a sidecar on
loopback — the shipped OpenShift shape — it shares the pod's network namespace, so the NetworkPolicy
cannot see it either. That case is refused at boot rather than at dial, which is also what lets it
reach the child process the paragraph above concedes: `git` inherits the environment.

**What that refusal is and is not, stated narrowly because two tellings of it were not.** It fires
when a proxy variable is set *and* would carry a destination this process reaches through something
that **reads the environment** *and* that destination is not bypassed by `NO_PROXY` *and* the
proxy's host is not named in `egress_allow`. It is not "a configured proxy refuses the process".
The narrowing to env-reading clients is the whole correctness argument and lives on
`_env_reading_destinations`: charging every destination instead refused pods over hosts a proxy
could not carry — every HTTP client on a *served* path passes `trust_env=False` — while the two
that genuinely are proxied went uncharged, and on the shipped loopback defaults it stopped a
developer behind a corporate proxy from importing this module at all.

**That clause is a control now rather than a claim, and the word "served" is no longer doing any
work in it.** It was written here as "every first-party HTTP client", and measured it was false for
eight constructions in the live/eval lane, none on a path a chemist reaches, one carrying a bearer;
so it was narrowed to *served* and those four modules were held in a named exemption list.
`D-2026-09-12-an-ambient-proxy-is-a-destination-nobody-declared` closed them and deleted the list,
including the Entra JWKS fetch, which PyJWT makes through `urlopen` and which no `trust_env`
reaches. `tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy` walks
every `httpx.Client`/`AsyncClient` construction in `src/` with **no** exemption, so a new client
anywhere fails on the day it is written — which the list could not do for a client added inside one
of the four files it named. `httpx` defaults `trust_env` to True, which makes this a property that
decays by omission — the one kind a docstring cannot hold.

**And the boot refusal has two arms, because charging a destination stopped being able to carry
it.** The first is `_env_reading_destinations`: a *named* destination on this settings object,
dialled by something that reads the environment, refused with the destination, the reader and the
variable in the message. That arm covers less than two backlog rows used to say — on this
repository's own defaults (`entra_required=false`, `otel_enabled=false`) it charges nothing, and
measured with a real loopback proxy a plain `httpx.get` to an external host returned 200 with
`_refused` at 0 before and after, the proxy's log showing the absolute-URI request line.

**The second arm exists because the first one emptied out for a whole class of deployment.** The
JWKS fetch was the only destination `entra_required` implied, and `_HttpxJwkClient`'s
`trust_env=False` made it immune, so the row had to go — leaving an `entra_required=true` +
`otel_enabled=false` process charging **nothing**, measured: `charged: []`, boot proceeds, where
the same settings refused the day before. That is not a configuration with nothing to carry. This
process has carriers that read the environment and name **no destination this module can derive**,
and the load-bearing one is measured rather than argued: `kg/git_writer._git_child_env` deliberately
keeps every proxy variable in the `git` child's environment (verified — `HTTPS_PROXY` survives it
while `CHEMCLAW_LLM_API_KEY` is scrubbed), and `git ls-remote` behind a loopback recorder standing
in for a sidecar sent it `CONNECT notes.example.invalid:443`. The `git` destination is explicitly
*not* charged above (its host is `"origin"`, not a URL on this object) and the `LD_PRELOAD`
interposer exempts loopback by construction, so for a loopback sidecar there is no layer left. So
under `entra_required` an **undeclared ambient proxy is itself the refusable condition**, with no
destination needed — `refuse_proxied_egress`'s second arm.

**Why that gate and not no gate:** `entra_required` is this repository's existing signal for "the
deployment that believes it is in the enforced posture" (`publish/drivers/http.py`,
`publish/drivers/postgres.py`, the broker-TLS and DSN-`sslmode` refusals in `core/config`), and a
developer's checkout behind a corporate proxy must still import — which is a measured requirement
here, not a courtesy. So what is uncovered is `make chat`, `make connectors`, CI, a hand-started
worker **with identity off**; a hand-started worker in the enforced posture is now covered, which
is what the sentence this replaces got wrong. It claimed the shipped Helm chart's
`CHEMCLAW_ENTRA_REQUIRED: "true"` was why the refusal fired in the OpenShift topology the sidecar
argument is about. It was not: `entra_required` charged nothing, and the only thing still firing in
that topology was the chart's unrelated `CHEMCLAW_OTEL_ENABLED: "true"`. A causal claim about a
control, resting on a value that is not the control's input, is the shape this repository keeps
finding — and it shipped in the same commit that made it false.

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
import subprocess
from collections.abc import Iterable
from functools import lru_cache
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

    **An internet address is a tuple, and everything else is local IPC.** That is the whole rule,
    and the previous version of this function claimed it while doing the opposite: it fell through
    to `host = address` for a non-tuple, so an `AF_UNIX` address — which is a bare `str` path, or
    `bytes` in the abstract namespace — arrived at `_check` as a hostname, failed `is_loopback_host`
    and was refused. The docstring said "Returns None for a family the check cannot read (AF_UNIX is
    a path, not a host) so `_check` treats it as 'nothing to leave for' rather than refusing local
    IPC", and nothing asserted it in either direction.

    Measured: `multiprocessing`'s forkserver — which `ingest/documents/isolate.py` needs to run a
    parse in a killable child — connects to its own listener at `/tmp/pymp-*/listener-*`, and every
    such connect was refused with "outbound connection to '/tmp/pymp-…/listener-…' is not on the
    allowlist". A path under `/tmp` leaves this host by no route, so refusing it protected nothing
    and broke local process IPC.

    `netguard_preload.c` — the same control one layer down — had it right all along: it reads
    `sa_family` and checks only `AF_INET`/`AF_INET6`, and its comment states the same reason. The
    two layers disagreed, and the C one was the correct half.

    A `bytes` host inside a tuple is still decoded, because a `bytes` host in an address tuple
    walked past a `str`-only check in pure Python.

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


#: How long `git remote get-url` may take. It reads `.git/config` and opens no socket, so this is a
#: bound on a wedged filesystem rather than on the network; a process that cannot answer it in five
#: seconds has a problem this allowlist is not going to fix.
_GIT_REMOTE_TIMEOUT_SECONDS = 5.0


@lru_cache(maxsize=8)
def _git_remote_host(repo_dir: str, remote: str) -> str | None:
    """The host `kg/git_writer.py` would push notes to, or `None` if it would push nowhere.

    **This is the one destination that is not on the settings object.** `git_remote` is the string
    `"origin"` — a name, resolved inside the checkout — so every other entry in `derive_allowed`
    can be read off a field and this one cannot. It went from unbounded to bounded when
    `D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py` armed the compiled layer: a
    child process inherits `LD_PRELOAD`, so `git push` is now refused like any other dial, and a
    deployment that pushes notes off-box had to name its git host in `egress_allow` by hand.

    **Resolved here rather than in `cli/egress_preload.py`**, although that is where the subprocess
    would be cheapest. The two layers arm from *one* derivation — the compiled guard reads what
    this function returns and the in-process guard patches `socket` with it — and a host added on
    one side only would be a destination one layer refuses and the other permits, which is the
    defect this family keeps finding.

    **Only when a deployment has moved `note_repo_dir` off its default.** At `"."` the writer
    refuses the write before it can push (`git_writer._require_dedicated_checkout`: committing into
    the running application's own tree and pushing to the source repository), so there is no
    destination to allow — and deriving one anyway would put the *source* repository's host on the
    allowlist of every dev checkout, which is a widening for a push that cannot happen. A single
    comparison rather than importing that predicate, because `core/` may not import `kg/`.

    A local-path remote is not a destination and returns `None`; so does any failure — no git, no
    checkout, no such remote. That direction is deliberate: an absent entry refuses a push that a
    deployment can still name in `egress_allow`, while a wrong entry opens a host nobody declared.
    """
    if not repo_dir or repo_dir == "." or not remote:
        return None
    try:
        # A fixed argv, no shell, and `--` before the remote name so a remote called `-x` is a
        # remote and not a flag.
        found = subprocess.run(
            ["git", "-C", repo_dir, "remote", "get-url", "--", remote],
            capture_output=True,
            text=True,
            timeout=_GIT_REMOTE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode != 0:
        return None
    url = found.stdout.strip()
    # A path is not a destination. `_host_from_url` reads `../notes` as the host `..`, which would
    # put a nonsense entry on the allowlist, and `file://` and an absolute path have no host at all.
    if not url or url.startswith(("file://", "/", ".", "~")):
        return None
    if "://" not in url and ":" not in url:
        return None  # a bare relative path, e.g. `notes`
    return _host_from_url(url)


def derive_allowed(settings: Any) -> frozenset[str]:
    """Build the allowlist from the destinations this deployment actually dials.

    Every entry is a host this process has a configured, legitimate reason to reach. Reading them
    off the settings object rather than a static list is what keeps the allowlist in step with the
    dial: a moved LLM endpoint or a new connector updates both at once. Loopback needs no entry
    *for the guard* (`core.http.is_loopback_host` covers it), so the dev defaults add nothing the
    allowlist check consults — but they are in the returned set all the same: measured on bare
    `Settings()`, this returns `{'127.0.0.1', 'localhost'}`. That distinction went from harmless to
    load-bearing when `refuse_proxied_egress` arrived, since a reader who took "add nothing here"
    literally would expect an empty set to reason from.

    **The walk below is hand-written and the coverage is not.** "It cannot drift" was a claim about
    this list, and two settings had already drifted out of it; `tests/test_netguard.py` now gives
    every `Settings` field whose name ends in a destination word a sentinel host and asserts each
    one arrives here or is named there as somebody else's socket, so the next such field fails on
    the day it is declared rather than in a deployment that split its session store.

    **Two entries here used to be bookkeeping rather than bounds, and now they are bounds.**
    `temporal_address` and `otel_endpoint` are dialled by Temporal's Rust sdk-core and grpc's
    C-core, neither of which goes through the patched `socket.socket` or the patched resolvers —
    measured, both reached an off-allowlist host with `_refused` at 0. `core/netguard_preload.c`
    enforces them at libc and reads *this* set, so what this function returns is now the
    allowlist of both layers rather than a description one of them ignores.

    **Which is why the asymmetry below is deliberate rather than an oversight.**
    `temporal_address` is added unconditionally because every component dials it; `otel_endpoint`
    only under `otel_enabled`, because with tracing off nothing dials it and an entry would be a
    permission for a destination no client opens. Under enforcement a conditional entry is the
    *correct* shape — it tracks whether the dial exists — and the unconditional one would be the
    defect if Temporal were optional.

    **What the same reasoning then exposed: `otel_endpoint` is not the only spelling of that
    destination.** `core/logging.py` bridges it into `OTEL_EXPORTER_OTLP_ENDPOINT` with
    `setdefault`, so a deployment that sets the standard variable (or the per-signal
    `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) directly keeps winning — and the exporter then dials a
    host this function never saw. Harmless while nothing enforced it; with the compiled layer armed
    it is an exporter refused by its own deployment, reported as an `UNAVAILABLE` the exporter
    swallows. So the standard variables are read here too, in the exporter's own precedence order.

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
        # Every spelling the exporter would resolve, most specific first, because `core/logging.py`
        # only `setdefault`s the bridge and OTel's own precedence prefers the per-signal variable.
        # A deployment that configures the collector the standard way is configuring a destination
        # this object does not carry, and the compiled layer refuses what is not here.
        add(settings.otel_endpoint)
        add(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
        add(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if getattr(settings, "vector_store_provider", "pgvector") != "pgvector":
        add(settings.vector_store_url)
    # The git note remote, which is a *name* on this object rather than a destination — see
    # `_git_remote_host` for why it is resolved here and not in the entrypoint that runs the
    # subprocess for the compiled layer.
    remote_host = _git_remote_host(
        str(getattr(settings, "note_repo_dir", "") or ""),
        str(getattr(settings, "git_remote", "") or ""),
    )
    if remote_host:
        hosts.add(remote_host)
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
        # Record the IPs an **allowlisted** name resolved to, so the subsequent `connect` to one of
        # them is permitted. Only the allowlist branch, and that qualifier is the fix rather than a
        # restatement: `_check` has three ways to pass, and one of them — `is_loopback_host` — is
        # answered *by name*, without resolving anything. So `localhost` used to deposit whatever
        # it resolved to into a set `_check` then trusted permanently and port-independently, with
        # no re-derivation. A second A record on that name (a hosts file, a split-horizon resolver)
        # would have become a standing allowlist entry for an address the guard otherwise refuses.
        # Nothing is lost by narrowing it: a loopback address is already exempt by address, so the
        # only entries this set ever needed are the ones an allowlisted name produced.
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


# The variables every consumer of this convention reads, lower case winning. `grpc_proxy` is here
# because grpc reads it *first* and then falls back to the other two; nothing else in this process
# looks at it.
_PROXY_VARIABLES = ("all_proxy", "grpc_proxy", "http_proxy", "https_proxy")


def _proxy_value(name: str) -> str:
    """One proxy variable's value, lower case winning — as httpx, requests, git and grpc read it.

    **Read here rather than through `urllib.request.getproxies_environment`, and that is a reversal
    with a measurement behind it.** Going through the stdlib was the right call when the consumers
    were all `urllib`-shaped, and it fixed a real bypass: a hand-rolled `name`/`name.upper()` read
    missed `Https_Proxy` and every other mixed-case spelling. This keeps that fix — both cases,
    lower winning, which is exactly what `getproxies_environment` does — and drops the one behaviour
    of it that is wrong for *these* consumers: CPython pops `http` from the mapping whenever
    `REQUEST_METHOD` is in the environment (the CGI `Proxy:` header defence, CVE-2016-1000110), and
    neither git nor grpc implements that carve-out. Measured with `REQUEST_METHOD=GET` and
    `HTTP_PROXY` set: `getproxies_environment()` returns `{}` while `git` still sends
    `GET http://…/info/refs` to the proxy. Reproducing the carve-out here would have made the
    refusal silent in exactly the case a child process is still proxied.
    """
    if os.environ.get(name, "").strip():
        return os.environ[name].strip()
    # Every other spelling, not just `.upper()`. Writing this as `name` plus `name.upper()` is the
    # bypass a reviewer already found once on this branch and which this function reintroduced
    # while fixing something else: measured, `Grpc_Proxy` gave grpc a live proxy and left the
    # check silent. `getproxies_environment` lower-cases *every* name for exactly this reason, and
    # prefers an exact lower-case hit when both spellings are set — both halves are reproduced here
    # rather than approximated.
    for key, value in os.environ.items():
        if key.lower() == name and value.strip():
            return value.strip()
    return ""


def _env_reading_destinations(settings: Any) -> list[tuple[str, str, tuple[str, ...]]]:
    """The destinations whose clients actually read a proxy variable, as (url, reader, variables).

    **This is deliberately not `derive_allowed`, and not "every http(s) destination" either.** A
    first version of this check charged the LLM gateway, the calc backend and every connector
    endpoint — and every client this repository builds for those passes `trust_env=False`, so a
    proxy variable cannot carry one of them. Measured with both variables set: zero proxy mounts on
    the gateway client against two on a default `httpx.Client`. The check refused processes over
    destinations that were already immune while the destinations that are *not* immune went
    uncharged, which is the module's own stated failure mode — "a refusal for a reason that is not
    true is a pod that will not start" — with a false negative behind it.

    It also broke a developer's checkout outright: the shipped destinations are loopback, so on a
    stock tree, importing `chemclaw.core.config` with any corporate `HTTP_PROXY` exported raised,
    and `pytest` collection died with it. Anyone behind such a proxy could not run this repository.
    (The proxy address is described rather than quoted, because `tests/test_no_egress.py` scans
    this file's *text* for `http(s)://` host literals and cannot tell a measurement in a docstring
    from a default in code — which is that guard working, and it caught this line.)

    So the question is not "which hosts does this process dial" but **"which of them are dialled by
    something that reads the environment"**, and today that is one:

    - **The OTLP span exporter.** `core/logging.py` uses the *gRPC* exporter, and grpc resolves
      `grpc_proxy` then `https_proxy` then `http_proxy` **regardless of the target's scheme** —
      measured, `http_proxy` alone carried a `https://` target, three `CONNECT` frames to the
      recorder. With `otel_include_sensitive_data` that traffic is prompts and completions.

    **The Entra JWKS endpoint was the second and is gone, which is a deletion this function had to
    make rather than keep.** It was charged here because `api/auth.py` fetched the key set through
    `urllib.request.urlopen`, which takes no `trust_env` and was measured following `HTTP_PROXY`.
    `_HttpxJwkClient` now fetches it with `httpx` and `trust_env=False`, so that destination is
    immune by construction — and a destination that is immune must leave this list, because what
    this function feeds is a *refusal*. Keeping the row would refuse a pod to boot over a hazard
    that no longer exists, which is the failure this module's own docstring names above: a refusal
    for a reason that is not true is a pod that will not start.

    **`git` is the third, it is still not charged here** (`docs/planning/BACKLOG.md`)**, and that is
    no longer the end of it.** The KG note writer shells out to `git push`, which inherits the
    environment and is measurably proxied — `_git_child_env` keeps every proxy variable on purpose,
    and a `git ls-remote` behind a loopback recorder sent it
    `CONNECT notes.example.invalid:443`. Its URL is still not on this
    object (`git_remote` is the string `"origin"`, and resolving it means `git remote get-url` in a
    subprocess at *config import*, a cost every entrypoint would pay at every start for a
    destination only one subsystem uses), so it cannot be a row here — a row needs a host to put in
    the message and to test `proxy_bypass` against. It is instead what `ambient_proxies` is for:
    a carrier with no derivable destination refuses on the *proxy*, under the enforced posture only.
    That is a narrower claim than a row would make and it is the one this function can support.
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

    **The carriers this answers for name no destination, which is why it takes no settings.** A
    `git` child (`kg/git_writer._git_child_env` keeps every proxy variable deliberately) and any
    dependency that builds its own HTTP client read these variables and reach hosts
    `_env_reading_destinations` cannot derive — so there is nothing to look up, and the only
    question left is whether a proxy is configured at all.

    **`no_proxy` is honoured only in its universal form, and that is a measurement rather than a
    simplification.** `proxy_bypass` answers a *per-host* question and there is no host here to ask
    it about; a sentinel host would be a fabrication that a specific `no_proxy` entry could match by
    accident. What can be honoured is `*`, which is what CPython's `proxy_bypass_environment`
    short-circuits on and what git implements: measured, `NO_PROXY=*` took the same `git ls-remote`
    off the recorder entirely — it resolved the host directly and the recorder saw nothing, where
    without it the recorder saw the `CONNECT`. Read through `_proxy_value` so the case rules are the
    one set this module already has, rather than a second reading of the same variable.
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

    Keyed by destination and *valued* with the proxy rather than the reverse, and holding both
    facts, because the message has to name all three. An earlier version mapped host to proxy alone
    and was overwritten when two variables named different proxies for one host — the declared one
    won the comparison and the undeclared one carried the traffic, which is a false pass on the one
    question this function exists to answer.
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

    **A proxy moves the destination out of the address, which is the one thing an allowlist guard
    cannot see.** Everything below `arm()` asks "which *host* may this process dial"; a client
    configured with a proxy dials the *proxy* and names the real destination in the request line.
    Measured with the allowlist empty and a local recorder standing in for a sidecar: a request to
    an external host through `proxy=http://127.0.0.1:<port>` returned HTTP 200 with the body, and
    `_refused` never moved. The loopback arm needs no allowlisting, because `_check` exempts
    loopback by construction and must keep exempting it: this process dials Postgres, Temporal and
    the calc backend there. An OpenShift service mesh or egress sidecar is a loopback proxy by
    design, and a sidecar shares the pod's network namespace, so its traffic never crosses a
    NetworkPolicy enforcement point either — for this shape there is no layer below this one.

    **What it charges is the narrow half, and `_env_reading_destinations` is where that argument
    is.** The first-party HTTP clients take `trust_env=False` (`core/http.gateway_client_kwargs`
    and four others), so a proxy variable cannot carry them and charging them refused deployments
    for a reason that was not true. What is charged is what reads the environment.

    **The second arm is not about a destination at all, and it exists because the first one can be
    empty while the hazard is not.** Measured: `entra_required=true` with `otel_enabled=false`
    charges nothing, so this function returned silently for the exact deployment the sidecar
    argument is about. The carriers that were left are the ones with no derivable destination — the
    `git` child whose environment `kg/git_writer` deliberately keeps every proxy variable in, and
    any dependency's own HTTP client — and for a **loopback** sidecar neither the allowlist (it sees
    the dial to the proxy), nor the NetworkPolicy (a sidecar shares the pod's network namespace),
    nor the `LD_PRELOAD` interposer (loopback is exempt by construction) can see the traffic. So
    under the enforced posture an *undeclared* proxy is itself refusable. It is gated on
    `entra_required` — this repository's existing signal for the deployment that believes it is in
    the enforced posture — rather than run unconditionally, because a stock checkout behind a
    corporate proxy must still import, which is a measured requirement here and not a courtesy.

    **The two arms raise separately because their remedies differ.** The first can offer `NO_PROXY`
    per destination, since it knows the destination; the second cannot, and offering it there would
    be a remedy that does not clear the refusal. Both messages open on the same string, so an
    operator greps one thing.

    Raises:
        RuntimeError: naming the proxy, what would carry it, and the one edit that proceeds — plus
            the destination and the reader where there is one. Loud at boot rather than loud on the
            first turn, and it reaches every process kind because it hangs off the
            `chemclaw.core.config` import every entrypoint makes — which is the property the gateway
            guard beside it did *not* have while it lived in `api/middleware.py`, and now has by
            being called from each entrypoint instead (`core/llm_gateway.py`).
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
    """Re-derive the allowlist without re-patching. Tests only — the patch itself is idempotent.

    **A pooled connection opened while a host was allowed survives its removal**, because `_check`
    is a connect-time hook: measured, a warm `httpx` pool returned 200 from a de-allowlisted host
    with the counter unmoved. That is unreachable in production — the allowlist is derived once at
    `chemclaw.core.config` import and never changes for the life of the process — so it is stated
    here rather than filed, at the one function that can make the allowlist move. A future "reload
    config" feature would make it real, and would need to close pools rather than only re-derive.
    """
    global _allowed
    _allowed = frozenset(allowed)
    _git_remote_host.cache_clear()
