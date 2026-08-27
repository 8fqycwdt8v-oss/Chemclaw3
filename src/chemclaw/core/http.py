"""The HTTP fact more than one layer needs: what an address means.

One primitive rather than a module of its own, because it answers "how do we talk about somebody
else's HTTP endpoint" and exists only to stop a second copy appearing:

- **`LOOPBACK_HOSTS` / `is_loopback_url`** — the one definition of "this address cannot be reached
  from the network", which two unrelated safety rules ask about: the front door refuses to boot
  unauthenticated on a non-loopback *bind* (`api.middleware`, SEC-2), and a connector manifest
  refuses `auth: mode: none` for a non-loopback *endpoint* (`connectors.manifest`). The two
  questions differ; the answer must not, or one of them would be enforcing a weaker notion of
  "safe address" than the other claims. It lives here because `connectors -> api` is an edge the
  layering policy explicitly removed (`tests/test_layering.py`).
- **`same_origin`** — the one definition of "this request is still talking to the endpoint we
  meant", which is what decides whether the turn's identity headers may travel on it. Two callers
  ask it of two different transports: `connectors.identity.turn_identity_hook` for a bundle's own
  client, and `core.mcp_session.open_session` for the MCP session this kernel opens on a caller's
  behalf. Same reason as above — the two must not answer differently, or one of them is stripping
  identity where the other leaks it.

There was a second primitive, `error_detail`, and the paragraph above used to say in the present
tense that "several modules (the Nextflow launcher, the Entra token/OBO exchanges)" called it. All
three were deleted — the launcher with the HPC tier in
`D-2026-08-26-semiempirical-is-the-whole-tier`, the OBO exchange in
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` — and the function outlived its
callers while the prose outlived the function. Whoever needs a bounded quotation of somebody else's
error body again should write it back with the caller that needs it, not before.
"""

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


# The port a scheme means when a URL does not spell one out, so an address written bare and the same
# address written with an explicit `:80` are one origin — the normalization httpx itself does before
# it strips `Authorization` on a redirect.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def same_origin(url: str, other: str) -> bool:
    """Whether two URLs share a scheme, host and port — i.e. name the same server.

    The question behind it is always the same: an httpx request hook runs on *every* hop of a
    redirect chain, and httpx carries the previous request's headers into the redirected one,
    dropping `Authorization` alone. So a server that answers `302` toward an origin it controls
    harvests everything else — which for this system is the caller's Entra object id, their session
    and the turn's correlation id. Both callers use this to remove those headers rather than merely
    decline to re-add them, because the copied originals arrive on the foreign hop anyway.

    Conservative in the same direction as `is_loopback_url`: a URL that cannot be parsed does not
    match anything, so an unreadable address is treated as foreign and the headers are stripped.

    Args:
        url: The URL a request is actually going to.
        other: The endpoint the caller meant to talk to.

    Returns:
        True when both name the same (scheme, host, port).
    """

    def origin(value: str) -> tuple[str, str, int] | None:
        try:
            parts = urlsplit(value)
        except ValueError:
            return None
        if not parts.hostname:
            return None
        return (
            parts.scheme,
            parts.hostname,
            parts.port or _DEFAULT_PORTS.get(parts.scheme, 0),
        )

    first = origin(url)
    return first is not None and first == origin(other)
