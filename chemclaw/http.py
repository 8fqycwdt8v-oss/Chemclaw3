"""Bounded, safe formatting of a failed HTTP response for logs and exceptions.

Why this exists: several modules (the Nextflow launcher, the Entra token/OBO exchanges) turn an
upstream failure into an exception or log line. Interpolating the raw `response.text` can splatter
an unbounded upstream body — an HTML error page, a reverse-proxy dump — into that record. This caps
the body once (DRY) so every caller reports "<status> <reason>: <body>" instead of a whole page.
"""

import httpx

# How many characters of an upstream error body to keep: enough to diagnose the failure, not a whole
# error page. A module constant (like the audit/tool-arg previews elsewhere), not a tuning knob.
_ERROR_BODY_MAX_CHARS = 500


def error_detail(response: httpx.Response, *, limit: int = _ERROR_BODY_MAX_CHARS) -> str:
    """Return a bounded "STATUS REASON: BODY" summary of a failed HTTP response for logs/errors.

    The body is truncated to `limit` characters (with an ellipsis when cut) so a large or hostile
    upstream response cannot flood the log. On a failed request an OAuth/launcher body carries an
    error description, not a credential, so a bounded excerpt is safe and useful for diagnosis.
    """
    body = response.text
    if len(body) > limit:
        body = body[:limit] + "…"
    reason = response.reason_phrase or ""
    return f"{response.status_code} {reason}: {body}".rstrip()
