"""Conditional GET for the two read routes a surface fetches repeatedly.

`GET /sessions/{id}/tool-results/{ref}` and `GET /notes/{id}` are served `private, no-cache` with a
strong `ETag`, never `public, immutable`:

- Neither body is immutable. A tool-result ref addresses the text, but its `tool` and
  `correlation_id` labels can collapse to `''` when another turn produces identical text
  (`api/tool_results.py`). A note id is stable across edits, its neighbourhood changes when other
  notes link to it, and `Note.is_current` depends on today's date.
- Neither is public. A tool result is protected by `resolve_session` and a note by `CurrentUser`;
  the URL carries no principal, so a shared cache would serve them past the gate.

`no-cache` means "store, then revalidate", so a repeat fetch becomes a conditional request that can
return 304 without a body. No `max-age`: there is no measured safe staleness.
"""

import hashlib

from fastapi import Request, Response
from pydantic import BaseModel

# One policy for both routes: `private` because the gate is not in the URL, `no-cache` because the
# body can change under a stable URL.
_CACHE_CONTROL = "private, no-cache"


def _etag(payload: BaseModel) -> str:
    """A strong validator for `payload`: the SHA-256 of its serialized body, quoted.

    Over the whole `model_dump_json()`, since the labels move while the addressed bytes do not.
    Pydantic serializes fields in declaration order, so the hash is stable across processes.
    """
    digest = hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()
    return f'"{digest}"'


def _already_held(if_none_match: str | None, etag: str) -> bool:
    """Whether the caller's `If-None-Match` covers `etag` (RFC 9110 §13.1.2).

    Handles `*` and a list of tags, and compares weakly as the spec requires (a proxy may add `W/`).
    """
    if not if_none_match:
        return False
    candidates = {tag.strip() for tag in if_none_match.split(",")}
    if "*" in candidates:
        return True
    return any(tag.removeprefix("W/") == etag for tag in candidates)


def revalidatable(request: Request, response: Response, payload: BaseModel) -> Response | None:
    """Stamp the caching policy on `response`; return a 304 when the caller already holds `payload`.

    `None` means "send the payload": the headers are already on the injected `Response`. A returned
    304
    is used as-is, so it carries the validator and policy itself, with no body (RFC 9110 §15.4.5).
    The
    payload is computed first in both routes — the ETag and the ownership gate need the read — so
    this
    saves the transfer and re-render, not the server's work.
    """
    etag = _etag(payload)
    headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
    response.headers.update(headers)
    if _already_held(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return None
