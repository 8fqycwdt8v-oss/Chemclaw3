"""The HTTP fact more than one layer needs: what an address means.

One primitive rather than a module of its own, because it answers "how do we talk about somebody
else's HTTP endpoint" and exists only to stop a second copy appearing:

- **`is_loopback_host` / `is_loopback_url`** — the one definition of "this address cannot be
  reached from the network", which every safety rule in the tree that asks the question calls: the
  front door refuses to boot unauthenticated on a non-loopback *bind* (`api.middleware`, SEC-2),
  **every** process that makes a model call refuses to boot pointed at a loopback gateway
  (`core.llm_gateway`, in every posture — the non-loopback-*bind* condition this line used to state
  is exactly what `D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard` §2
  retired), a connector manifest refuses `auth: mode: none` for a non-loopback *endpoint*
  (`connectors.manifest`), and the egress guard permits a *destination* without allowlisting it
  (`core.netguard`). The questions differ; the answer must not, or one of them would be enforcing a
  weaker notion of "safe address" than the other claims. It lives here because `connectors -> api`
  is an edge the layering policy explicitly removed (`tests/test_layering.py`) and because
  `core.netguard` arms at config import, so the definition has to sit below both.

  **It was a set of three literal strings and a parsed predicate, and they disagreed.** Measured on
  2026-09-05, before this was one function: a second address in `127.0.0.0/8`, and the unspecified
  address, were loopback to the guard and not to the front door — so a pod bound non-loopback with
  its gateway on such an address passed `refuse_unconfigured_llm_gateway` (then in
  `api/middleware.py`, now `core/llm_gateway.py`), the check written to
  stop exactly that, and then failed every turn on a refused connection. (The addresses are
  described rather than written as URLs: `tests/test_no_egress.py` scans this file's *text* for
  `http(s)://` host literals and cannot tell a measurement in a docstring from a default in code,
  which is the guard working. `tests/test_netguard.py` holds them as data.)

  The set could not simply be widened to close it, because the two roles genuinely differ on one
  address: `0.0.0.0` as a *bind* is every interface (the whole subject of SEC-2) and as a
  *destination* never leaves the host. So this answers the narrow question only — the unspecified
  address is **not** loopback here — and `core.netguard` no longer exempts it. Nothing dialled it:
  the tree holds no `0.0.0.0` URL, and no local-server bind idiom reaches the guard with an
  unspecified or empty host (asyncio and `socket.create_server` resolve an IP literal without ever
  calling the resolver).

  **There is a third loopback constant and it is deliberately not this one.**
  `core.config.PG_LOOPBACK_HOSTS` asks a different question — *is this connection local*, for a TLS
  or plaintext exemption — and its extra member `""` is what makes the difference load-bearing
  rather than stylistic: a sink or channel URL with no host at all (a `file://` outbox) reads as
  local there, and `cli/validate_channels.py` already records the sharp edge in the present tense,
  that any "could not tell" answer therefore takes the exemption. Measured on 2026-09-05 across its
  three readers (`publish/drivers/postgres`, `publish/drivers/http`, `deliver/driver`), swapping it
  for `is_loopback_host` moves behaviour in **both** directions: it *widens* the exemption for
  `127.0.0.2`, the rest of `127.0.0.0/8`, a bracketed `[::1]` and a zone id, and it *narrows* it for
  the empty host, which would start demanding TLS of a hostless sink. Neither is a rename, both are
  publish-path behaviour changes with their own refusal messages, so the two stay separate and this
  paragraph is the one place that says why. `tests/test_netguard.py` names it as the single allowed
  second constant, so a *fourth* still fails.

  An address the parser cannot read falls on the demanding side, which is conservative in every
  caller: a bind refuses to boot, an endpoint demands a credential, a destination demands an
  allowlist entry. None of them waives anything. An IPv4-mapped literal follows its mapped address
  in both directions (`::ffff:127.0.0.1` is loopback, `::ffff:8.8.8.8` is not) — `ipaddress` does
  that itself, and this docstring said the opposite until `tests/test_netguard.py` was run.

- **`default_ssl_context`** — one TLS trust store for the whole process, shared by every
  *connector* client. Building one is not cheap and httpx builds a fresh one per client, so a
  seven-connector turn paid it seven times on the loop that serves every user on the pod
  (156.1 ms against 0.4 ms). It is deliberately not the gateway's context: that one is
  `gateway_client_kwargs` below, which answers a different question about trust.

- **`gateway_client_kwargs`** — the decisions a client reaching the model gateway must take,
  stated once. Both LLM seams need them and neither may own them: the chat client
  (`agent/llm_provider._tls_http_clients`) and the embedding client
  (`core/embeddings._openai_client`) reach the *same* gateway with the same CA bundle, and they
  wrote the same lines separately. A `dict` of kwargs rather than a client, because the only thing
  that legitimately differs between them is the class — and returning a client would force this
  module to pick sync or async for a caller that already knows. It used to be called
  `private_ca_transport` and to return `None` when no bundle was configured; that name and that
  return are why `trust_env=False` reached no shipped deployment
  (`D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address`).

There was a second primitive, `error_detail`, and the paragraph above used to say in the present
tense that "several modules (the Nextflow launcher, the Entra token/OBO exchanges)" called it. All
three were deleted — the launcher with the HPC tier in
`D-2026-08-26-semiempirical-is-the-whole-tier`, the OBO exchange in
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` — and the function outlived its
callers while the prose outlived the function. Whoever needs a bounded quotation of somebody else's
error body again should write it back with the caller that needs it, not before.
"""

