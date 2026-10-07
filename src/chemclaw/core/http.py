"""The HTTP facts more than one layer needs: what an address means, and how to trust a peer.

- `is_loopback_host` / `is_loopback_url`: the one definition of "this address cannot be reached
  from the network", used by the front door's bind check, the LLM gateway guard, connector
  manifest auth and the egress guard, so none of them enforces a weaker notion than the others. It
  sits in `core` because `core.netguard` arms at config import and `connectors -> api` is a
  forbidden edge. An unparseable address falls on the demanding side in every caller.
  `core.config.PG_LOOPBACK_HOSTS` is a deliberately separate constant: it asks "is this connection
  local" and treats an empty host as local, which this predicate does not.
- `default_ssl_context`: one TLS trust store shared by every connector client, because building one
  per client is expensive blocking CPU on the event loop.
- `gateway_client_kwargs`: the transport decisions every client reaching the model gateway takes
  (chat and embeddings alike), returned as kwargs so the caller picks sync or async.
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

    `ipaddress.ip_address` accepts only dotted quads, while `inet_aton(3)` — what `connect(2)` is
    ultimately given — also accepts short, octal and hex forms (`127.1`, `2130706433`, `0x7f.1`), so
    both are tried; otherwise those spellings would evade every loopback check. A name is never
    resolved (in `core.netguard` that would itself be egress). Whitespace disqualifies a host, since
    `inet_aton` tolerates trailing blanks.

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

    `localhost` and any literal parsing as a loopback IP (all of `127.0.0.0/8`, `::1`, and the
    spellings `parse_host` covers) qualify. A `.localhost` suffix is not trusted, since names are
    never resolved. The unspecified address and an empty host are not loopback, because as a bind
    they mean every interface; `core.llm_gateway` checks `is_unspecified` itself for the destination
    case.

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

    A URL whose host cannot be parsed is not loopback, so callers deciding whether a credential is
    required fall on the side that demands one.

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

    httpx builds and loads a fresh `SSLContext` per client, and a turn opens one client per
    connector; that blocking CPU on the shared event loop stalls every user's stream.
    `cafile=certifi.where()` matches what httpx's `verify=True` trusts; a bare default context would
    load the OS store and honour `SSL_CERT_FILE`, changing what the fleet trusts.

    Sharing is safe only because every client writes the same ALPN list (httpcore sets it per
    connect) and nothing enables HTTP/2; if one does, this must become a per-profile context. A
    caller needing a different trust decision passes its own `verify=`.
    """
    return ssl.create_default_context(cafile=certifi.where())


def gateway_client_kwargs(ca_bundle: str = "") -> dict[str, Any]:
    """The httpx client kwargs for a client this process builds to reach the model gateway.

    - `trust_env=False`, always: an ambient `HTTP(S)_PROXY` would otherwise redirect every prompt
      and bearer token to a host of the env setter's choosing, re-terminating TLS past the CA
      pinning and invisibly to the egress guard (the socket only sees the proxy's address).
    - An `SSLContext`, always, reproducing httpx's `trust_env=True` trust precedence exclusively
      (one
      source, never a union): configured bundle, else `SSL_CERT_FILE`, else `SSL_CERT_DIR`, else
      certifi. Only proxy discovery is objected to, not the trust store.

    Callers must always build their client from these kwargs; leaving it to the SDK reinstates
    `trust_env=True`.

    Args:
        ca_bundle: Path to the CA bundle (`settings.llm_tls_ca_bundle`), or "" to take the store
            from the environment, else certifi. OpenSSL's default paths are never used.

    Returns:
        Kwargs for `httpx.Client(**kwargs)` / `httpx.AsyncClient(**kwargs)`. Never None.
    """
    import os
    import ssl

    import certifi

    # Exclusive precedence, as `httpx._config.create_ssl_context` does it. Merging sources would let
    # one environment variable add roots to a pinned client (`get_ca_certs()` does not even report a
    # `capath`), and `cafile=None` would fall through to OpenSSL's default paths and drop certifi.
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
