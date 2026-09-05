"""The HTTP fact more than one layer needs: what an address means.

One primitive rather than a module of its own, because it answers "how do we talk about somebody
else's HTTP endpoint" and exists only to stop a second copy appearing:

- **`default_ssl_context`** — one TLS trust store for the whole process, because building one is
  expensive and every caller wants the same one. Measured below.
- **`LOOPBACK_HOSTS` / `is_loopback_url`** — the one definition of "this address cannot be reached
  from the network", which two unrelated safety rules ask about: the front door refuses to boot
  unauthenticated on a non-loopback *bind* (`api.middleware`, SEC-2), and a connector manifest
  refuses `auth: mode: none` for a non-loopback *endpoint* (`connectors.manifest`). The two
  questions differ; the answer must not, or one of them would be enforcing a weaker notion of
  "safe address" than the other claims. It lives here because `connectors -> api` is an edge the
  layering policy explicitly removed (`tests/test_layering.py`).

There was a second primitive, `error_detail`, and the paragraph above used to say in the present
tense that "several modules (the Nextflow launcher, the Entra token/OBO exchanges)" called it. All
three were deleted — the launcher with the HPC tier in
`D-2026-08-26-semiempirical-is-the-whole-tier`, the OBO exchange in
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` — and the function outlived its
callers while the prose outlived the function. Whoever needs a bounded quotation of somebody else's
error body again should write it back with the caller that needs it, not before.
"""

import ssl
from functools import cache
from urllib.parse import urlsplit

# Loopback interfaces: an address here is reachable only from the local host, so an unauthenticated
# service or connector on one is not a network-exposed footgun. Anything else is — notably the
# `service_host="0.0.0.0"` default and every in-cluster Service name.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_url(url: str) -> bool:
    """Whether `url`'s host is a loopback interface — i.e. unreachable from the network.

    Conservative by construction: a URL whose host cannot be parsed at all (`urlsplit` raising on a
    malformed IPv6 literal, or a bare path with no authority) is *not* loopback. Every caller uses
    this to decide whether a credential is required, so the unparseable case must fall on the side
    that demands one rather than the side that waives it.

    Args:
        url: An absolute URL, e.g. a connector endpoint's `http://127.0.0.1:8811/mcp`.

    Returns:
        True when the host is one of `LOOPBACK_HOSTS`.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


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

    **Sharing one context between clients is safe and is what httpx documents.** A context is
    read-only in use, carries no per-connection state, and holds the same trust decision every
    caller here wants: the system trust store as certifi sees it. A caller needing a *different*
    trust decision — a private CA — passes its own `verify=` and does not come here; the embedding
    client already does exactly that.

    Most endpoints this is handed to are plain in-cluster `http://`, where the context is never
    consulted at all. That is not a reason to skip it: the cost was paid on construction, not on
    use, which is precisely why it was invisible.
    """
    return ssl.create_default_context()