import ipaddress
import socket
import ssl
from functools import cache
from typing import Any
from urllib.parse import urlsplit

import certifi


def parse_host(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address `host` names, in every spelling `connect(2)` accepts, or `None` for a name.

    **`ipaddress.ip_address` is not that set, and the gap was measured.** It accepts only the
    dotted-quad form, while `inet_aton(3)` — which is what `connect(2)` is ultimately handed, and
    what `socket.create_connection` reaches through `getaddrinfo` — also accepts the short, octal
    and hexadecimal forms. Driven on this tree against a real listener on `127.0.0.1:8820`, five
    spellings resolved to `127.0.0.1` and four of them were *not* loopback to this module:

        127.1  ·  2130706433  ·  0x7f.1  ·  0177.1   → peer ('127.0.0.1', 8820)

    So a deployment naming any of them as its model gateway booted clean and sent every prompt to
    whatever was listening inside its own pod, which is the failure `core.llm_gateway` exists to
    refuse. Nothing downstream caught it either: `core.netguard.derive_allowed` puts the same
    literal on the allowlist, and the compiled interposer sees `inet_ntop`'s canonical
    `127.0.0.1`, which is loopback-exempt.

    A *name* is never resolved — in `core.netguard` resolving one would itself be egress — so this
    answers `None` for anything that is not a literal, and every caller decides what that means.
    Whitespace disqualifies a host before `inet_aton` sees it, because that function tolerates
    trailing blanks and a "host" with a space in it is not one.

    Args:
        host: A bare host — a settings field, a URL's `hostname`, or a socket address's first
            element. A bracketed IPv6 literal and a zone id (`[::1]`, `fe80::1%eth0`) are read.

    Returns:
        The parsed address, or `None` when `host` is empty, a name, or unparseable.
    """
    if not host:
        return None
    bare = host.strip("[]").lower()
    if any(character.isspace() for character in bare):
        return None
    try:
        return ipaddress.ip_address(bare.split("%", 1)[0])
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(bare)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


def is_loopback_host(host: str | None) -> bool:
    """Whether `host` is unreachable from the network — decided by parsing, never by name.

    `localhost` and any literal that parses as a loopback IP (the whole of `127.0.0.0/8`, `::1`,
    and the short/octal/hex spellings `parse_host` covers) qualify. A *name* is never resolved — in
    `core.netguard` resolving it would itself be egress — so a `.localhost` suffix is not trusted:
    an `/etc/hosts` line or a wildcard zone would otherwise turn the suffix into "any destination"
    (the sibling fleet guard's own recorded bug).

    The unspecified address (`0.0.0.0`, `::`) and an empty host are **not** loopback here, because
    as a *bind* they mean every interface and two of the four callers are binds. As a *destination*
    `0.0.0.0` never leaves the host, and the one caller that reads a destination it can be wrong
    about — `core.llm_gateway` — asks `parse_host(...).is_unspecified` itself rather than moving
    this predicate under the binds' feet.

    Args:
        host: A bare host — a settings field like `service_host`, or a socket address's first
            element. `None` and `""` answer False.

    Returns:
        True when the address cannot be reached from the network.
    """
    if not host:
        return False
    if host.strip("[]").lower() == "localhost":
        return True
    address = parse_host(host)
    return address is not None and address.is_loopback


def is_loopback_url(url: str) -> bool:
    """Whether `url`'s host is a loopback interface — i.e. unreachable from the network.

    Conservative by construction: a URL whose host cannot be parsed at all (`urlsplit` raising on a
    malformed IPv6 literal, or a bare path with no authority) is *not* loopback. Every caller uses
    this to decide whether a credential is required, so the unparseable case must fall on the side
    that demands one rather than the side that waives it.

    Args:
        url: An absolute URL, e.g. a connector endpoint's `http://127.0.0.1:8811/mcp`.

    Returns:
        `is_loopback_host` of the URL's host.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return is_loopback_host(host)


@cache
def default_ssl_context() -> ssl.SSLContext:
    """The process's one TLS trust store, built once.

    **Why this exists at all: constructing it is not cheap, and httpx does it per client.** An
    `httpx.AsyncClient` with no `verify=` builds a fresh `ssl.SSLContext` and parses the whole
    certifi CA bundle into it. A turn opens one client per connector, so the shipped seven-connector
    deployment paid that seven times *per turn*, on the single event loop that serves every user on
    the pod, before any tool ran. Measured in this project's environment:

        7 default clients (one turn): 156.1 ms
        7 shared-context clients    :   0.4 ms

    — a 390x difference, and `load_verify_locations` was the largest single entry in a cProfile of
    connector setup (0.433 s of 1.371 s). It is *blocking* CPU rather than await time, so it does
    not merely slow the turn that pays it: at the shipped eight-turn admission cap it stalled every
    other user's stream on the pod for over a second, and there is a cliff behind that — around
    45-50 concurrent opens the client's own CPU exceeds `connector_open_timeout_seconds`, healthy
    connectors are recorded unreachable, and the pod then serves turns with no tools at all.

    **`cafile=certifi.where()` is not decoration — without it this changes what the fleet trusts.**
    The first version of this function returned a bare `ssl.create_default_context()`, and that is
    not the context httpx would have built: `verify=True` passes `cafile=certifi.where()`, while a
    bare call loads the *operating system* store. Measured in this environment, **138 roots against
    109 — 42 CAs newly trusted and 13 dropped** on every connector call and the bearer token it
    carries; on a hardened image with no `ca-certificates` package it loads **zero** roots and every
    `https://` connector fails at handshake. Both call sites also pass `trust_env=False` to refuse
    ambient environment, and a bare context silently defeats that too, because `load_default_certs`
    reads `SSL_CERT_FILE`/`SSL_CERT_DIR` where `verify=True` does not — measured, an `SSL_CERT_FILE`
    pointing at a one-certificate bundle took httpx's 118 roots down to **1**. A performance fix is
    not a licence to move a trust boundary.

    **Sharing one context between clients is safe, but not for the reason first written here.** The
    original said "a context is read-only in use", and that is false: `httpcore` calls
    `set_alpn_protocols` on it at every TLS connect. It is safe because every client in this process
    writes the *same* ALPN list — nothing here sets `http2=True` — so the write is idempotent. If a
    caller ever enables HTTP/2, that stops being true and this has to become a per-profile context.
    A caller needing a different *trust* decision passes its own `verify=` and does not come here;
    `private_ca_transport` below already does exactly that.

    Most endpoints this is handed to are plain in-cluster `http://`, where the context is never
    consulted at all. That is not a reason to skip it: the cost was paid on construction, not on
    use, which is precisely why it was invisible.
    """
    return ssl.create_default_context(cafile=certifi.where())


def gateway_client_kwargs(ca_bundle: str = "") -> dict[str, Any]:
    """The httpx client kwargs for a client this process builds to reach the model gateway.

    Two decisions, and the second one is why this function stopped being allowed to return `None`:

    - **`trust_env=False`, always.** `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` set on the pod would
      otherwise redirect every prompt, completion, embedded note and `Authorization` bearer to a
      host of the env setter's choosing — and a proxy *re-terminates TLS*, so it walks past the CA
      pinning below rather than being caught by it.
    - **An `SSLContext`, always, not `verify="<path>"` and not nothing.** httpx deprecated the
      string form ("`verify=<str>` is deprecated. Use `verify=ssl.create_default_context(...)`"),
      and building the context is also the only form that says what the bundle *is* — a CA file to
      verify the peer against, rather than a path httpx has to guess the meaning of. It is built on
      the no-bundle branch too, and it reproduces httpx's own `trust_env=True` precedence —
      configured bundle, else `SSL_CERT_FILE`, else `SSL_CERT_DIR`, else certifi — because
      `trust_env` conflates proxy discovery with the trust store and only the first is objected to
      here. **Exclusive, one source, never a union**, which is what httpx does and what a first
      version of this got wrong. Getting that precedence wrong in either direction silently changes
      which certificates every deployment trusts, and one of the two directions is invisible to
      `get_ca_certs()`; the comment below has both measurements.

    **This returned `None` for the whole no-bundle branch until 2026-09-05, and that made the first
    decision unreachable in every shipped configuration.** `llm_tls_ca_bundle` defaults to `""` and
    is set nowhere in `deploy/`, `infra/` or `.env.example`, so both callers took the `None` branch,
    passed no client to the SDK, and the SDK built its own with httpx's default `trust_env=True`.
    Measured on that configuration with `HTTP_PROXY` pointed at a local recorder: the recorder
    received `POST /v1/chat/completions` carrying the prompt body and the gateway bearer, and
    `netguard._refused` never moved — the guard cannot see it, because the socket layer is dialling
    the proxy's own address and the destination has left the address entirely.

    So the kwargs are unconditional and a caller must always build the client. The CA half is the
    part that is conditional, which is the opposite of how this read before.

    Args:
        ca_bundle: Path to the CA bundle (`settings.llm_tls_ca_bundle`), or "" to take the store
            from the environment, else certifi. Not "the system store": OpenSSL's own default
            paths are what this deliberately does *not* fall through to.

    Returns:
        Kwargs for `httpx.Client(**kwargs)` / `httpx.AsyncClient(**kwargs)`. Never None.
    """
    import os
    import ssl

    import certifi

    # **This reproduces what httpx does with `trust_env=True`, minus the proxy** — including that
    # its precedence is *exclusive*. `httpx._config.create_ssl_context` is
    # `if SSL_CERT_FILE: … elif SSL_CERT_DIR: … else certifi`, one source and never a union, and a
    # first version of this function merged them: `cafile` conditional, `capath` unconditional. The
    # consequence was the opposite of this function's purpose, and only a real handshake could see
    # it — `SSLContext.get_ca_certs()` does not report a `capath` at all, so the table that
    # "verified" the merge agreed with itself while the stores diverged. Driven against a rogue CA
    # dropped into a hashed directory, with `llm_tls_ca_bundle` pinned to a *different* store:
    #
    #     old private_ca_transport (pin, trust_env=False)  -> refuses the rogue CA
    #     httpx trust_env=True + the same pin              -> refuses the rogue CA
    #     the merged version                               -> TRUSTS it
    #
    # One environment variable adding roots to a client whose whole point is that the environment
    # cannot redirect it, on the one path `llm_tls_ca_bundle` exists for. `trust_env` conflates
    # proxy discovery with the trust store, only the first is objected to here, and reproducing the
    # second means reproducing its precedence exactly rather than approximately.
    #
    # Getting it wrong in the *other* direction is just as silent: passing
    # `create_default_context(cafile=None)` picks up `SSL_CERT_FILE` but on the shipped
    # configuration falls through to OpenSSL's default paths and drops certifi — measured, 152
    # certificates from the OS store against the SDK's previous 118, with 13 roots in certifi and
    # not in the OS store.
    cafile: str | None = None
    capath: str | None = None
    if ca_bundle:
        cafile = ca_bundle
    elif os.environ.get("SSL_CERT_FILE"):
        cafile = os.environ["SSL_CERT_FILE"]
    elif os.environ.get("SSL_CERT_DIR"):
        capath = os.environ["SSL_CERT_DIR"]
    else:
        cafile = certifi.where()
    return {
        "trust_env": False,
        "verify": ssl.create_default_context(cafile=cafile, capath=capath),
    }
