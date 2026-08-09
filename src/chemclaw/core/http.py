"""The HTTP facts more than one layer needs: what an address means, and how a failure is quoted.

Two small primitives rather than a module each, because both answer "how do we talk about somebody
else's HTTP endpoint" and both exist only to stop a second copy appearing:

- **`error_detail`** — several modules (the Nextflow launcher, the Entra token/OBO exchanges) turn
  an upstream failure into an exception or log line. Interpolating the raw `response.text` can
  splatter an unbounded upstream body — an HTML error page, a reverse-proxy dump — into that record.
  This caps the body once so every caller reports "<status> <reason>: <body>" instead of a whole
  page.
- **`LOOPBACK_HOSTS` / `is_loopback_url`** — the one definition of "this address cannot be reached
  from the network", which two unrelated safety rules ask about: the front door refuses to boot
  unauthenticated on a non-loopback *bind* (`api.middleware`, SEC-2), and a connector manifest
  refuses `auth: mode: none` for a non-loopback *endpoint* (`connectors.manifest`). The two
  questions differ; the answer must not, or one of them would be enforcing a weaker notion of
  "safe address" than the other claims. It lives here because `connectors -> api` is an edge the
  layering policy explicitly removed (`tests/test_layering.py`).
"""

from urllib.parse import urlsplit

import httpx

# How many characters of an upstream error body to keep: enough to diagnose the failure, not a whole
# error page. A module constant (like the audit/tool-arg previews elsewhere), not a tuning knob.
_ERROR_BODY_MAX_CHARS = 500

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


def error_detail(response: httpx.Response) -> str:
    """Return a bounded "STATUS REASON: BODY" summary of a failed HTTP response for logs/errors.

    The body is truncated to `_ERROR_BODY_MAX_CHARS` (with an ellipsis when cut) so a large or
    hostile upstream response cannot flood the log. On a failed request an OAuth/launcher body
    carries an error description, not a credential, so a bounded excerpt is safe and useful for
    diagnosis.
    """
    body = response.text
    if len(body) > _ERROR_BODY_MAX_CHARS:
        body = body[:_ERROR_BODY_MAX_CHARS] + "…"
    reason = response.reason_phrase or ""
    return f"{response.status_code} {reason}: {body}".rstrip()
