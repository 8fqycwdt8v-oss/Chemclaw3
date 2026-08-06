"""The HTTP facts more than one layer needs: what a failed response may say, and what is loopback.

Why the first exists: several modules (the Nextflow launcher, the Entra token/OBO exchanges) turn an
upstream failure into an exception or log line. Interpolating the raw `response.text` can splatter
an unbounded upstream body — an HTML error page, a reverse-proxy dump — into that record. This caps
the body once (DRY) so every caller reports "<status> <reason>: <body>" instead of a whole page.

Why the second: "reachable only from this machine" is the condition both unauthenticated modes in
this system are allowed under — the front door's dev principal (`api.middleware`) and a connector
served with no credential (`connectors.identity`) — and two spellings of that set would be two
different security boundaries wearing one name.
"""

import httpx

# How many characters of an upstream error body to keep: enough to diagnose the failure, not a whole
# error page. A module constant (like the audit/tool-arg previews elsewhere), not a tuning knob.
_ERROR_BODY_MAX_CHARS = 500

# Loopback interfaces: binding or addressing one of these keeps an unauthenticated mode reachable
# only from the local host, so it is a dev convenience rather than a network-exposed footgun.
# Anything else (notably the "0.0.0.0" bind default and any in-cluster Service name) is.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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
