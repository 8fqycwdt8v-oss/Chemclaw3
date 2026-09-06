"""Conditional GET for the two read routes a surface fetches over and over.

The frontend caches `GET /sessions/{id}/tool-results/{ref}` and `GET /notes/{id}` client-side and
asked for `Cache-Control: public, max-age=31536000, immutable` on both, on the premise that a
content-addressed URL names content that cannot change. **Both halves of that premise turned out to
be wrong here**, in opposite ways, and this module is what the measurement left:

**`immutable` is a promise about the response body, and neither body is immutable.**

- A tool-result *ref* is the SHA-256 of the result text, so `text` and `byte_size` genuinely cannot
  change under a given URL. `tool` and `correlation_id` can and routinely do: `_UPSERT_LINK`
  (`api/tool_results.py`) keys a link on `(session_id, content_hash)` and **collapses a disagreeing
  label to `''`**, so the second turn in one session that produces identical text rewrites the row
  a client already fetched. `correlation_id` is per-turn, which makes any repeat enough, and
  `include_detailed_errors` is off, so every unexpected tool failure in the system returns the same
  byte string — one row per session collapsing the moment a second tool fails.
  `tests/test_tool_results.py::test_a_result_two_calls_produced_names_neither_of_them` proved this
  before this module existed. A year-long `immutable` would pin the withdrawn label in the client
  for a year, which is exactly the mispairing that store refuses on the write side.
- A note id is *stable across edits* by construction: the graph is Markdown in Git, a PR-gate merge
  rewrites a note's body under the same id, and the neighbourhood is other notes' business
  entirely — a new note linking here changes this response with nothing about this note touched.
  And `Note.is_current` is evaluated against `date.today()`, so a neighbour leaves the view on the
  day its `valid_to` passes **with no write at all**. Nothing about that URL is content-addressed.

**`public` is wrong on both, and worse on the first.** A tool result belongs to one session and one
owner; `resolve_session` is what protects it, and the URL carries no principal. `public` invites any
shared cache — an ingress cache, a corporate proxy — to store one owner's result and serve it on
that URL to the next caller, which is the ownership gate removed by a response header. `/notes/{id}`
is deliberately not owner-scoped (the graph has no owner), so a shared copy would leak nothing
between chemists; it is still `private`, because it is `CurrentUser`-gated and a shared cache would
serve it to callers who never presented a credential and are in nobody's rate budget. Neither route
carries a `Vary: Authorization` that would make a shared copy safe, and adding one would be a
second, weaker statement of a gate that already exists.

So: **`private, no-cache` plus a strong `ETag`.** `no-cache` is "store it, and revalidate before
reusing it" — not "do not store it" — so the client keeps its copy and the repeat fetch becomes a
conditional request. What that buys is the half of the frontend's ask that is honest here: a
revalidation that returns 304 with no body instead of re-sending a result up to
`stream_max_result_bytes` or a note view with its whole neighbourhood. What it deliberately does not
buy is skipping the request, because on these two resources skipping it can serve a wrong answer.

No freshness lifetime is configured, and that is deliberate rather than an omission: any `max-age`
here would be a guess at how long a label collapse or a note edit may go unnoticed, and there is no
measurement that produces one. `no-cache` needs no number, so this module adds no setting.
"""

import hashlib

from fastapi import Request, Response
from pydantic import BaseModel

# `private` because both resources sit behind an authorization gate the URL does not encode, and
# `no-cache` because both bodies can change under a stable URL (see the module docstring). One
# constant for two routes, so the two cannot drift into disagreeing policies.
_CACHE_CONTROL = "private, no-cache"


def _etag(payload: BaseModel) -> str:
    """A strong validator for `payload`: the SHA-256 of its serialized body, quoted.

    Over `model_dump_json()` rather than over any one field, because the validator has to cover
    *everything* the caller will render — the tool-result labels are precisely the part that moves
    while the addressed bytes do not, and a validator derived from the ref would say "unchanged"
    across the one change that happens. Pydantic emits fields in declaration order, so the same
    model instance serializes identically in two processes.

    Strong (unquoted by `W/`) is the honest strength: two responses with this validator are
    byte-identical, not merely equivalent.
    """
    digest = hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()
    return f'"{digest}"'


def _already_held(if_none_match: str | None, etag: str) -> bool:
    """Whether the caller's `If-None-Match` covers `etag` (RFC 9110 §13.1.2).

    `*` matches any current representation, and the header is a *list* — a client that has seen
    two versions of a note may legitimately offer both. Weak comparison is what the spec requires
    for `If-None-Match`, so a `W/` prefix is stripped before comparing rather than treated as a
    mismatch; this module only ever mints strong validators, but a proxy in between may weaken one.
    """
    if not if_none_match:
        return False
    candidates = {tag.strip() for tag in if_none_match.split(",")}
    if "*" in candidates:
        return True
    return any(tag.removeprefix("W/") == etag for tag in candidates)


def revalidatable(request: Request, response: Response, payload: BaseModel) -> Response | None:
    """Stamp the caching policy on `response`; return a 304 when the caller already holds `payload`.

    Returns `None` for "send the payload" so a handler reads as
    `return not_modified if not_modified is not None else payload` — the headers are already on the
    injected `Response` FastAPI will build the 200 from, and the 304 carries them itself because a
    `Response` returned from a handler is used as-is and the injected one is discarded.

    A 304 must carry the validator and the caching policy and no body (RFC 9110 §15.4.5); Starlette
    omits `content-length` for that status, so an empty `Response` is already the right shape.

    The payload is computed before this is called, on both routes and unavoidably: the ETag covers
    fields that only the database read produces, and the ownership gate is that same read. So this
    saves the response body and the client's re-render, never the server's work — which is the
    honest scope of a validator on a resource whose identity is not its content.
    """
    etag = _etag(payload)
    headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
    response.headers.update(headers)
    if _already_held(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return None
